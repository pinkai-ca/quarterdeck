"""Claude Code: the record in ``projects/<slug>/<session>.jsonl`` under its home.

An entry per step. A typed prompt is a ``user`` entry whose content is a string; a tool
result is a ``user`` entry whose content lists ``tool_result`` blocks. Each ``assistant``
entry carries the model, the usage of its API message (repeated on every entry of the
same message) and the stop reason that ends a turn. Queued messages appear as
``queue-operation`` entries the moment they are queued.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .base import State, Tokens, ToolCall, Turn

ENDS_TURN = {"end_turn", "max_tokens", "stop_sequence"}
LOCAL_TAGS = ("<command-name>", "<local-command", "<bash-input>", "<bash-stdout>", "<bash-stderr>")
INTERRUPTED = "[Request interrupted"


def records(home: Path) -> list[Path]:
    files = [p for p in (home / "projects").glob("*/*.jsonl") if p.is_file()]
    return sorted(files, key=lambda p: p.stat().st_mtime)


def entries(record: Path) -> list[dict]:
    """Every parseable line; a line still being written is skipped, not an error."""
    out = []
    try:
        lines = record.read_text().splitlines()
    except FileNotFoundError:
        return out
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def when(value: str | None) -> datetime | None:
    if not value:
        return None
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def first_timestamp(record: Path) -> datetime | None:
    return next((when(e["timestamp"]) for e in entries(record) if e.get("timestamp")), None)


def session_id(record: Path) -> str | None:
    return next((e["sessionId"] for e in entries(record) if e.get("sessionId")), None)


def prompt_text(entry: dict) -> str | None:
    """The text of a typed prompt, or None for a tool result or a local command's echo."""
    if entry.get("type") != "user" or entry.get("isSidechain"):
        return None
    content = entry.get("message", {}).get("content")
    if isinstance(content, list):
        if any(block.get("type") == "tool_result" for block in content):
            return None
        content = "\n".join(block.get("text", "") for block in content if block.get("type") == "text")
    if not isinstance(content, str) or content.startswith(LOCAL_TAGS):
        return None
    return content


def usage_tokens(usage: dict) -> Tokens:
    return Tokens(
        read_cached=usage.get("cache_read_input_tokens", 0) or 0,
        read_uncached=usage.get("input_tokens", 0) or 0,
        cache_written=usage.get("cache_creation_input_tokens", 0) or 0,
        written=usage.get("output_tokens", 0) or 0,
    )


class _Reader:
    """Walks a record once, producing the ended turns and the live state together."""

    def __init__(self, record: Path):
        self.record = record
        self.turns: list[Turn] = []
        self.open: dict | None = None
        self.pending: dict[str, tuple[str, datetime | None]] = {}
        self.queued: list[str] = []
        self.model = self.effort = self.last_input = None
        self.subagent_turns = 0
        for entry in entries(record):
            self.take(entry)

    def take(self, entry: dict) -> None:
        kind = entry.get("type")
        if kind == "queue-operation":
            self.queue(entry)
        elif entry.get("isSidechain"):
            if kind == "assistant" and entry.get("message", {}).get("stop_reason") in ENDS_TURN:
                self.subagent_turns += 1
        elif kind == "user":
            self.user(entry)
        elif kind == "assistant":
            self.assistant(entry)

    def queue(self, entry: dict) -> None:
        text = entry.get("content") or ""
        if entry.get("operation") == "enqueue":
            self.queued.append(text)
        elif text in self.queued:
            self.queued.remove(text)

    def user(self, entry: dict) -> None:
        text = prompt_text(entry)
        if text is not None and text.startswith(INTERRUPTED):
            if self.open is not None:
                self.end(when(entry.get("timestamp")))
            return
        if text is not None:
            self.last_input = text
            if text in self.queued:
                self.queued.remove(text)
            self.open = {"started": when(entry.get("timestamp")), "input": text, "usage": {}, "tools": [],
                         "reply": "", "model": None, "subagents": self.subagent_turns}
            return
        for block in entry.get("message", {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                self.close_tool(block, when(entry.get("timestamp")))

    def close_tool(self, block: dict, at: datetime | None) -> None:
        name, started = self.pending.pop(block.get("tool_use_id"), ("?", None))
        seconds = (at - started).total_seconds() if at and started else None
        if self.open is not None:
            self.open["tools"].append(ToolCall(name, seconds, bool(block.get("is_error"))))

    def assistant(self, entry: dict) -> None:
        message = entry.get("message", {})
        self.model = message.get("model") or self.model
        self.effort = entry.get("effort") or self.effort
        if self.open is None:
            return
        self.open["model"] = self.model
        if message.get("usage"):
            self.open["usage"][message.get("id") or entry.get("requestId") or entry.get("uuid")] = message["usage"]
        for block in message.get("content") or []:
            if block.get("type") == "tool_use":
                self.pending[block.get("id")] = (block.get("name", "?"), when(entry.get("timestamp")))
            elif block.get("type") == "text" and block.get("text"):
                self.open["reply"] = block["text"]
        if message.get("stop_reason") in ENDS_TURN:
            self.end(when(entry.get("timestamp")))

    def end(self, at: datetime | None) -> None:
        turn = self.open
        self.open = None
        tokens = sum((usage_tokens(u) for u in turn["usage"].values()), Tokens())
        self.turns.append(
            Turn(
                index=len(self.turns),
                started=turn["started"] or at,
                ended=at or turn["started"],
                input=turn["input"],
                reply=turn["reply"],
                model=turn["model"],
                tokens=tokens,
                tools=tuple(turn["tools"]),
                subagent_turns=self.subagent_turns - turn["subagents"],
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
            model=self.model,
            effort=self.effort,
            queued=tuple(self.queued),
            turns=len(self.turns),
        )


def state(record: Path) -> State:
    return _Reader(record).state()


def turns(record: Path) -> list[Turn]:
    return _Reader(record).turns
