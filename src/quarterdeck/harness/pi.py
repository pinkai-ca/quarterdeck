"""pi: one JSON-lines file per session under ``sessions/`` in the seat home.

The first line is the session, with its id and working directory; ``model_change`` and
``thinking_level_change`` lines carry the model and the thinking level; ``message`` lines carry the
conversation: a user message, the assistant messages that answer it, each with its model, usage,
cost and stop reason, and the tool results between them. A message typed while a turn runs is
steered into it after the current tool call, and enters the record only then. Proven on 0.74.2
against a local vLLM endpoint.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .. import endpoint
from ..ship import Pool, SeatSpec, ShipError
from .base import (Box, Input, Control, Credential, Harness, Interruption, Keys, Launch, RecordError, Screen, State, Tokens,
                   ToolCall, Turn, entries, merged, program_version, when)

SETTINGS = {
    "defaultProjectTrust": "always",  # the workspace is the ship's: no trust dialog
    "quietStartup": True,
    "enableInstallTelemetry": False,
}
UNMEASURED_STOPS = {"aborted", "error"}
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh")


# -- the record ---------------------------------------------------------------------------------


def records(home: Path) -> list[Path]:
    folder = home / "sessions"
    return sorted(p for p in folder.glob("*.jsonl") if p.is_file()) if folder.is_dir() else []


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
    if not isinstance(usage, dict) or (not usage.get("totalTokens") and message.get("stopReason") in UNMEASURED_STOPS):
        return Tokens.unknown()  # a step the program never measured: aborted before the usage arrived
    return Tokens(read_cached=usage.get("cacheRead") or 0, read_uncached=usage.get("input") or 0,
                  cache_written=usage.get("cacheWrite") or 0, written=usage.get("output") or 0)


class _Reader:
    def __init__(self, record: Path):
        self.record = record
        self.turns: list[Turn] = []
        self.open: dict | None = None
        self.pending: dict[str, tuple[str, datetime | None]] = {}
        self.model = self.effort = self.last_input = self.last_input_at = None
        self.consumed: list[str] = []
        rows = entries(record)
        if rows and not any(row.get("type") in ("session", "message", "model_change") for row in rows):
            raise RecordError("no pi entries in this record: another program's file")
        for entry in rows:
            try:
                self.take(entry)
            except (TypeError, ValueError, AttributeError, KeyError):
                continue  # a line of a shape this reader does not know is skipped; the rest is still read

    def take(self, entry: dict) -> None:
        kind, at = entry.get("type"), when(entry.get("timestamp"))
        if kind == "model_change":
            self.model = entry.get("modelId") or self.model
        elif kind == "thinking_level_change":
            self.effort = entry.get("thinkingLevel") or self.effort
        elif kind == "message" and isinstance(entry.get("message"), dict):
            self.message(entry["message"], at)

    def message(self, message: dict, at: datetime | None) -> None:
        role = message.get("role")
        if role == "user":
            text = _text(message.get("content"))
            self.last_input, self.last_input_at = text, at
            if self.open is not None:
                self.consumed.append(text)  # steered into the running turn after its current tool call
            else:
                self.open = {"started": at, "input": text, "tokens": Tokens(), "tools": [], "reply": "", "cost": 0.0}
        elif role == "assistant":
            if self.open is None:
                self.open = {"started": at, "input": "", "tokens": Tokens(), "tools": [], "reply": "", "cost": 0.0}
            self.model = message.get("model") or self.model
            self.open["tokens"] = self.open["tokens"] + _tokens(message)
            cost = ((message.get("usage") or {}).get("cost") or {}).get("total")
            if isinstance(cost, (int, float)):
                self.open["cost"] += cost
            for block in message.get("content") or []:
                if block.get("type") == "text" and block.get("text"):
                    self.open["reply"] = block["text"]
                elif block.get("type") == "toolCall":
                    name = " ".join(str(block.get("name") or "").split()) or "?"  # a malformed call may carry a name with line breaks
                    self.pending[block.get("id")] = (name, at)
            if message.get("stopReason") != "toolUse":
                self.end(at)
        elif role == "toolResult":
            name, started = self.pending.pop(message.get("toolCallId"), (message.get("toolName") or "?", None))
            seconds = (at - started).total_seconds() if at and started else None
            if self.open is not None:
                self.open["tools"].append(ToolCall(name, seconds, bool(message.get("isError"))))

    def end(self, at: datetime | None) -> None:
        turn, self.open = self.open, None
        self.pending.clear()
        self.turns.append(Turn(len(self.turns), turn["started"] or at, at or turn["started"], turn["input"], turn["reply"],
                               self.model, turn["tokens"], tuple(turn["tools"]), cost=turn["cost"]))

    def state(self) -> State:
        try:
            last_write = datetime.fromtimestamp(self.record.stat().st_mtime, timezone.utc)
        except FileNotFoundError:
            last_write = None
        tool = next(iter(self.pending.values()), None) if self.open is not None else None
        return State(turn_open=self.open is not None, open_tool=tool[0] if tool else None, open_tool_since=tool[1] if tool else None,
                     last_write=last_write, last_input=self.last_input, last_input_at=self.last_input_at, model=self.model,
                     effort=self.effort, consumed=tuple(self.consumed), turns=len(self.turns))


def state(record: Path) -> State:
    return _Reader(record).state()


def turns(record: Path) -> list[Turn]:
    return _Reader(record).turns


def read(record: Path) -> tuple[State, list[Turn]]:
    reader = _Reader(record)
    return reader.state(), reader.turns


def inputs(record: Path) -> list[Input]:
    """Every user message with the time it entered the record, for the delivery receipt."""
    found = []
    for entry in entries(record):
        message = entry.get("message")
        if entry.get("type") == "message" and isinstance(message, dict) and message.get("role") == "user":
            found.append(Input(_text(message.get("content")), when(entry.get("timestamp"))))
    return found


# -- the adapter --------------------------------------------------------------------------------

RULE = re.compile(r"^─{20,}\s*$")
# "↑1.6k ↓19 1.6%/98k (auto)    qwen3.8-27b • medium"; a model declared without reasoning shows no thinking level
FOOTER = re.compile(r"^(?P<stats>\S.*?)\s{2,}(?P<model>\S+)(?: • (?P<level>\w+))?\s*$")
WORKING = re.compile(r"^\s*[⠁-⣿] Working\.\.\.", re.M)
QUEUED_HINT = "Alt+Up to edit all queued messages"
STEERING = re.compile(r"^\s*Steering: (.*?)\s*$", re.M)


class PiHarness(Harness):
    """pi, proven on 0.74.2. Runs on an endpoint pool; model and thinking level change only at launch."""

    name = "pi"
    home_variable = "PI_CODING_AGENT_DIR"
    keys = Keys(submit="Enter", send_now=None, interrupt="Escape", empty=None)
    brief_channel = "system-prompt"
    wrap_slack = 1  # probed on 0.74.2: a word wraps one column short of where a word without spaces breaks

    def accepts(self, pool: Pool) -> str | None:
        if pool.kind != "endpoint":
            return f"pool {pool.name} names no endpoint, and {self.name} runs on an endpoint pool: declare endpoint = \"http://host:port/v1\""
        return None

    def build_home(self, home: Path, seat: SeatSpec, pool: Pool, ship_home: Path | None) -> Credential | None:
        agent, sessions = home / "agent", home / "sessions"
        agent.mkdir(parents=True, exist_ok=True)
        sessions.mkdir(parents=True, exist_ok=True)
        if seat.effort and seat.effort not in THINKING_LEVELS:
            raise ShipError(f"{seat.name}: effort {seat.effort!r} is not a pi thinking level; the levels are {', '.join(THINKING_LEVELS)}")
        choice = endpoint.choose(pool, seat.model)
        secret = endpoint.key(pool)
        model = {"id": choice.model, "name": choice.model, "reasoning": seat.effort is not None, "input": ["text"],
                 "contextWindow": choice.context, "maxTokens": choice.output,
                 "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}
        provider = {"baseUrl": pool.endpoint, "api": "openai-completions",
                    "apiKey": endpoint.KEY_VARIABLE if secret else "local",  # a variable's name, read by the program; never the key
                    "models": [model]}
        models = {"$schema": "https://pi.dev/schemas/models.schema.json", "providers": {pool.name: provider}}
        if ship_home and (ship_home / "models.json").is_file():
            models = merged(models, json.loads((ship_home / "models.json").read_text()))
        settings = merged(SETTINGS, json.loads((ship_home / "settings.json").read_text())) if ship_home and (ship_home / "settings.json").is_file() else dict(SETTINGS)
        settings = merged(settings, seat.settings)
        (agent / "models.json").write_text(json.dumps(models, indent=2) + "\n")
        (agent / "settings.json").write_text(json.dumps(settings, indent=2) + "\n")
        choice.write(home)
        return Credential(variable=endpoint.KEY_VARIABLE) if secret else None

    def launch(self, seat: SeatSpec, pool: Pool, home: Path, brief: Path, resume: str | None, first: str | None) -> Launch:
        choice = endpoint.Choice.read(home)
        argv = ["pi", "--provider", pool.name, "--model", choice.model, "--session-dir", str(home / "sessions"),
                "--append-system-prompt", str(brief), "--no-context-files",  # the brief, and no instruction file of the user's
                "--no-extensions", "--no-skills", "--no-prompt-templates"]  # nothing of the project's runs or instructs a seat
        if seat.effort:
            argv += ["--thinking", seat.effort]
        if resume:
            record = next((r for r in reversed(records(home)) if session_id(r) == resume), None)
            if record is None:
                raise ShipError(f"{seat.name}: no record in {home} holds the session {resume}; qd fresh {seat.name} starts a new one")
            argv += ["--session", str(record)]
        if first:
            argv.append(first)
        env = {self.home_variable: str(home / "agent"), "PI_SKIP_VERSION_CHECK": "1", **endpoint.key(pool)}
        return Launch(tuple(argv), env)

    def version(self) -> str | None:
        return program_version(("pi", "--version"))

    inputs = staticmethod(inputs)
    records = staticmethod(records)
    first_timestamp = staticmethod(first_timestamp)
    session_id = staticmethod(session_id)
    state = staticmethod(state)
    turns = staticmethod(turns)
    read = staticmethod(read)

    def screen(self, text: str) -> Screen:
        """The box is between two rules above the working directory and the footer with the model and thinking level."""
        lines = text.rstrip("\n").split("\n")
        working = bool(WORKING.search(text))
        queued = 1 if QUEUED_HINT in text else 0
        echoes = tuple(m.group(1) for m in STEERING.finditer(text))
        footer_at = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].strip()), None)  # the program draws top-down: rows below it stay blank
        footer = FOOTER.match(lines[footer_at]) if footer_at is not None else None
        shows = {}
        if footer:
            shows = {"model": footer.group("model"), **({"effort": footer.group("level")} if footer.group("level") else {})}
        interruption = self.recognize(text)
        if footer is None or interruption is not None:
            unknown = None if footer or interruption else "no footer drawn as 0.74.2 draws it"
            return Screen(text, box=None, working=working, interruption=interruption, unknown=unknown, queued=queued, shows=shows, echoes=echoes)
        bottom = next((i for i in range(footer_at - 1, -1, -1) if RULE.match(lines[i])), None)
        if bottom is None or footer_at - bottom > 3:  # the working directory sits between the bottom rule and the footer
            return Screen(text, box=None, working=working, unknown="no box drawn as 0.74.2 draws it", queued=queued, shows=shows, echoes=echoes)
        top = next((i for i in range(bottom - 1, -1, -1) if RULE.match(lines[i])), None)
        if top is None:
            return Screen(text, box=None, working=working, unknown="no box drawn as 0.74.2 draws it", queued=queued, shows=shows, echoes=echoes)
        body = lines[top + 1:bottom]
        while body and not body[-1].strip():
            body.pop()
        return Screen(text, box="\n".join(body), working=working, queued=queued, shows=shows, echoes=echoes)

    def box_columns(self, pane_width: int) -> int | None:
        return pane_width - 1  # the box spans the pane from the first column; the last column stays clear

    def box(self, text: str) -> Box:
        """Probed on 0.74.2: a short paste shows whole; a long one folds to its count of lines, or of characters when few."""
        return Box(text, folded=rf"\[paste #\d+ (?:\+{text.count(chr(10)) + 1} lines|{len(text)} chars)\]")

    def controls(self) -> tuple[Control, ...]:
        return (
            Control("model", launch="--model <value>", session=None, confirmed_by="record"),
            Control("effort", launch="--thinking <value>", session=None, confirmed_by="record"),
        )

    def interruptions(self) -> tuple[Interruption, ...]:
        return (Interruption("model picker", r"Only showing models from configured providers", key="Escape"),)
