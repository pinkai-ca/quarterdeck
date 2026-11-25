"""pi: one JSON-lines file per session under ``sessions/`` in the seat home.

The first line is the session, with its id and working directory; ``model_change`` and
``thinking_level_change`` lines carry the model and the thinking level; ``message`` lines carry the
conversation: a user message, the assistant messages that answer it, each with its model, usage,
cost and stop reason, and the tool results between them. A message typed while a turn runs is
steered into it after the current tool call, and enters the record only then. Proven on 0.74.2
against a local vLLM endpoint.
"""

from __future__ import annotations

from .. import seat_files
from .capabilities import (DefaultMetadata, EffortCapability, SuggestedValue, reject_default_overrides,
                           reject_unclassified_overrides)

import json
import hashlib
import re
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from .. import durable, endpoint
from ..pricing import Price, amount
from .endpoint import EndpointHarness
from .costs import CostStep, costed, model_name
from .json_record import JsonLinesReader, input_reader, input_rows
from .record_file import Opened
from .types import CappedGeneration, Inputs, _cells, _pi_wrap

from ..ship import Pool, SeatSpec, ShipError
from .base import (Box, ship_template, trimmed, Input, Control, Credential, Harness, Interruption, Keys, Launch, RecordError, Screen, State, Tokens,
                   ToolCall, Turn, entries, merged, program_version, when)

SETTINGS = {
    "defaultProjectTrust": "always",  # the workspace is the ship's: no trust dialog
    "quietStartup": True,
    "enableInstallTelemetry": False,
}
UNMEASURED_STOPS = {"aborted", "error"}
ENDS_TURN = {"stop", "length", *UNMEASURED_STOPS}
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh")


# -- the record ---------------------------------------------------------------------------------


def records(home: Path) -> list[Path]:
    from .. import record_location
    folder = home / "sessions"
    return sorted({*(p for p in folder.glob("*.jsonl") if p.is_file()),
                   *record_location.records(home, "sessions/*.jsonl")})


def launch_ended(record: Path) -> bool:
    """Whether this record's launch has ended, judged only from qd's trusted launch record of the seat's current
    launch: that launch found this record in its home before it began (``records_before``), and does not resume it
    (its ``--session`` names another or none). A launch that has ended writes nothing more, so a turn it left open was
    cut, not paused. A record the current launch writes or resumes, or a home with no readable launch record, makes
    no claim of an ending; nothing the seat's jail writes in its home counts here. A retired record has ended: it lies
    in the service's own tree, which no jail writes and no launch resumes, whether it is named there or in the home."""
    from .. import record_location, trusted_files
    record = Path(record)
    if record.parent.name != "sessions" or record.suffix != ".jsonl":
        return False  # not a launch's own record: no claim of an ending is made
    if record_location.retired(record):
        return True
    try:
        data = json.loads((trusted_files.of_home(record.parent.parent) / trusted_files.LAUNCH).read_text())
    except (OSError, ValueError):
        return False
    before = data.get("records_before") if isinstance(data, dict) else None
    argv = data.get("argv") if isinstance(data, dict) else None
    if not isinstance(before, list) or not isinstance(argv, list) or str(record) not in before:
        return False
    resumed = [argv[at + 1] for at, word in enumerate(argv[:-1]) if word == "--session"]
    if data.get("resume") is not None and not resumed:
        return False  # a resumed launch that names no record: which it resumed is not known
    return str(record) not in resumed


def _session(record: Path) -> dict | None:
    return next((e for e in entries(record) if e.get("type") == "session"), None)


def first_timestamp(record: Path) -> datetime | None:
    session = _session(record)
    return when(session.get("timestamp")) if session else next((when(e["timestamp"]) for e in entries(record) if e.get("timestamp")), None)


def session_id(record: Path) -> str | None:
    session = _session(record)
    return session.get("id") if session else None


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(block.get("text", "") for block in content or [] if isinstance(block, dict) and block.get("type") == "text")


def _tokens(message: dict) -> Tokens:
    usage = message.get("usage")
    if isinstance(usage, dict) and message.get("stopReason") in UNMEASURED_STOPS and not usage.get("totalTokens"):
        return Tokens.unknown()  # the program initializes an aborted step with zero counters before any response
    return Tokens.from_usage(usage, ("cacheRead", "input", "cacheWrite", "output"), total="totalTokens")


def _compaction_row(entry):
    """The measured native checkpoint, not new input or evidence ending a tool."""
    fields = {'type': str, 'id': str, 'parentId': str, 'timestamp': str,
              'summary': str, 'firstKeptEntryId': str, 'tokensBefore': int,
              'fromHook': bool, 'details': dict}
    if (not isinstance(entry, dict) or entry.keys() != fields.keys()
            or any(type(entry[key]) is not kind for key, kind in fields.items())
            or entry['type'] != 'compaction' or entry['fromHook'] is not False
            or entry['tokensBefore'] < 0
            or not all(entry[key] for key in ('id', 'parentId', 'summary', 'firstKeptEntryId'))):
        return False
    details = entry['details']
    if (details.keys() != {'readFiles', 'modifiedFiles'}
            or type(details['readFiles']) is not list or not details['readFiles']
            or any(type(name) is not str for name in details['readFiles'])
            or type(details['modifiedFiles']) is not list or details['modifiedFiles']):
        return False
    try:
        return datetime.fromisoformat(entry['timestamp']).tzinfo is not None
    except ValueError:
        return False


class _Reader(JsonLinesReader):
    kinds = ('session', 'message', 'model_change', 'thinking_level_change')
    label = "pi"
    limit_reasons = frozenset({"length"})  # measured on 15 Nov: 123 of pi's ended assistant messages in crew records

    @classmethod
    def read_snapshot(cls, record, snapshot):
        state, turns = super().read_snapshot(record, snapshot)
        if state.turn_open and isinstance(record, Path) and launch_ended(record):
            # A reader of its own, so that the cached reader keeps the record's own reading for a later one.
            reader = cls(record, snapshot)
            reader.cut()
            state, turns = reader.state(), list(reader.turns)
        if (snapshot.rows and snapshot.rows[-1][0] > snapshot.complete_lines
                and snapshot.rows[-1][1].get('type') == 'compaction'):
            state = replace(state, pending_tools=None)
        return state, turns

    def take(self, entry: dict) -> None:
        kind, at = entry.get("type"), when(entry.get("timestamp"))
        if kind == "model_change":
            self.model = entry.get("modelId") or self.model
        elif kind == "thinking_level_change":
            self.effort = entry.get("thinkingLevel") or self.effort
        elif kind == "message" and isinstance(entry.get("message"), dict):
            self.message(entry["message"], at)
        elif kind != 'session' and not (kind == 'compaction' and _compaction_row(entry)):
            self.tools_known = False
        self.native_entry(entry)

    def native_entry(self, entry: dict) -> None:
        """Recovery identity needs the native parent chain; ordinary accounting remains unchanged."""
        kind, ident = entry.get('type'), entry.get('id')
        if kind == 'session':
            self.native_session = ident if isinstance(ident, str) and ident else None
            self.native_chain = self.native_session is not None
            self.native_parent = self.native_user = self.native_work = self.native_capped = None
            self.native_trace = ''
            return
        if (not isinstance(ident, str) or not ident or not getattr(self, 'native_session', None)
                or entry.get('parentId') != getattr(self, 'native_parent', None)):
            self.native_chain = False
            self.native_user = self.native_work = self.native_capped = None
        self.native_parent = ident
        if kind == 'model_change':
            self.native_capped = None
        elif kind == 'message':
            self.native_capped = None
            message = entry.get('message')
            if not isinstance(message, dict) or not isinstance(ident, str):
                return
            if message.get('role') == 'user':
                self.native_user = ident
                self.native_work = entry if self.message_known(message) and _text(message.get('content')).strip() else None
                self.native_trace = hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()
                return
            work = getattr(self, 'native_work', None)
            self.native_trace = hashlib.sha256(json.dumps([getattr(self, 'native_trace', ''), entry], sort_keys=True).encode()).hexdigest()
            content, usage = message.get('content'), message.get('usage')
            if (not work or not self.native_chain or message.get('role') != 'assistant' or message.get('stopReason') != 'length'
                    or message.get('errorMessage')
                    or not self.message_known(message) or not _tokens(message).complete or self.open is not None
                    or not isinstance(content, list) or not content
                    or not all(b.get('type') == 'thinking' and isinstance(b.get('thinking'), str) for b in content)
                    or not any(b['thinking'] for b in content)
                    or not isinstance(usage, dict) or type(usage.get('output')) is not int or usage['output'] <= 0
                    or not all(isinstance(message.get(k), str) and message[k] for k in ('model', 'provider'))):
                return
            # Pi retains total output, not a separate reasoning count. Thinking-only
            # native content is the evidence; never invent a reasoning measurement.
            signature = hashlib.sha256(json.dumps([self.native_session, work['id'], self.native_trace], sort_keys=True).encode()).hexdigest()
            self.native_capped = CappedGeneration(self.native_session, work['id'], self.native_user, ident,
                message['model'], message['provider'], None, usage['output'], signature)
        elif kind not in ('thinking_level_change',):
            self.native_capped = None

    def state(self) -> State:
        state = super().state()
        work = getattr(self, 'native_work', None)
        chain = getattr(self, 'native_chain', False)
        return replace(state, conversation=getattr(self, 'native_session', None),
                       input_id=getattr(self, 'native_user', None) if chain else None, work_id=work['id'] if work and chain else None,
                       capped=getattr(self, 'native_capped', None)
                       if state.turn_open is False and state.pending_tools == () and not state.queued else None)

    def message(self, message: dict, at: datetime | None) -> None:
        role = message.get("role")
        if not self.message_known(message):
            self.tools_known = False
        if role == "user":
            text = _text(message.get("content"))
            self.last_input, self.last_input_at = text, at
            if self.open is not None:
                self.consumed.append(text)  # steered into the running turn after its current tool call
            else:
                self.start_turn({"started": at, "input": text, "tokens": Tokens(), "tools": [], "reply": "", "costs": [],
                                 "inputs": [], "activity": False})
            self.open["inputs"].append(Input(text, at))
        elif role == "assistant":
            if self.open is None:
                self.start_turn({"started": at, "input": "", "tokens": Tokens(), "tools": [], "reply": "", "costs": [],
                                 "inputs": [], "activity": False})
            step_model = model_name(message.get("model"))
            self.model = step_model or self.model
            tokens = _tokens(message)
            self.open["tokens"] = self.open["tokens"] + tokens
            usage = message.get("usage")
            costs = usage.get("cost") if isinstance(usage, dict) else None
            cost = costs.get("total") if isinstance(costs, dict) else None
            self.open["costs"].append(CostStep(step_model, tokens, amount(cost)))
            self.open["last_step"] = at
            for block in message.get("content") or []:
                if block.get("type") in ("thinking", "toolCall") or block.get("type") == "text" and block.get("text"):
                    self.open["activity"] = True  # model output; pi records no proof of its absence
                if block.get("type") == "text" and block.get("text"):
                    self.open["reply"] = block["text"]
                elif block.get("type") == "toolCall":
                    name = " ".join(str(block.get("name") or "").split()) or "?"  # a malformed call may carry a name with line breaks
                    if block.get('id') in self.live_tools:
                        self.tools_known = False
                    self.open_call(block.get('id'), name, at)
            if message.get("stopReason") in ENDS_TURN:
                if message.get("stopReason") == "error":  # law: record-first
                    from .failures import words
                    from .types import TurnFailure
                    self.open["failure"] = TurnFailure(words(message.get("errorMessage")), at, tuple(self.open["inputs"]),
                                                       True if self.open["activity"] else None, "program error")
                self.open["end_reason"] = message["stopReason"]
                self.end(at)
        elif role == "toolResult":
            self.close_tool(message.get("toolCallId"), at, bool(message.get("isError")), message.get("toolName") or "?")

    def message_known(self, message: dict) -> bool:
        """Native role/content boundaries must account for every possible call."""
        role, content = message.get('role'), message.get('content')
        if role in ('branchSummary', 'compactionSummary'):
            return isinstance(message.get('summary'), str) and 'content' not in message
        if role == 'bashExecution':
            return ('content' not in message and isinstance(message.get('command'), str) and isinstance(message.get('output'), str)
                    and (type(message.get('exitCode')) is int or message.get('cancelled') is True))
        if role not in ('user', 'assistant', 'toolResult', 'custom'):
            return False
        if role == 'toolResult':
            call = message.get('toolCallId')
            if (not isinstance(call, str) or call not in self.live_tools
                    or message.get('toolName') != self.live_tools[call] or not isinstance(message.get('isError'), bool)):
                return False
        if role in ('user', 'custom') and isinstance(content, str):
            return True
        if not isinstance(content, list):
            return False
        for block in content:
            if not isinstance(block, dict):
                return False
            kind = block.get('type')
            if kind == 'text':
                known = isinstance(block.get('text'), str)
            elif role == 'assistant' and kind == 'thinking':
                known = isinstance(block.get('thinking'), str)
            elif role == 'assistant' and kind == 'toolCall':
                call, name = block.get('id'), block.get('name')
                known = (isinstance(call, str) and bool(call) and call not in self.live_tools
                         and isinstance(name, str) and bool(name) and isinstance(block.get('arguments'), dict))
            elif role != 'assistant' and kind == 'image':
                known = isinstance(block.get('data'), str) and isinstance(block.get('mimeType'), str)
            else:
                known = False
            if not known:
                return False
        return True

    def end(self, at: datetime | None) -> None:
        turn = self.close_turn()
        self.record_turn(costed(Turn(len(self.turns), turn["started"] or at, at or turn["started"], turn["input"], turn["reply"],
                                    self.model, turn["tokens"], tuple(turn["tools"]), cost_steps=tuple(turn["costs"]),
                                    failure=turn.get("failure"), inputs=tuple(turn["inputs"]),
                                    end_reason=turn.get("end_reason"))), at)

    def cut(self) -> None:
        """Its launch has ended with this turn open, so it was cut and will never be written again: it closes untimed
        at its last step, with its measured steps, tokens and costs, each tool it left running noted as such, and
        nothing pending. A turn with no step yet stays open, as OpenCode's does."""
        if self.open is None or "last_step" not in self.open:
            return
        from .base import ToolCall
        for name, _ in self.pending.values():
            self.open["tools"].append(ToolCall(name, None, None))  # left running when its launch ended: its end unknown
        self.live_tools.clear()
        self.live_tool_starts.clear()
        turn = self.close_turn()
        at = turn["last_step"] or turn["started"]
        self.record_turn(costed(Turn(len(self.turns), turn["started"] or at, at, turn["input"], turn["reply"], self.model,
                                    turn["tokens"], tuple(turn["tools"]), cost_steps=tuple(turn["costs"]),
                                    inputs=tuple(turn["inputs"]), timed=False)), None)


state = _Reader.read_state
turns = _Reader.read_turns
read = _Reader.read


def _input_relevant(row):
    return (row.get('type') not in ('session', 'model_change', 'thinking_level_change')
            and not (row.get('type') == 'compaction' and _compaction_row(row)))


@input_reader(_input_relevant)
def inputs(record: Path) -> list[Input]:
    """Every user message with the time it entered the record, for the delivery receipt."""
    found = Inputs()
    opened = False
    for line, entry in input_rows(record, _Reader.kinds, _Reader.label, found):
        try:
            kind = entry.get('type')
            if kind == 'compaction' and _compaction_row(entry):
                continue
            if kind not in _Reader.kinds:
                found.skip(line, 'unknown', 'unknown row kind')
                continue
            if kind != 'message':
                continue
            message = entry.get("message")
            if not isinstance(message, dict):
                raise TypeError
            if message.get("role") == "user":
                content = message.get('content')
                if not isinstance(content, (str, list)):
                    raise TypeError
                if isinstance(content, list) and (not content or any(
                        not isinstance(block, dict) or block.get('type') != 'text'
                        or not isinstance(block.get('text'), str) for block in content)):
                    found.skip(line, 'message', 'unknown user-message content')
                    if not any(isinstance(block, dict) and block.get('type') == 'text'
                               and isinstance(block.get('text'), str) for block in content):
                        continue
                at = when(entry.get("timestamp"))
                found.append(Input(_text(message.get("content")), at, "consumed" if opened else "accepted", at if opened else None))
                opened = True
            elif message.get("role") == "assistant":
                opened = message.get("stopReason") not in ENDS_TURN
            elif message.get('role') not in ('toolResult', 'branchSummary', 'compactionSummary'):
                found.skip(line, 'message', 'unknown message role')
        except (TypeError, ValueError, AttributeError, KeyError):
            found.skip(line, 'message', 'malformed input row')
    return found


# -- the adapter --------------------------------------------------------------------------------

RULE = re.compile(r"^─{20,}\s*$")
# 0.79.0 also draws "(provider) model • medium" when multiple providers are
# available and the prefix fits. A model without reasoning has no thinking level.
FOOTER = re.compile(r"^(?P<stats>\S.*?)\s{2,}(?:\([^()\s]+\) )?(?P<model>\S+)(?: • (?P<level>thinking off|\w+))?\s*$")
# At 80 columns the 0.79.0 xAI footer can lose the last cell of "thinking off"
# once the usage section fills, while both editor rules still span the pane.
CLIPPED_OFF_FOOTER = re.compile(
    r"↑[0-9]+ ↓[0-9]+ R[0-9]+(?:\.[0-9]+)?k? CH[0-9]+\.[0-9]+% "
    r"[0-9]+\.[0-9]+%/[0-9]+k \(auto\) {2,}(?P<model>grok-4\.20-0309-non-reasoning) • thinking of"
)
RULE_80 = "─" * 80
WORKING = re.compile(r"^\s*[⠁-⣿] Working\.\.\.", re.M)
QUEUED_HINT = "Alt+Up to edit all queued messages"
STEERING = re.compile(r"^\s*Steering: (.*?)\s*$", re.M)


class PiHarness(EndpointHarness):
    """Read Pi session JSONL and the terminal editor."""

    box_geometry_known = True  # box_columns below declares the established text area
    minimum_pane_heights = {"0.79.0": 5}  # OPEN native idle pane, 52/80/120/256 columns, both resize directions

    def check_tool_evidence(self, record: Path) -> dict:
        from .check_evidence import json_tools, pi_tools
        return json_tools(record, pi_tools)

    human_tool_policy = ("The enumerated core tools have no question tool. Extension discovery is off, "
                         "and the OpenAI output hook adds no tools. Custom tools are not classified.")

    efforts = EffortCapability("levels", THINKING_LEVELS, ("0.74.2",), complete=True)
    defaults = DefaultMetadata(model_default=False, effort=SuggestedValue(None, "program-default"))

    name = "pi"
    display_name = 'pi'
    program = "pi"
    home_variable = "PI_CODING_AGENT_DIR"
    keys = Keys(submit="Enter", send_now=None, interrupt="Escape")

    def check_defaults(self, seat: SeatSpec, ship_home: Path | None = None) -> None:
        super().check_defaults(seat, ship_home)
        if seat.effort_mode != 'program-default':
            return
        template = ship_template(ship_home, 'models.json')
        scope = 'template models.json'
        for key in template:
            if key not in ('$schema', 'providers'):
                reject_unclassified_overrides(seat, {key: None}, scope=scope)
        providers = template.get('providers', {})
        if not isinstance(providers, dict):
            raise ShipError(f'seat {seat.name}: {scope}.providers is unclassified for program-default; '
                            'remove that override or choose an explicit named value')
        for name, provider in providers.items():
            if name not in seat.pools:
                continue  # --provider binds selection; unrelated model definitions stay intact
            where = f'{scope}.providers.{name}'
            if not isinstance(provider, dict):
                raise ShipError(f'seat {seat.name}: {where} is unclassified for program-default; '
                                'remove that override or choose an explicit named value')
            for key in provider:
                if key not in ('baseUrl', 'api', 'apiKey', 'models'):
                    reject_unclassified_overrides(seat, {key: None}, scope=where)
            models = provider.get('models', [])
            if not isinstance(models, list):
                raise ShipError(f'seat {seat.name}: {where}.models is unclassified for program-default; '
                                'remove that override or choose an explicit named value')
            for index, model in enumerate(models):
                selected = f'{where}.models[{index}]'
                if not isinstance(model, dict) or seat.model is None:
                    raise ShipError(f'seat {seat.name}: {selected} has no explicit selected model for program-default; '
                                    'remove that override or choose an explicit named value')
                if model.get('id') != seat.model:
                    continue
                reject_default_overrides(seat, model, scope=selected, model_keys=(), effort_keys=('reasoning',))
                for key in model:
                    if key not in ('id', 'name', 'input', 'contextWindow', 'maxTokens', 'cost'):
                        reject_unclassified_overrides(seat, {key: None}, scope=selected)
                cost = model.get('cost', {})
                if not isinstance(cost, dict):
                    reject_unclassified_overrides(seat, {'cost': None}, scope=selected)
                for key in cost:
                    if key not in ('input', 'output', 'cacheRead', 'cacheWrite'):
                        reject_unclassified_overrides(seat, {key: None}, scope=f'{selected}.cost')

    def build_home(self, home: Path, seat: SeatSpec, pool: Pool, ship_home: Path | None) -> Credential | None:
        self.check_defaults(seat, ship_home)
        agent, sessions = home / "agent", home / "sessions"
        seat_files.make_folder(agent)  # never through a link its jail planted
        seat_files.make_folder(sessions)
        choice, secret = self.endpoint_home(home, seat, pool)
        model = {"id": choice.model, "name": choice.model, "input": ["text"],
                 "contextWindow": choice.context, "maxTokens": choice.output}
        if seat.effort_mode != "program-default":
            model["reasoning"] = seat.effort is not None
        if not seat.human_session and choice.model == 'qwen3.8-27b' and seat.effort in THINKING_LEVELS:
            model['compat'] = {'thinkingFormat': 'qwen-chat-template'}
        provider = {"baseUrl": pool.endpoint, "api": "openai-completions",
                    "apiKey": endpoint.KEY_VARIABLE if secret else "local",  # a variable's name, read by the program; never the key
                    "models": [model]}
        models = {"$schema": "https://pi.dev/schemas/models.schema.json", "providers": {pool.name: provider}}
        layered = ship_template(ship_home, "models.json")
        custom = layered.get("providers", {}).get(pool.name, {}).get("models", [])
        matching = [m for m in custom if isinstance(m, dict) and m.get("id") == choice.model]
        if len(matching) > 1:
            raise ShipError(f"pool {pool.name}: template repeats the selected model; price target is ambiguous")
        if matching:
            # The generic list merge appends. Merge this known model identity instead
            # of generating two conflicting definitions for the program to choose from.
            provider["models"] = []
            matching[0].update(merged(model, matching[0]))
        models = merged(models, layered)
        provider = models["providers"][pool.name]
        if (provider.get("api") == "openai-completions" and endpoint.openai_responses(pool.endpoint)
                and endpoint.openai_responses(provider.get("baseUrl"))):
            provider["api"] = "openai-responses"
        price = Price.parse(pool.price) if pool.price is not None else None
        selected = [m for m in provider["models"] if m["id"] == choice.model]
        if len(selected) != 1:
            raise ShipError(f"pool {pool.name}: generated model does not identify one price target")
        model = selected[0]
        reject_default_overrides(seat, model, scope=f"selected model in template models.json.providers.{pool.name}",
                                 model_keys=(), effort_keys=("reasoning",))
        if (pool.provider == "google" and endpoint.google_openai(pool.endpoint)
                and endpoint.google_openai(model.get("baseUrl", provider.get("baseUrl")))
                and model.get("api", provider.get("api")) == "openai-completions"):
            # Google's Chat Completions examples use system, and the endpoint rejects store.
            model.setdefault("compat", {}).update(supportsDeveloperRole=False, supportsStore=False)
        if pool.output is not None:
            model['maxTokens'] = pool.output
        model.pop("cost", None)  # omission is supported; null rates are not
        if price:
            model["cost"] = {"input": price.input, "output": price.output,
                             "cacheRead": price.cache_read, "cacheWrite": price.cache_write}
        settings = merged(SETTINGS, ship_template(ship_home, "settings.json"))
        settings = merged(settings, seat.settings)
        durable.replace_file(agent / "models.json", json.dumps(models, indent=2) + "\n")
        durable.replace_file(agent / "settings.json", json.dumps(settings, indent=2) + "\n")
        return self.endpoint_ready(home, choice, secret, price)

    def launch_with_record(self, seat: SeatSpec, pool: Pool, home: Path, brief: Path, resume: str,
                           first: str | None, record: Path) -> Launch:
        try:
            with Opened(record, 0):
                pass  # the proved path must still open as the seat's regular record
        except OSError:
            raise ShipError(f'{seat.name}: its proved conversation is unavailable. '
                            f'Use qd fresh {seat.name} to start a new conversation carrying its handoff.') from None
        return self.launch(seat, pool, home, brief, resume, first, resume_record=record)

    def launch(self, seat: SeatSpec, pool: Pool, home: Path, brief: Path, resume: str | None, first: str | None,
               *, resume_record: Path | None = None) -> Launch:
        choice = endpoint.Choice.read(home)
        argv = [self.program, "--provider", pool.name, "--model", choice.model, "--session-dir", str(home / "sessions"),
                "--append-system-prompt", str(brief), "--no-context-files",  # the brief, and no instruction file of the user's
                "--no-extensions", "--no-skills", "--no-prompt-templates",
                "--no-approve"]  # 0.79.0 ignores project-local files without a first-launch trust question
        if seat.effort:
            argv += ["--thinking", seat.effort]
        if resume:
            record = resume_record
            if record is None:
                record = next((r for r in reversed(records(home)) if session_id(r) == resume), None)
            if record is None:
                raise ShipError(f"{seat.name}: no record in {home} holds the session {resume}; qd fresh {seat.name} starts a new one")
            from ..record_location import retired
            if retired(record):
                raise ShipError(f"{seat.name}: conversation is retired; use qd fresh {seat.name}")
            argv += ["--session", str(record)]
        env = {self.home_variable: str(home / "agent"), "PI_SKIP_VERSION_CHECK": "1", **endpoint.key(pool)}
        if endpoint.openai_responses(pool.endpoint):
            argv += ["--extension", str(Path(__file__).with_name("pi_responses.ts"))]
            env.update(QD_PI_RESPONSES_PROVIDER=pool.name, QD_PI_RESPONSES_BASE_URL=endpoint.OPENAI_RESPONSES_BASE)
        if first:
            argv.append(first)
        from .pi_thinking import launch_evidence
        return Launch(tuple(argv), env, choice.price, launch_evidence(home, seat, pool, choice))

    def thinking_recovery(self, home, evidence, version, seat, pool):
        from .pi_thinking import client
        return client(home, evidence, version, seat, pool)

    inputs = staticmethod(inputs)
    retirement_process_names = ('pi', 'node', 'bun')

    def stamp_extra(self, record: Path):
        """What besides its bytes decides a record's reading: whether its launch has ended (ledger.record_stamp)."""
        return ['later-launch', launch_ended(record)]

    def retirement_bundle(self, home: Path, record: Path, kept: Path):
        if record.parent != home / 'sessions' or record.suffix != '.jsonl' or record.is_symlink():
            return None
        return record, kept, [record]

    records = staticmethod(records)
    first_timestamp = staticmethod(first_timestamp)
    session_id = staticmethod(session_id)
    reader = staticmethod(read)

    def screen(self, text: str) -> Screen:
        """0.74.2/0.79.0: two box rules above the directory and model/optional-provider footer."""
        if not text.strip():
            return Screen(text, box=None, starting=True)
        lines = text.rstrip("\n").split("\n")
        working = bool(WORKING.search(text))
        queued = 1 if QUEUED_HINT in text else 0
        echoes = tuple(m.group(1) for m in STEERING.finditer(text))
        footer_at = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].strip()), None)  # the program draws top-down: rows below it stay blank
        footer_line = lines[footer_at] if footer_at is not None else ""
        footer = FOOTER.match(footer_line)
        clipped = CLIPPED_OFF_FOOTER.fullmatch(footer_line) if footer is None else None
        if clipped is not None and _cells(footer_line) != 80:
            clipped = None
        shows = {}
        if footer:
            shows = {"model": footer.group("model"),
                     **({"effort": footer.group("level").removeprefix('thinking ')} if footer.group("level") else {})}
        interruption = self.recognize(text)
        if (footer is None and clipped is None) or interruption is not None:
            unknown = None if interruption else "pi footer not found"
            return Screen(text, box=None, working=working, interruption=interruption, unknown=unknown, queued=queued, shows=shows, echoes=echoes)
        bottom = next((i for i in range(footer_at - 1, -1, -1) if RULE.match(lines[i])), None)
        if bottom is None or footer_at - bottom > 3:  # the working directory sits between the bottom rule and the footer
            return Screen(text, box=None, working=working, unknown="pi input box not found", queued=queued, shows=shows, echoes=echoes)
        top = next((i for i in range(bottom - 1, -1, -1) if RULE.match(lines[i])), None)
        if top is None:
            return Screen(text, box=None, working=working, unknown="pi input box not found", queued=queued, shows=shows, echoes=echoes)
        if clipped is not None:
            if footer_at - bottom != 2 or lines[top] != RULE_80 or lines[bottom] != RULE_80:
                return Screen(text, box=None, working=working, unknown="pi footer not found", queued=queued, echoes=echoes)
            shows = {"model": clipped.group("model")}
        body = lines[top + 1:bottom]
        body = trimmed(body)
        return Screen(text, box="\n".join(body), working=working, queued=queued, shows=shows, echoes=echoes)

    def human_wake_box_margin(self, pane_width: int) -> int:
        return 1  # measured final column kept clear

    def box_columns(self, pane_width: int) -> int | None:
        return pane_width - 1  # the box spans the pane from the first column; the last column stays clear

    def missing_box_known(self, screen: Screen) -> bool:
        return screen.unknown == 'pi input box not found' and bool(screen.shows.get('model'))

    def minimum_pane_rows(self, text: str, pane_width: int, version: str | None) -> int | None:
        """0.79.0: four chrome rows; the editor shows max(5, floor(0.3 * pane rows)) text rows.

        A final LF needs its cursor row too. Native pastes over ten lines or
        1000 UTF-16 code units fold to one marker; unknown wrapped glyphs stay unknown.
        This is display metadata only, never a substitute for reading the box.
        """
        if version != '0.79.0' or type(pane_width) is not int or pane_width < 52:
            return None
        lines = text.split('\n')
        if any(_cells(line) is None for line in lines):
            return None
        if len(lines) > 10 or sum(2 if ord(char) > 0xffff else 1 for char in text) > 1000:
            return 5
        rows = 0
        for line in lines:
            wrapped = _pi_wrap(line, self.box_columns(pane_width))
            if wrapped is None:
                return None
            rows += len(wrapped)
        return max(rows + 4, (10 * rows + 2) // 3 if rows > 5 else 5)

    def box(self, text: str) -> Box:
        """Probed on 0.74.2: a short paste shows whole; a long one folds to its count of lines, or of characters when few."""
        return Box(text, folded=rf"\[paste #\d+ (?:\+{text.count(chr(10)) + 1} lines|{len(text)} chars)\]", pi_word_wrap=True)

    def controls(self) -> tuple[Control, ...]:
        return (
            Control("model", launch="--model <value>", session=None, confirmed_by="record"),
            Control("effort", launch="--thinking <value>", session=None, confirmed_by="record"),
        )

    def interruptions(self) -> tuple[Interruption, ...]:
        return (Interruption("model picker", r"Only showing models from configured providers", key="Escape"),)
