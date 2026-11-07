"""Codex: the rollout in ``sessions/YYYY/MM/DD/rollout-<time>-<id>.jsonl`` under its home.

Every line is ``{"timestamp", "type", "payload"}``. A turn opens with the event
``task_started`` and closes with ``task_complete`` (or ``turn_aborted``); the prompt is a
``response_item`` message from the user; tool calls are ``function_call`` and
``custom_tool_call`` items closed by their ``_output`` items; a ``token_count`` event
follows each model response with the usage of that response and the running total.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from ..ship import Pool, SeatSpec, literal
from .base import (Box, Control, Credential, Harness, Interruption, Keys, Launch, Screen, State, Tokens,
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


def _failed(output: str | list | None) -> bool:
    """A shell call's output is JSON with the exit code; anything else is taken as success."""
    if not isinstance(output, str):
        return False
    try:
        return bool(json.loads(output).get("metadata", {}).get("exit_code", 0))
    except (json.JSONDecodeError, AttributeError):
        return False


class _Reader:
    def __init__(self, record: Path):
        self.record = record
        self.turns: list[Turn] = []
        self.open: dict | None = None
        self.pending: dict[str, tuple[str, datetime | None]] = {}
        self.model = self.effort = self.last_input = self.last_input_at = None
        for entry in entries(record):
            self.take(entry)

    def take(self, entry: dict) -> None:
        payload = entry.get("payload") or {}
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
            if text and not text.startswith("<") and self.open is not None and not self.open["input"]:
                self.open["input"] = self.last_input = text
                self.last_input_at = at
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
WORKING = re.compile(r"• Working \(\d+s • esc to interrupt")
CONFIG = """\
model = {model}
model_reasoning_effort = {effort}
approval_policy = "never"
sandbox_mode = "workspace-write"
project_doc_max_bytes = 0
check_for_update_on_startup = false

[sandbox_workspace_write]
writable_roots = [{runtime}]
network_access = true

[projects.{cwd}]
trust_level = "trusted"

[tui]
notifications = false
"""


class CodexHarness(Harness):
    """Codex, proven on 0.166.0. Model and effort change only at launch: ``/model`` opens a picker."""

    name = "codex"
    home_variable = "CODEX_HOME"
    keys = Keys(submit="Enter", send_now=None, interrupt="Escape", empty=("C-c",))
    brief_channel = "override"

    def build_home(self, home: Path, seat: SeatSpec, pool: Pool, ship_home: Path | None) -> Credential | None:
        home.mkdir(parents=True, exist_ok=True)
        runtime = home.parents[2]  # runtime/seats/<seat>/home
        if ship_home and (ship_home / "config.toml").is_file():
            config = (ship_home / "config.toml").read_text()
        else:
            config = CONFIG.format(model=literal(seat.model or "gpt-6-astra"), effort=literal(seat.effort or "medium"),
                                   runtime=literal(str(runtime)), cwd=literal(str(seat.cwd or runtime.parent.parent)))
        for key, value in seat.settings.items():
            config = f"{key} = {literal(value)}\n" + config
        (home / "config.toml").write_text(config)
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
        lines = text.rstrip("\n").split("\n")
        box = None
        for index in range(len(lines) - 1, -1, -1):
            start = BOX_START.match(lines[index])
            if start:
                body = [start.group(1)]
                for following in lines[index + 1:]:
                    if not following.startswith("  ") or not following.strip():
                        break
                    body.append(following[2:])
                box = "" if body == [PLACEHOLDER] else "\n".join(body).rstrip()
                break
        shows = {}
        for line in lines:
            status = STATUS_LINE.match(line)
            if status:
                shows = {"model": status.group(1), "effort": status.group(2)}
        return Screen(text, box=box, working=bool(WORKING.search(text)), interruption=self.recognize(text), shows=shows)

    def box(self, text: str) -> Box:
        return Box(text)

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
