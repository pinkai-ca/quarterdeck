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

from ..ship import Pool, SeatSpec, literal
from .base import (Box, Control, Credential, Harness, Interruption, Keys, Launch, RecordError, Screen, State, Tokens,
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
    cached = usage.get("cached_input_tokens", 0) or 0
    return Tokens(
        read_cached=cached,
        read_uncached=max((usage.get("input_tokens", 0) or 0) - cached, 0),
        cache_written=usage.get("cache_write_input_tokens", 0) or 0,
        written=usage.get("output_tokens", 0) or 0,
    )


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


class _Reader:
    def __init__(self, record: Path):
        self.record = record
        self.turns: list[Turn] = []
        self.open: dict | None = None
        self.pending: dict[str, tuple[str, datetime | None]] = {}
        self.model = self.effort = self.last_input = self.last_input_at = None
        self.consumed: list[str] = []
        rows = entries(record)
        if not any(row.get('type') in ['event_msg', 'response_item', 'session_meta', 'turn_context'] for row in rows):
            raise RecordError('record has no recognizable codex entries')
        try:
            for entry in rows:
                self.take(entry)
        except (TypeError, ValueError, AttributeError, KeyError) as error:
            raise RecordError('malformed codex record fields') from error

    def take(self, entry: dict) -> None:
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            raise RecordError("Codex entry payload is not an object")
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
                self.end(at)
            self.pending.clear()
            self.open = {"started": at, "input": "", "tokens": Tokens(), "tools": [], "reply": ""}
        elif sub == "token_count" and self.open is not None:
            last = (payload.get("info") or {}).get("last_token_usage") or {}
            self.open["tokens"] = self.open["tokens"] + usage_tokens(last)
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
        elif sub in CALLS:
            self.pending[payload.get("call_id")] = (payload.get("name", "?"), at)
        elif sub in OUTPUTS:
            name, started = self.pending.pop(payload.get("call_id"), ("?", None))
            seconds = (at - started).total_seconds() if at and started else None
            if self.open is not None:
                self.open["tools"].append(ToolCall(name, seconds, _failed(payload.get("output"))))

    def end(self, at: datetime | None) -> None:
        turn = self.open
        self.open = None
        self.pending.clear()
        self.turns.append(
            Turn(
                index=len(self.turns),
                started=turn["started"] or at,
                ended=at or turn["started"],
                input=turn["input"],
                reply=turn["reply"],
                model=self.model,
                tokens=turn["tokens"],
                tools=tuple(turn["tools"]),
            )
        )

    def state(self) -> State:
        try:
            last_write = datetime.fromtimestamp(self.record.stat().st_mtime, timezone.utc)
        except FileNotFoundError:
            last_write = None
        tool = next(iter(self.pending.values()), None) if self.open is not None else None
        return State(
            turn_open=self.open is not None,
            open_tool=tool[0] if tool else None,
            open_tool_since=tool[1] if tool else None,
            last_write=last_write,
            last_input=self.last_input,
            last_input_at=self.last_input_at,
            model=self.model,
            effort=self.effort,
            consumed=tuple(self.consumed),
            turns=len(self.turns),
        )


def state(record: Path) -> State:
    return _Reader(record).state()


def turns(record: Path) -> list[Turn]:
    return _Reader(record).turns


def read(record: Path) -> tuple[State, list[Turn]]:
    reader = _Reader(record)
    return reader.state(), reader.turns


# -- the adapter -----------------------------------------------------------------------------

BOX_START = re.compile(r"^› (.*)$")
PLACEHOLDER = "Ask Codex to do anything"
STATUS_LINE = re.compile(r"^\s+(\S+) (low|medium|high|xhigh|max) · ")
WORKING = re.compile(r"• Working \([^)]*esc to interrupt")  # the elapsed time reads 10s, then 1m 05s
CONFIG = """\
model = {model}
model_reasoning_effort = {effort}
approval_policy = "never"
sandbox_mode = "danger-full-access"
project_doc_max_bytes = 0
check_for_update_on_startup = false

[projects.{cwd}]
trust_level = "trusted"

[tui]
notifications = false
"""


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

    name = "codex"
    home_variable = "CODEX_HOME"
    keys = Keys(submit="Enter", send_now=None, interrupt="Escape", empty=("C-c",))
    brief_channel = "override"

    def build_home(self, home: Path, seat: SeatSpec, pool: Pool, ship_home: Path | None) -> Credential | None:
        home.mkdir(parents=True, exist_ok=True)
        runtime = home.parents[2]  # runtime/seats/<seat>/home
        cwd = seat.cwd or runtime.parent
        defaults = tomllib.loads(CONFIG.format(model=literal(seat.model or "gpt-6-astra"),
                                               effort=literal(seat.effort or "medium"), cwd=literal(str(cwd))))
        template = ship_home / "config.toml" if ship_home else None
        config = tomllib.loads(template.read_text()) if template and template.is_file() else {}
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
        return credential(pool, home / "auth.json", "OPENAI_API_KEY")

    def launch(self, seat: SeatSpec, pool: Pool, home: Path, brief: Path, resume: str | None, first: str | None) -> Launch:
        argv = ["codex", "--no-daemon", "-c", f"developer_instructions={json.dumps(brief.read_text())}"]
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

    def version(self) -> str | None:
        return program_version(("codex", "--version"))

    records = staticmethod(records)
    first_timestamp = staticmethod(first_timestamp)
    session_id = staticmethod(session_id)
    state = staticmethod(state)
    turns = staticmethod(turns)
    read = staticmethod(read)

    def screen(self, text: str) -> Screen:
        """The box is the last ``› `` block above the status line, blank lines inside it and all."""
        lines = text.rstrip("\n").split("\n")
        status_at = next((i for i in range(len(lines) - 1, -1, -1) if STATUS_LINE.match(lines[i])), None)
        shows = {}
        if status_at is not None:
            status = STATUS_LINE.match(lines[status_at])
            shows = {"model": status.group(1), "effort": status.group(2)}
        box = None
        if status_at is not None:
            start_at = next((i for i in range(status_at - 1, -1, -1) if BOX_START.match(lines[i])), None)
            if start_at is not None:
                body = [BOX_START.match(lines[start_at]).group(1)] + [l[2:] if l.startswith("  ") else l for l in lines[start_at + 1:status_at]]
                while body and not body[-1].strip():
                    body.pop()
                box = "" if body == [PLACEHOLDER] else "\n".join(body)
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
        return Box(text, folded=rf"\[Pasted Content {len(text)} chars\]")

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
