"""Codex: the rollout in ``sessions/YYYY/MM/DD/rollout-<time>-<id>.jsonl`` under its home.

Every line is ``{"timestamp", "type", "payload"}``. A turn opens with the event
``task_started`` and closes with ``task_complete`` (or ``turn_aborted``); the prompt is a
``response_item`` message from the user; tool calls are ``function_call`` and
``custom_tool_call`` items closed by their ``_output`` items; a ``token_count`` event
follows each model response with the usage of that response and the running total.
"""

from __future__ import annotations

import copy
import json
import re
import tomllib
from datetime import date, datetime, time, timezone
from pathlib import Path

from .json_record import JsonLinesReader, input_rows
from .types import Inputs
from . import codex_usage

from ..ship import Pool, SeatSpec, literal
from .base import (Box, ship_template, trimmed, Input, Control, Credential, Harness, Interruption, Keys, Launch, RecordError, Screen, State, Tokens,
                   ToolCall, Turn, credential, entries, key_environment, program_version, when)

ENDS_TURN = {"task_complete", "turn_aborted"}
CALLS = {"function_call", "custom_tool_call"}
OUTPUTS = {"function_call_output", "custom_tool_call_output"}


def records(home: Path) -> list[Path]:
    files = [p for p in (home / "sessions").glob("*/*/*/rollout-*.jsonl") if p.is_file()]
    return sorted(files, key=lambda p: p.stat().st_mtime)


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


def inputs(record: Path) -> list[Input]:
    found = Inputs()
    seen = False
    for line, row in input_rows(record, _Reader.kinds, _Reader.label, found):
        kind = row.get('type')
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


class _Reader(JsonLinesReader):
    kinds = ('event_msg', 'response_item', 'session_meta', 'turn_context')
    label = "Codex"

    def take(self, entry: dict) -> None:
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            return  # not an entry this reader knows
        kind, sub, at = entry.get("type"), payload.get("type"), when(entry.get("timestamp"))
        if kind == "turn_context":
            self.model = payload.get("model") or self.model
            self.effort = payload.get("effort") or self.effort
        elif kind == "event_msg":
            self.event(sub, payload, at)
        elif kind == "response_item":
            self.item(sub, payload, at)

    def event(self, sub: str | None, payload: dict, at: datetime | None) -> None:
        if sub == "task_started":
            if self.open is not None:
                self.end(at, completed=False)
            self.start_turn({"started": at, "input": "", "tokens": Tokens(), "measurements": 0, "response": False, "tools": [], "reply": ""})
        elif sub == "token_count" and self.open is not None:
            info = payload.get("info")
            last = info.get("last_token_usage") if isinstance(info, dict) else None
            self.open["tokens"] = self.open["tokens"] + usage_tokens(last)
            self.open["measurements"] += 1
            self.open["response"] = False
        elif sub in ENDS_TURN and self.open is not None:
            if payload.get("last_agent_message"):
                self.open["reply"] = payload["last_agent_message"]
            self.end(at)

    def item(self, sub: str | None, payload: dict, at: datetime | None) -> None:
        if sub == "message" and payload.get("role") == "user":
            text = _text(payload.get("content"))
            if text and not text.startswith("<") and self.open is not None:
                self.last_input, self.last_input_at = text, at
                if self.open["input"]:
                    self.consumed.append(text)  # a message taken into the running turn
                else:
                    self.open["input"] = text
        elif sub == "message" and payload.get("role") == "assistant" and self.open is not None:
            self.open["reply"] = _text(payload.get("content")) or self.open["reply"]
            self.open["response"] = True
        elif sub in CALLS:
            if self.open is not None:
                self.open["response"] = True
            self.pending[payload.get("call_id")] = (payload.get("name", "?"), at)
        elif sub in OUTPUTS:
            if self.open is not None and self.open["response"]:
                self.open["tokens"] += Tokens.unknown()
                self.open["measurements"] += 1
                self.open["response"] = False
            self.close_tool(payload.get("call_id"), at, _failed(payload.get("output")))

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


CONFIG = """\
model = {model}
model_reasoning_effort = {effort}
approval_policy = "never"
sandbox_mode = "danger-full-access"
project_doc_max_bytes = 0
check_for_update_on_startup = false

[projects.{cwd}]
trust_level = "trusted"

[features]
memories = false

[tui]
notifications = false
"""
# features.memories: the program's own memory across conversations, off: a seat keeps nothing of its own
# between conversations, and what must survive is a post or a file of the ship's.


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
    """Codex, proven on 0.166.0. Model and effort change only at launch: ``/model`` opens a picker."""

    reads_usage = background_usage = True
    usage_from_records = staticmethod(codex_usage.read)

    def headline_windows(self):
        return ((None, "5h"), (None, "7d"))

    name = "codex"
    program = "codex"
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
        home.mkdir(parents=True, exist_ok=True)
        runtime = home.parents[2]  # runtime/seats/<seat>/home
        cwd = seat.cwd or runtime.parent
        defaults = tomllib.loads(CONFIG.format(model=literal(seat.model or "gpt-6-astra"),
                                               effort=literal(seat.effort or "medium"), cwd=literal(str(cwd))))
        config = ship_template(ship_home, "config.toml")
        for key, value in defaults.items():
            if isinstance(value, dict):
                config[key] = {**value, **config.get(key, {})}
            else:
                config.setdefault(key, value)
        config.update(copy.deepcopy(seat.settings))
        # Full access is required policy, including when a template selects a profile.
        for scope in [config, *config.get("profiles", {}).values()]:
            scope["sandbox_mode"] = "danger-full-access"  # law: can-post
            scope["approval_policy"] = "never"
            scope.pop("sandbox_workspace_write", None)
        (home / "config.toml").write_text(config_text(config))
        return credential(pool, home / self.credential_file, "OPENAI_API_KEY")

    def launch(self, seat: SeatSpec, pool: Pool, home: Path, brief: Path, resume: str | None, first: str | None) -> Launch:
        argv = [self.program, "--no-daemon", "-c", f"developer_instructions={json.dumps(brief.read_text())}"]
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

    def box_columns(self, pane_width: int) -> int:
        # 0.166.0 ordinary composer: LIVE_PREFIX_COLS=2, right inset=1.
        return pane_width - 3

    def box(self, text: str) -> Box:
        """A long paste folds to ``[Pasted Content N chars]``, N its whole length (0.166.0); a short one shows whole."""
        return Box(text, folded=rf"\[Pasted Content {len(text)} chars\]", textwrap_ascii=True)

    def controls(self) -> tuple[Control, ...]:
        return (
            Control("model", launch="-m <value>", session=None, confirmed_by="screen"),
            Control("effort", launch="-c model_reasoning_effort=<value>", session=None, confirmed_by="screen"),
        )

    def interruptions(self) -> tuple[Interruption, ...]:
        return (
            Interruption("model picker", r"Select Model and Effort", key="Escape"),
            Interruption("daybreak notice", r"Daybreak", key="Escape"),
            Interruption("login", r"Sign in with ChatGPT|Welcome to Codex|Log in", key=None),
            Interruption("approval prompt", r"Allow command\?|Approve\?|Yes, proceed", key=None),
        )
