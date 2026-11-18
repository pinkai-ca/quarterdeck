"""Codex: the rollout in ``sessions/YYYY/MM/DD/rollout-<time>-<id>.jsonl`` under its home.

Every line is ``{"timestamp", "type", "payload"}``. A turn opens with the event
``task_started`` and closes with ``task_complete`` (or ``turn_aborted``); the prompt is a
``response_item`` message from the user; tool calls are ``function_call`` and
``custom_tool_call`` items closed by their ``_output`` items; a ``token_count`` event
follows each model response with the usage of that response and the running total.
"""

from __future__ import annotations

from .. import seat_files
from .base import SetupMetadata
from .capabilities import DefaultMetadata, EffortCapability, SuggestedValue

import copy
import json
import re
import tomllib
from datetime import date, datetime, time, timezone
from pathlib import Path

from .json_record import JsonLinesReader, input_reader, input_rows, records_matching
from .types import BoxScroll, Inputs, PasteClearRule, _cells
from . import codex_usage

from .. import durable
from ..ship import Pool, SeatSpec, literal
from .base import (Box, ship_template, trimmed, Input, Control, Credential, Harness, Interruption, Keys, Launch, RecordError, Screen, State, Tokens,
                   ToolCall, Turn, credential, entries, key_environment, program_version, when)

ENDS_TURN = {"task_complete", "turn_aborted"}
CALLS = {"function_call", "custom_tool_call"}
OUTPUTS = {"function_call_output", "custom_tool_call_output"}
INPUT_IGNORED = ('token_usage_record', 'world_state')


class EditorBox(Box):
    """0.166.0 inserts an editor row when a logical line ends at the right edge."""

    def wrapped_rows(self, columns, slack=0):
        rows = []
        lines = self.text.rstrip('\n').split('\n')
        for index, line in enumerate(lines):
            wrapped = Box(line, textwrap_ascii=True).wrapped_rows(columns, slack)
            if wrapped is None:
                return None
            rows.extend(wrapped)
            if index < len(lines) - 1 and _cells(wrapped[-1]) == columns:
                rows.append('')  # the same measured editor row the whole comparison requires
        return rows if self.matches('\n'.join(rows), columns, slack) else None

    def matches(self,shown,columns=None,slack=0):
        lines=self.text.rstrip('\n').split('\n')
        if shown is None or not columns or columns<1:
            return super().matches(shown,columns,slack)
        if self.folded and re.fullmatch(self.folded,shown.strip()):
            return True
        rows=shown.rstrip('\n').split('\n');at=0
        for index,line in enumerate(lines):
            start=at
            while at<len(rows):
                at+=1
                if Box(line,textwrap_ascii=True).matches('\n'.join(rows[start:at]),columns,slack):
                    break
                if not rows[at-1].strip(' '):
                    return False
            else:
                return False
            from .types import _cells
            if index<len(lines)-1 and _cells(rows[at-1].rstrip(' '))==columns:
                if at>=len(rows) or rows[at].strip(' '):
                    return False
                at+=1  # this one row is derived from the payload/geometry, never discarded generally
        return at==len(rows)


def compactions(record: Path):
    from .compactions import History, identity, read

    class Interpret:
        """The compaction history, fed rows in turn and kept with the record, so an append feeds only its new
        rows (compactions.read); a row it refuses refuses the history, which then stays refused."""

        def __init__(self):
            self.history, self.checkpoints, self.native_turn, self.index = History(), set(), None, 0

        def feed(self, rows):
            for row in rows:
                index, self.index = self.index, self.index + 1
                kind, payload = row.get('type'), row.get('payload')
                if not isinstance(payload, dict):
                    raise ValueError('unknown native history')
                sub = payload.get('type')
                if kind == 'session_meta':
                    if index != 0 or any(payload.get(key) is not None for key in
                                         ('forked_from_id', 'history_base', 'parent_thread_id', 'subagent_history_start_ordinal')):
                        raise ValueError('native origin not complete')
                    self.history.conversation = payload['id']
                    self.history.origin = when(row.get('timestamp'))
                    if not isinstance(self.history.conversation, str) or payload.get('session_id', self.history.conversation) != self.history.conversation:
                        raise ValueError('native identity missing')
                elif self.history.conversation is None:
                    raise ValueError('native header missing')
                elif kind == 'compacted':
                    key = identity(row)
                    if not isinstance(payload.get('message'), str) or key in self.checkpoints:
                        raise ValueError('ambiguous checkpoint')
                    self.checkpoints.add(key)
                    self.history.count += 1
                    self.history.work(row)
                elif kind == 'event_msg' and sub == 'task_started':
                    self.history.work(row, turn=True)
                    self.native_turn = payload.get('turn_id')
                elif kind == 'event_msg' and sub == 'task_complete':
                    self.history.complete = isinstance(self.native_turn, str) and bool(self.native_turn) and payload.get('turn_id') == self.native_turn
                elif kind == 'event_msg' and sub in ENDS_TURN:
                    self.history.work(row)  # an abort invalidates readiness; it is not the promised completion
                elif kind == 'response_item' and sub in CALLS:
                    self.history.work(row, call=payload.get('call_id'))
                elif kind == 'response_item' and sub == 'message' and payload.get('role') == 'user':
                    self.history.work(row)
                elif kind not in ('response_item', 'event_msg', 'turn_context', *INPUT_IGNORED):
                    raise ValueError('unknown native history')

    return read(record, Interpret, _Reader)


def records(home: Path) -> list[Path]:
    return records_matching(home, "sessions/*/*/*/rollout-*.jsonl")


def first_timestamp(record: Path) -> datetime | None:
    return next((when(e["timestamp"]) for e in entries(record) if e.get("timestamp")), None)


def session_id(record: Path) -> str | None:
    for entry in entries(record):
        if entry.get("type") == "session_meta":
            return entry.get("payload", {}).get("id")
    return None


def usage_tokens(usage: dict) -> Tokens:
    measured = Tokens.from_usage(usage, ("cached_input_tokens", "input_tokens", "cache_write_input_tokens", "output_tokens"),
                                 total="total_tokens")
    if not measured.complete:
        return Tokens.unknown()
    uncached = measured.read_uncached - measured.read_cached if "input_tokens" in usage else 0
    if uncached < 0:
        return Tokens.unknown()
    return Tokens.measured(read_cached=measured.read_cached,
                           read_uncached=uncached,
                           cache_written=measured.cache_written, written=measured.written)


def _text(content: list | str | None) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(block.get("text", "") for block in content or [] if isinstance(block, dict))


EXIT_CODE = re.compile(r"[Ee]xit code:? (\d+)")


def _failed(output: str | list | None) -> bool:
    """A shell call's output carries its exit code, as JSON metadata or as text blocks that state it."""
    text = _text(output) if isinstance(output, list) else output
    if not isinstance(text, str):
        return False
    try:
        return bool(json.loads(text).get("metadata", {}).get("exit_code", 0))
    except (json.JSONDecodeError, AttributeError):
        match = EXIT_CODE.search(text)
        return bool(match and int(match.group(1)))


PROCESS_HEADER = re.compile(
    r'\A(?:Chunk ID: [^\n]+\n)?Wall time: [0-9]+(?:\.[0-9]+)? seconds\n'
    r'(?:Process exited with code (-?\d+)\n)?'
    r'(?:Process running with session ID (\d+)\n)?'
    r'(?:Original token count: \d+\n)?Output:(?:\n|\Z)')


def process_result(output) -> tuple[bool, str | None]:
    """Explicit terminal code or a live native handle; never search command stdout for proof."""
    if isinstance(output, list):
        if len(output) != 1 or not isinstance(output[0], dict) or output[0].get('type') not in ('input_text', 'output_text', 'text'):
            return False, None
        output = output[0].get('text')
    if not isinstance(output, str):
        return False, None
    try:
        value = json.loads(output)
    except ValueError:
        header = PROCESS_HEADER.match(output)
        if header is None:
            return False, None
        code, session = header.groups()
        return (code is not None and session is None), session if code is None else None
    if not isinstance(value, dict):
        return False, None
    meta = value.get('metadata') if isinstance(value.get('metadata'), dict) else {}
    code, session = value.get('exit_code', meta.get('exit_code')), value.get('session_id', meta.get('session_id'))
    if type(code) is int and session is None:
        return True, None
    if code is None and (type(session) is int and session > 0 or isinstance(session, str) and session.isdecimal()):
        return False, str(session)
    return False, None


def _input_relevant(row):
    kind, payload = row.get('type'), row.get('payload', {})
    if kind in INPUT_IGNORED or kind == 'compacted' and _compacted_row(row):
        return False
    if kind not in _Reader.kinds:
        return True
    if not isinstance(payload, dict):
        return kind in ('event_msg', 'response_item')
    return (kind == 'event_msg' and payload.get('type') in {'task_started', *ENDS_TURN}
            or kind == 'response_item' and payload.get('role') == 'user')


@input_reader(_input_relevant)
def inputs(record: Path) -> list[Input]:
    found = Inputs()
    seen = False
    for line, row in input_rows(record, _Reader.kinds, _Reader.label, found):
        kind = row.get('type')
        if kind in INPUT_IGNORED or kind == 'compacted' and _compacted_row(row):
            continue  # normal metadata, not a program-format marker or receipt candidate
        if kind not in _Reader.kinds:
            found.skip(line, 'unknown', 'unknown row kind')
            continue
        try:
            payload = row.get("payload", {})
            if row.get("type") == "event_msg" and payload.get("type") in {"task_started", *ENDS_TURN}:
                seen = False
            if row.get("type") != "response_item" or payload.get("role") != "user":
                continue
            if payload.get('type') != 'message':
                found.skip(line, 'response_item', 'unknown user-message subtype')
                continue
            content = payload.get("content")
            if not isinstance(content, list):
                raise TypeError
            if not any(c.get('type') == 'input_text' for c in content):
                found.skip(line, 'response_item', 'no known input text')
                continue
            text = "\n".join(c["text"] for c in content if c.get("type") == "input_text")
            at = when(row.get("timestamp"))
            found.append(Input(text, at, "consumed" if seen else "accepted", at if seen else None))
            seen = True
        except (TypeError, ValueError, AttributeError, KeyError):
            found.skip(line, kind, 'malformed input row')
    return found


REFUSAL_CODES = {'cyber_policy'}  # law: record-first -- the one native refusal form these records are known to carry


def _failure(error, at, inputs, output):
    """A recorded turn error. Only a known refusal code is a refusal; a message alone is a provider error."""
    from .failures import words
    from .types import TurnFailure
    known = isinstance(error, dict) and isinstance(error.get('message'), str) and bool(error['message'].strip())
    code = error.get('codex_error_info') if isinstance(error, dict) else None
    code = code if isinstance(code, str) and code.isidentifier() and len(code) <= 64 else None
    kind = 'provider refusal' if code in REFUSAL_CODES and known else 'provider error'
    return TurnFailure(words(error.get('message') if isinstance(error, dict) else None), at, inputs,
                       output if known else None, kind, code)


def _shape(value, fields):
    return (isinstance(value, dict) and value.keys() == fields.keys()
            and all(type(value[key]) is kind for key, kind in fields.items()))


def _compacted_row(row):
    """Measured checkpoint envelope; its retained history is not new execution/input."""
    if (not _shape(row, {'ordinal': int, 'payload': dict, 'timestamp': str, 'type': str})
            or row['type'] != 'compacted' or row['ordinal'] < 0 or when(row['timestamp']) is None):
        return False
    payload = row['payload']
    return (_shape(payload, {'compaction_response_id': str, 'first_window_id': str,
                            'latest_token_usage_record': dict, 'message': str,
                            'previous_window_id': str, 'replacement_history': list,
                            'replacement_history_metadata': list, 'resume_metadata': dict,
                            'retained_context': dict, 'window_id': str, 'window_number': int})
            and payload['window_number'] > 0
            and all(payload[key] for key in ('compaction_response_id', 'first_window_id',
                                            'previous_window_id', 'window_id')))


def _completed_extension(item):
    """Only the measured sleep and web completion metadata, never tool results."""
    if item.get('kind') == 'clock.sleep':
        return (_shape(item, {'type': str, 'id': str, 'kind': str, 'durationMs': int})
                and item['durationMs'] >= 0)
    if not _shape(item, {'type': str, 'id': str, 'kind': str, 'query': str,
                         'action': dict, 'results': list}) or item['kind'] != 'web.search':
        return False
    action = item['action']
    fields = {'type': str, 'url': str}
    if action.get('type') == 'findInPage':
        fields['pattern'] = str
    elif action.get('type') != 'openPage':
        return False
    return (_shape(action, fields)
            and all(_shape(result, {'type': str, 'domain': str, 'ref_id': str,
                                    'snippet': str, 'thumbnail_url': str, 'title': str, 'url': str})
                    and result['type'] == 'text_result' for result in item['results']))


def _completed_item(payload):
    """Measured native completion metadata; never a result for a response-item call.

    In particular, this cannot establish completion of an opaque exec wrapper or
    its detached children. Unknown and progress variants remain unknown.
    """
    if not _shape(payload, {'type': str, 'thread_id': str, 'turn_id': str, 'item': dict,
                            'started_at_ms': int, 'completed_at_ms': int}):
        return False
    if (not payload['thread_id'] or not payload['turn_id']
            or not 0 <= payload['started_at_ms'] <= payload['completed_at_ms']):
        return False
    item = payload['item']
    kind = item.get('type')
    if kind == 'Extension':
        return bool(item.get('id')) and _completed_extension(item)
    if kind == 'ContextCompaction':
        return _shape(item, {'type': str, 'id': str}) and bool(item['id'])
    schemas = {
        'UserMessage': {'type': str, 'id': str, 'client_id': str, 'content': list},
        'AgentMessage': {'type': str, 'id': str, 'phase': str, 'content': list},
        'Reasoning': {'type': str, 'id': str, 'raw_content': list, 'summary_text': list},
        'CommandExecution': {'type': str, 'id': str, 'command': list, 'cwd': str, 'duration': dict,
                             'exit_code': int, 'parsed_cmd': list, 'process_id': str, 'source': str,
                             'status': str, 'aggregated_output': str},
        'FileChange': {'type': str, 'id': str, 'changes': dict, 'status': str,
                       'stdout': str, 'stderr': str},
    }
    if not isinstance(kind, str) or kind not in schemas or not _shape(item, schemas[kind]) or not item['id']:
        return False
    if kind in ('UserMessage', 'AgentMessage'):
        fields = {'type': str, 'text': str}
        if kind == 'UserMessage':
            fields['text_elements'] = list
        return all(_shape(block, fields) and block['type'] == ('text' if kind == 'UserMessage' else 'Text')
                   for block in item['content'])
    if kind == 'Reasoning':
        return not item['raw_content'] and not item['summary_text']  # only this measured variant
    if kind == 'CommandExecution':
        duration = item['duration']
        return (item['status'] in ('completed', 'failed') and bool(item['process_id'])
                and bool(item['command']) and all(isinstance(part, str) for part in item['command'])
                and _shape(duration, {'secs': int, 'nanos': int})
                and duration['secs'] >= 0 and 0 <= duration['nanos'] < 1_000_000_000)
    return item['status'] == 'completed'  # FileChange: no unmeasured progress/failure variant


class _Reader(JsonLinesReader):
    kinds = ('event_msg', 'response_item', 'session_meta', 'turn_context')
    label = "Codex"

    @classmethod
    def read_snapshot(cls, record, snapshot):
        state, turns = super().read_snapshot(record, snapshot)
        if snapshot.rows and snapshot.rows[-1][0] > snapshot.complete_lines:
            entry = snapshot.rows[-1][1]
            payload = entry.get('payload')
            if (entry.get('type') == 'compacted'
                    or entry.get('type') == 'event_msg' and isinstance(payload, dict)
                    and payload.get('type') == 'item_completed'):
                from dataclasses import replace
                # This exact metadata row may still be extended by the native writer.
                state = replace(state, pending_tools=None)
        return state, turns

    def __init__(self, record, snapshot=None):
        self.process_inputs = {}
        self.active_processes = {}
        self.orphan_failure = None  # an error ending no turn this record opened
        self.unbound_inputs = []
        super().__init__(record, snapshot)

    def state(self):
        state = super().state()
        if self.orphan_failure is not None and self.open is None:
            from dataclasses import replace
            state = replace(state, failure=self.orphan_failure)
        return state

    def take(self, entry: dict) -> None:
        kind = entry.get('type')
        if kind == 'compacted':
            if not _compacted_row(entry):
                self.tools_known = False
            if self.open is not None:
                self.open['model_output'] = True  # the program worked inside this turn
            return
        if kind in INPUT_IGNORED:
            return  # known non-execution metadata, independent of accounting format markers
        if kind not in ('session_meta', 'turn_context', 'event_msg', 'response_item'):
            self.tools_known = False
            return
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            self.tools_known = False
            return
        kind, sub, at = entry.get("type"), payload.get("type"), when(entry.get("timestamp"))
        if kind == "turn_context":
            self.model = payload.get("model") or self.model
            self.effort = payload.get("effort") or self.effort
        elif kind == "event_msg":
            self.event(sub, payload, at)
        elif kind == "response_item":
            self.item(sub, payload, at)

    def event(self, sub: str | None, payload: dict, at: datetime | None) -> None:
        if sub == 'item_completed':
            if at is None or not _completed_item(payload):
                self.tools_known = False
        elif sub == "task_started":
            self.orphan_failure = None
            self.unbound_inputs = []
            if self.open is not None:
                self.end(at, completed=False)
            self.start_turn({"started": at, "input": "", "tokens": Tokens(), "measurements": 0, "response": False,
                             "tools": [], "reply": "", "inputs": [], "model_output": False})
        elif sub == "token_count" and self.open is not None:
            info = payload.get("info")
            last = info.get("last_token_usage") if isinstance(info, dict) else None
            self.open["tokens"] = self.open["tokens"] + usage_tokens(last)
            if isinstance(last, dict):
                counts = [last.get(k) for k in ('output_tokens', 'reasoning_output_tokens')]
                if any(type(n) is int and n > 0 for n in counts):
                    self.open['model_output'] = True  # the model produced something, shown or not
                elif any(type(n) is not int or n < 0 for n in counts) and self.open['model_output'] is not True:
                    self.open['model_output'] = None  # an unreadable count cannot prove no output
            elif info is not None and self.open['model_output'] is not True:
                self.open['model_output'] = None
            self.open["measurements"] += 1
            self.open["response"] = False
        elif sub in ENDS_TURN and self.open is not None:
            if payload.get("last_agent_message"):
                self.open["reply"] = payload["last_agent_message"]
                self.open["model_output"] = True
            if sub == 'task_complete' and payload.get('error') is not None:
                output = self.open['model_output']
                if output is not True and ('last_agent_message' not in payload or payload['last_agent_message'] is not None
                                           or not self.tools_known or at is None or self.open['started'] is None
                                           or at < self.open['started']):
                    output = None  # missing or odd fields are unknown, never zero output
                self.open['failure'] = _failure(payload['error'], at, tuple(self.open['inputs']), output)
            self.end(at)
        elif sub == 'task_complete' and payload.get('error') is not None:
            self.orphan_failure = _failure(payload['error'], at, tuple(self.unbound_inputs), None)  # no turn opened it
        elif sub in ('agent_message', 'agent_reasoning', 'agent_reasoning_raw_content') and self.open is not None:
            self.open['model_output'] = True
        elif sub not in ('task_started', 'token_count', *ENDS_TURN, 'user_message', 'agent_message',
                         'agent_reasoning', 'agent_reasoning_raw_content', 'context_compacted',
                         'thread_settings_applied', 'thread_goal_updated', 'model_reroute',
                         'model_verification', 'warning', 'deprecation_notice', 'plan_update',
                         'turn_diff', 'entered_review_mode', 'exited_review_mode'):
            self.tools_known = False

    def item(self, sub: str | None, payload: dict, at: datetime | None) -> None:
        if sub == 'message' and (payload.get('role') not in ('user', 'assistant', 'system', 'developer')
                                 or not isinstance(payload.get('content'), (str, list))):
            self.tools_known = False
        if sub == 'message' and isinstance(payload.get('content'), list):
            if any(not isinstance(block, dict) or block.get('type') not in
                   ('input_text', 'output_text', 'input_image', 'input_audio') for block in payload['content']):
                self.tools_known = False
        if sub == "message" and payload.get("role") == "user":
            text = _text(payload.get("content"))
            if text and not text.startswith('<') and self.open is None:
                self.unbound_inputs.append(Input(text, at))
            if text and not text.startswith("<") and self.open is not None:
                self.last_input, self.last_input_at = text, at
                self.open['inputs'].append(Input(text, at))
                if self.open["input"]:
                    self.consumed.append(text)  # a message taken into the running turn
                else:
                    self.open["input"] = text
        elif sub == "message" and payload.get("role") == "assistant" and self.open is not None:
            self.open["reply"] = _text(payload.get("content")) or self.open["reply"]
            self.open["response"] = True
            if _text(payload.get('content')):
                self.open['model_output'] = True
            elif self.open['model_output'] is False:
                self.open['model_output'] = None  # an assistant row without readable text is not proof of none
        elif sub == 'reasoning' and self.open is not None:
            self.open['model_output'] = True  # reasoning is model output, even without final text
        elif sub in CALLS:
            if self.open is not None:
                self.open["response"] = True
                self.open['model_output'] = True
            name = payload.get('name', '?')
            call = payload.get('call_id')
            if payload.get('status') not in (None, 'completed'):
                self.tools_known = False  # a streamed/in-flight call is not an established question wait
            arguments = None
            if sub == 'function_call':
                try:
                    arguments = json.loads(payload.get('arguments'))
                except (TypeError, ValueError):
                    pass
                if not isinstance(arguments, dict):
                    self.tools_known = False
            elif not isinstance(payload.get('input'), str):
                self.tools_known = False
            if isinstance(name, str):
                short = name.rsplit('.', 1)[-1]
                if short in ('spawn_agent', 'exec'):
                    self.tools_known = False  # opaque/delegated execution is not ended by a call's return
                if short in ('exec_command', 'write_stdin') and isinstance(call, str):
                    self.process_inputs[call] = (short, arguments or {})
            if isinstance(call, str) and call in self.live_tools:
                self.tools_known = False
            self.open_call(payload.get('call_id'), name, at)
        elif sub in OUTPUTS:
            call = payload.get('call_id')
            if not isinstance(call, str) or not call or not isinstance(payload.get('output'), (str, list)):
                self.tools_known = False
            elif call not in self.live_tools:
                self.tools_known = False  # an unjoined result cannot certify the full execution set
            if self.open is not None and self.open["response"]:
                self.open["tokens"] += Tokens.unknown()
                self.open["measurements"] += 1
                self.open["response"] = False
            name = self.live_tools.get(call) if isinstance(call, str) else None
            keep = self.process_output(call, payload.get('output')) if isinstance(call, str) else False
            self.close_tool(call, at, _failed(payload.get("output")))
            if keep:
                self.live_tools[call] = name
        elif sub not in ('message', 'reasoning', 'compaction', 'compaction_summary', 'context_compaction',
                         'configuration_update', 'compaction_trigger', 'additional_tools'):
            self.tools_known = False  # a future execution item cannot disappear from exclusivity proof

    def process_output(self, call, output):
        info = self.process_inputs.pop(call, None)
        if info is None:
            return False
        name, arguments = info
        completed, session = process_result(output)
        if name == 'write_stdin':
            original = arguments.get('session_id')
            original = str(original) if type(original) is int or isinstance(original, str) else None
            if original not in self.active_processes or session is not None and session != original:
                self.tools_known = False
            if completed:
                for origin in self.active_processes.pop(original, ()):
                    self.live_tools.pop(origin, None)
        if session is not None:
            self.active_processes.setdefault(session, set()).add(call)
            return True
        if not completed:
            self.tools_known = False
            return True
        return False

    def end(self, at: datetime | None, *, completed: bool = True) -> None:
        turn = self.close_turn()
        self.record_turn(
            Turn(
                index=len(self.turns),
                started=turn["started"] or at,
                ended=at or turn["started"],
                input=turn["input"],
                reply=turn["reply"],
                model=self.model,
                tokens=turn["tokens"] + (Tokens.unknown() if turn["response"] or not turn["measurements"] else Tokens()),
                tools=tuple(turn["tools"]),
                failure=turn.get('failure'),
            ), at if completed else None
        )

state = _Reader.read_state
turns = _Reader.read_turns
read = _Reader.read



# -- the adapter -----------------------------------------------------------------------------

BOX_START = re.compile(r"^› (.*)$")
PLACEHOLDER = "Ask Codex to do anything"
STATUS_LINE = re.compile(r"^\s+(\S+) (low|medium|high|xhigh|max) · ")
WORKING = re.compile(r"• Working \([^)]*esc to interrupt")  # the elapsed time reads 10s, then 1m 05s
# Complete logo captured on 0.166.0, not a Braille character class. Its position
# varies with pane size; a ready composer may still have this logo above it.
STARTUP_LOGO = (
    '⣀⣤⣤⣤⣀⣀',
    '⣠⣶⣿⣿⣿⣿⣿⣿⣿⣿⣦⣤⣤⣤⣤⣤⣤⡀',
    '⢀⣾⣿⣿⠿⠋⠉⠉⢉⣭⣿⣿⣿⣿⣿⠿⣿⣿⣿⣿⣷⣄',
    '⣀⣾⣿⣿⠃ ⣀⣴⣾⣿⣿⡿⠟⠋ ⣀⡀ ⠈⠙⢿⣿⣿⣧',
    '⣀⣶⣿⣿⣿⣿⡇ ⢸⣿⣿⡿⠛⠉⢀⣠⣴⣿⣿⣿⣷⣦⣀⠈⢻⣿⣿⡇',
    '⣰⣿⣿⡿⢿⣿⣿⡇ ⢸⣿⣿⢀⣤⣶⣿⣿⣿⠟⠛⠻⣿⣿⣿⣿⣮⣿⣿⡷',
    '⢰⣿⣿⡟⠁⢸⣿⣿⡇ ⢸⣿⣿⣿⣿⠿⠿⣿⣿⣿⣦⣄⡀⠈⠛⠿⣿⣿⣿⣧⡀',
    '⣾⣿⣿⠃ ⢸⣿⣿⡇ ⢸⣿⣿⠋⠁  ⠈⠙⢻⣿⣿⣿⣶⣤⡀ ⠹⢿⣿⣷⡄',
    '⢸⣿⣿⣇ ⠸⣿⣿⣷⣦⣼⣿⣿      ⢸⣿⣿⠻⢿⣿⣿⡇ ⠘⣿⣿⣿',
    '⠈⢿⣿⣿⣦⡀⠈⠛⠿⣿⣿⣿⣿⣄⡀  ⢀⣠⣼⣿⣿  ⣿⣿⡇  ⣿⣿⣿⠇',
    '⢻⣿⣿⣿⣷⣦⣀⠈⠙⠻⣿⣿⣿⣶⣶⣿⣿⣿⣿⣿  ⣿⣿⡇ ⣠⣿⣿⣿',
    '⢸⣿⣿⡿⢿⣿⣿⣿⣦⣠⣴⣿⣿⣿⡿⠟⠋⢸⣿⣿  ⣿⣿⣷⣴⣿⣿⣿⠃',
    '⠘⣿⣿⣷ ⠈⠛⢿⣿⣿⣿⠿⠋⠉ ⣠⣴⣿⣿⣿  ⣿⣿⣿⣿⣿⠟⠁',
    '⠻⣿⣿⣿⣤⣀ ⠈⠉ ⢀⣤⣶⣿⣿⣿⠿⠟⠁ ⣰⣿⣿⡟⠋⠁',
    '⠈⠿⣿⣿⣿⣿⣶⣾⣿⣿⣿⣿⠟⠋⠁  ⣠⣼⣿⣿⡟',
    '⠙⠛⠛⠛⠻⠛⢿⣿⣿⣿⣿⣷⣾⣿⣿⣿⡿⠏',
    '⠈⠙⠛⠿⠿⠿⠿⠛⠉',
)


def _starting(lines: list[str]) -> bool:
    """The observed initial header, logo and untouched placeholder, before a ready footer."""
    rows = [line.rstrip() for line in lines if line.strip()]
    if len(rows) < 21 or rows[0] != "  >_ OpenAI Codex (v0.166.0)" or not rows[1].startswith("     "):
        return False
    cwd = rows[1][5:]
    if not cwd.startswith(("/", "~/")):
        return False
    body = rows[2:]
    if body[0] == "  permissions: YOLO mode":
        body = body[1:]
    if tuple(line.strip() for line in body[:len(STARTUP_LOGO)]) != STARTUP_LOGO:
        return False
    tail = body[len(STARTUP_LOGO):]
    if not tail or tail[0] != "› " + PLACEHOLDER:
        return False
    return (tail[1:] == ["  ? for shortcuts"] or
            len(tail) == 3 and tail[2] == "  ? for shortcuts" and
            re.fullmatch(r"  \S+ default · " + re.escape(cwd), tail[1]) is not None)


DEFAULT_MODEL = "gpt-6-astra"
DEFAULT_EFFORT = "medium"
LOGIN_QUESTION = Interruption("login", r"Sign in with ChatGPT|Welcome to Codex|Log in", key=None)
APPROVAL_QUESTION = Interruption("approval prompt", r"Allow command\?|Approve\?|Yes, proceed", key=None)


CONFIG = """\
model = {model}
model_reasoning_effort = {effort}
approval_policy = "never"
sandbox_mode = "danger-full-access"
allow_login_shell = false
project_doc_max_bytes = 0
check_for_update_on_startup = false

[projects.{cwd}]
trust_level = "trusted"

[features]
memories = false
shell_snapshot = false

[tui]
notifications = false
"""
# features.memories: the program's own memory across conversations, off: a seat keeps nothing of its own
# between conversations, and what must survive is a post or a file of the ship's.
# allow_login_shell: off. The program's commands, and the shell snapshot it sources before each, would otherwise come
# from a login shell, which reads the startup files of HOME: the operator's own, since a seat inherits his HOME. Those
# brought the sudo helper, and a sudo() function that raises a password prompt on his screen, into every command.


def config_text(config: dict) -> str:
    """Write parsed configuration without losing unrelated TOML values or table scopes."""
    def quote(text: str) -> str:
        # JSON escapes the C0 controls; TOML also forbids literal DEL. Keep astral
        # characters literal: JSON's surrogate-pair escapes are invalid TOML scalars.
        return json.dumps(text, ensure_ascii=False).replace("\x7f", "\\u007f")

    def key(name: str) -> str:
        return name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else quote(name)

    def value(item: object) -> str:
        if isinstance(item, str):
            return quote(item)
        if isinstance(item, dict):
            return "{ " + ", ".join(f"{key(k)} = {value(v)}" for k, v in item.items()) + " }"
        if isinstance(item, list):
            return "[" + ", ".join(value(v) for v in item) + "]"
        if isinstance(item, (datetime, date, time)):
            return item.isoformat()
        return literal(item)

    def table(items: dict, path: tuple[str, ...] = ()) -> list[str]:
        lines = ["", "[" + ".".join(key(k) for k in path) + "]"] if path else []
        lines += [f"{key(k)} = {value(v)}" for k, v in items.items() if not isinstance(v, dict)]
        for name, item in items.items():
            if isinstance(item, dict):
                lines += table(item, (*path, name))
        return lines

    return "\n".join(table(config)) + "\n"


class CodexHarness(Harness):
    """Read Codex rollout JSONL and the terminal composer."""

    box_geometry_known = True  # box_columns below declares the established text area
    # 0.166.0: after paste, the composer shows the bottom rows. Its up-arrow
    # occupies the first visible row's one reserved right cell, outside the text.
    box_scroll = BoxScroll(mark='↑', anchor='bottom')

    def model_identity(self, value: str | None, *, version: str | None = None) -> str | None:
        # 0.166.0's footer displays the native id in title case (GPT-6-Astra).
        # Keep every character except case significant; the inherited record_model
        # reads turn_context.model through this same identity rule before screen fallback.
        value = super().model_identity(value, version=version)
        return value.casefold() if value is not None else None

    def check_tool_evidence(self, record: Path) -> dict:
        from .check_evidence import json_tools, codex_tools
        return json_tools(record, codex_tools)

    compactions = staticmethod(compactions)

    human_tools = ('request_user_input', 'request_user_input_async')
    human_tool_policy = "Native synchronous and asynchronous request_user_input tools are disabled, including plan mode."

    reads_usage = background_usage = True
    refuses_planted_instructions = True  # OpenAI's safeguard refuses work on a reader's planted instructions
    usage_from_records = staticmethod(codex_usage.read)

    def headline_windows(self):
        return ((None, "5h"), (None, "7d"))

    efforts = EffortCapability()  # CLI vocabulary is not measured
    defaults = DefaultMetadata(model_default=True, model=SuggestedValue(DEFAULT_MODEL, "named"),
                               effort=SuggestedValue(DEFAULT_EFFORT, "named"))
    effort_selectors = ("model_reasoning_effort",)
    selection_templates = ("config.toml",)

    name = "codex"
    display_name = 'Codex'
    setup = SetupMetadata(key_env="OPENAI_API_KEY", fresh_after_compactions=2)
    native_provider = "openai"
    program = "codex"
    sign_in_command = ("codex", "login")
    sign_in_note = ("Current docs place file-backed sign-in under CODEX_HOME (the directory must already exist), "
                    "but keyring/auto can use OS storage. Native storage selection is unknown; ask the person "
                    "how to sign in to the declared file-backed directory. No home-specific command is established here.")
    sign_in_sources = ("https://learn.chatgpt.com/docs/auth?surface=cli",
                       "https://learn.chatgpt.com/docs/config-file/environment-variables")
    credential_file = "auth.json"
    home_variable = "CODEX_HOME"
    keys = Keys(submit="Enter", send_now=None, interrupt="Escape")

    def freshness(self, path: Path) -> tuple[float, float] | None:
        try:
            tokens = json.loads(path.read_text()).get("tokens")
            if isinstance(tokens, dict) and tokens.get("refresh_token"):
                return (0.0, path.stat().st_mtime)
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return None

    def build_home(self, home: Path, seat: SeatSpec, pool: Pool, ship_home: Path | None) -> Credential | None:
        self.check_defaults(seat, ship_home)
        seat_files.make_folder(home)  # never through a link its jail planted
        runtime = home.parents[2]  # runtime/seats/<seat>/home
        cwd = seat.cwd or runtime.parent
        defaults = tomllib.loads(CONFIG.format(model=literal(seat.model or DEFAULT_MODEL),
                                               effort=literal(seat.effort or DEFAULT_EFFORT), cwd=literal(str(cwd))))
        if seat.model_mode == "program-default":
            defaults.pop("model")
        if seat.effort_mode == "program-default":
            defaults.pop("model_reasoning_effort")
        config = ship_template(ship_home, "config.toml")
        for key, value in defaults.items():
            if isinstance(value, dict):
                config[key] = {**value, **config.get(key, {})}
            else:
                config.setdefault(key, value)
        config.update(copy.deepcopy(seat.settings))
        # Full access, and no login shell, are required policy, including when a template selects a profile.
        for scope in [config, *config.get("profiles", {}).values()]:
            scope["sandbox_mode"] = "danger-full-access"  # law: can-post
            scope["approval_policy"] = "never"
            scope["allow_login_shell"] = False  # no command reads the operator's login startup files
            scope.setdefault("features", {})["shell_snapshot"] = False  # and no snapshot of a login shell is kept
            scope.pop("sandbox_workspace_write", None)
            if not seat.human_session:
                scope.setdefault('tools', {})['experimental_request_user_input'] = {'enabled': False}
        durable.replace_file(home / "config.toml", config_text(config))
        return credential(pool, home / self.credential_file, "OPENAI_API_KEY")

    def launch(self, seat: SeatSpec, pool: Pool, home: Path, brief: Path, resume: str | None, first: str | None) -> Launch:
        argv = [self.program, "--no-daemon", "-c", f"developer_instructions={json.dumps(brief.read_text())}"]
        if not seat.human_session:
            argv += ['-c', 'tools.experimental_request_user_input.enabled=false']
        if seat.model:
            argv += ["-m", seat.model]
        if seat.effort:
            argv += ["-c", f"model_reasoning_effort={json.dumps(seat.effort)}"]
        if resume:
            argv += ["resume", resume]
        if first:
            argv.append(first)
        env = {self.home_variable: str(home), **key_environment(pool, "OPENAI_API_KEY")}
        return Launch(tuple(argv), env)

    inputs = staticmethod(inputs)
    records = staticmethod(records)
    first_timestamp = staticmethod(first_timestamp)
    session_id = staticmethod(session_id)
    reader = staticmethod(read)

    def screen(self, text: str) -> Screen:
        """The box is the last ``› `` block above the status line, blank lines inside it and all."""
        lines = text.rstrip("\n").split("\n")
        status_at = next((i for i in range(len(lines) - 1, -1, -1) if STATUS_LINE.match(lines[i])), None)
        if status_at is None and _starting(lines):
            return Screen(text, box=None, starting=True)
        shows = {}
        if status_at is not None:
            status = STATUS_LINE.match(lines[status_at])
            shows = {"model": status.group(1), "effort": status.group(2)}
        box = None
        if status_at is not None:
            start_at = next((i for i in range(status_at - 1, -1, -1) if BOX_START.match(lines[i])), None)
            if start_at is not None:
                body = [BOX_START.match(lines[start_at]).group(1)] + [l[2:] if l.startswith("  ") else l for l in lines[start_at + 1:status_at]]
                body = trimmed(body)
                box = "" if len(body) == 1 and body[0].rstrip(" ") == PLACEHOLDER else "\n".join(body)
        working = bool(WORKING.search(text))
        return Screen(text, box=box, working=working, interruption=self.recognize(text) if box is None else None,
                      shows=shows, echoes=self.echoes(lines) if working else ())

    background_versions = frozenset({'0.166.0'})

    def background(self, screen: Screen, version: str | None) -> tuple[tuple[str, int], ...] | None:
        """0.166.0 names live terminals above the resting composer, not in the transcript.

        Measured with zero, one and two live exec sessions, and after both ended. The
        count disappears on process exit before the handles are drained. No terminal
        is a shell proved to be this seat's own inbox watch: even one stays work.
        """
        if version not in self.background_versions or screen.box != '' or screen.working is not False:
            return None
        lines = [line.rstrip() for line in screen.text.rstrip().split('\n')]
        if (len(lines) < 7 or lines[-4] != '› ' + PLACEHOLDER or lines[-3] != ''
                or not re.fullmatch(r'  \S+ (?:low|medium|high|xhigh|max) · \S.*', lines[-2])
                or lines[-1] != '  ? for shortcuts' or lines[-6:-4] != ['', '']):
            return None
        # A count or near-form anywhere else is not this measured native status row.
        counts = [(at, line) for at, line in enumerate(lines)
                  if re.match(r'^\s*\d+\s+background\b', line)]
        if not counts:
            return None if 'background' in lines[-7].casefold() else ()
        if len(counts) != 1 or counts[0][0] != len(lines) - 7:
            return None
        found = re.fullmatch(r'  ([1-9]\d*) background (terminals?) running · /ps to view · /stop to close',
                             counts[0][1])
        if found is None or found[2] != ('terminal' if found[1] == '1' else 'terminals'):
            return None
        return (('terminal', int(found[1])),)

    @staticmethod
    def echoes(lines: list[str]) -> tuple[str, ...]:
        """The inputs shown above ``• Working``: the turn's own prompt, and anything queued behind it."""
        found: list[list[str]] = []
        block: list[str] | None = None
        for line in lines:
            if WORKING.search(line):
                break
            start = BOX_START.match(line)
            if start:
                block = [start.group(1)]
                found.append(block)
            elif block is not None and (line.startswith("  ") or not line.strip()):
                block.append(line[2:] if line.startswith("  ") else "")
            else:
                block = None
        return tuple("\n".join(b).strip("\n") for b in found)

    def human_wake_box_margin(self, pane_width: int) -> int:
        return 3  # ordinary prefix plus measured right inset/cursor reserve

    def box_columns(self, pane_width: int) -> int:
        # 0.166.0 ordinary composer: LIVE_PREFIX_COLS=2, right inset=1.
        return pane_width - 3

    def box(self, text: str) -> Box:
        """A long paste folds to ``[Pasted Content N chars]``, N its whole length (0.166.0); a short one shows whole."""
        return EditorBox(text, folded=rf"\[Pasted Content {len(text)} chars\]", textwrap_ascii=True)

    paste_clear_rules = (PasteClearRule('codex-0.166.0-logical-line-clear', '0.166.0', ('C-a','C-k','BSpace')),)

    def paste_clear_keys(self,text,screen,columns,version,cursor):
        from .types import _cells
        rule=self.paste_clear_rule(version)
        if (rule is None or not text or text.endswith(' ')
                or not self.box(text).matches(screen.box,columns) or (screen.box or '').startswith('[Pasted')
                or any(_cells(line) is None for line in text.split('\n'))):
            return None
        content=text.rstrip('\n');trailing=len(text)-len(content)
        last=content.split('\n')[-1];width=_cells(last)
        if not last or width>=columns:
            return None  # end-of-line cursor wrapping is not a clear-path proof
        rows=screen.text.split('\n')
        status=next((i for i in range(len(rows)-1,-1,-1) if STATUS_LINE.match(rows[i])),None)
        if status is None:return None
        row=next((i for i in range(status-1,-1,-1) if rows[i].strip(' ')),None)
        expected=(2,row+trailing) if row is not None and trailing else (width+2,row)
        if row is None or rows[row][2:].rstrip(' ')!=last or cursor!=expected or cursor[1]>=status:
            return None
        # Native 0.166.0, measured unsubmitted with cursor at the paste's end:
        # kill the current logical line, then its preceding separator, once per line.
        return rule.keys(len(text.split('\n')))

    def controls(self) -> tuple[Control, ...]:
        return (
            Control("model", launch="-m <value>", session=None, confirmed_by="screen"),
            Control("effort", launch="-c model_reasoning_effort=<value>", session=None, confirmed_by="screen"),
        )

    def interruptions(self) -> tuple[Interruption, ...]:
        return (
            Interruption("model picker", r"Select Model and Effort", key="Escape"),
            Interruption("daybreak notice", r"Daybreak", key="Escape"),
            LOGIN_QUESTION,
            APPROVAL_QUESTION,
        )

    def check_account_questions(self) -> tuple[tuple[Interruption, str], ...]:
        return ((LOGIN_QUESTION, 'account-login'), (APPROVAL_QUESTION, 'account-permission'))
