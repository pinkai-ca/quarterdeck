"""The native Grok ACP update stream, read through the shared JSON-lines walk."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from .json_record import JsonLinesReader, RecordError, entries, input_reader, object_lines, records_matching, when
from .limits import Wall
from .types import Input, Inputs, Tokens, Turn

METHODS = ("session/update", "_x.ai/session/update")


def timestamp(value) -> datetime | None:
    if type(value) in (int, float):
        try:
            return datetime.fromtimestamp(value, timezone.utc) if math.isfinite(value) else None
        except (OverflowError, OSError, ValueError):
            return None
    return when(value)


def update(row: dict) -> dict | None:
    if row.get("method") not in METHODS:
        return None
    params = row.get("params")
    value = params.get("update") if isinstance(params, dict) else None
    return value if isinstance(value, dict) else None


def records(home: Path) -> list[Path]:
    return records_matching(home, "sessions/*/*/updates.jsonl")


def first_timestamp(record: Path) -> datetime | None:
    return next((timestamp(row.get("timestamp")) for row in entries(record) if update(row) is not None), None)


def session_id(record: Path) -> str | None:
    for row in entries(record):
        if update(row) is not None:
            value = row["params"].get("sessionId")
            if isinstance(value, str) and value:
                return value
    return None


def text_content(value: object) -> str | None:
    if isinstance(value, dict) and value.get("type") == "text" and isinstance(value.get("text"), str):
        return value["text"]
    return None


@input_reader(lambda row: (update(row) or {}).get("sessionUpdate") == "user_message_chunk")
def inputs(record: Path) -> Inputs:
    result = Inputs()
    rows = object_lines(record, result.skip)
    if rows and not any(update(row) is not None for _, row in rows):
        raise RecordError("no Grok updates in this record: another program's file")
    for number, row in rows:
        event = update(row)
        if event is None or event.get("sessionUpdate") != "user_message_chunk":
            continue
        text, at = text_content(event.get("content")), timestamp(row.get("timestamp"))
        if text is None or at is None:
            result.skip(number, "user_message_chunk", "text or timestamp unreadable")
        else:
            result.append(Input(text, at))
    return result


def usage_tokens(usage: object) -> Tokens:
    if not isinstance(usage, dict):
        return Tokens.unknown()
    measured = Tokens.from_usage(usage, ("cachedInputTokens", "inputTokens", "cacheWriteTokens", "outputTokens"))
    if not measured.complete or measured.read_cached > measured.read_uncached:
        return Tokens.unknown()
    return replace(measured, read_uncached=measured.read_uncached - measured.read_cached)


class Reader(JsonLinesReader):
    label = "Grok"

    @classmethod
    def identifies(cls, row: dict) -> bool:
        return update(row) is not None

    def __init__(self, record: Path, snapshot=None):
        self.walls = []
        self.execution_seen = False
        super().__init__(record, snapshot)

    def ensure_turn(self):
        """Keep ended native work even when its initiating input was unavailable."""
        self.execution_seen = True
        if self.open is None:
            self.start_turn(dict(input="", started=None, reply="", tools=[]))

    def take(self, row: dict) -> None:
        event, at = update(row), timestamp(row.get("timestamp"))
        if event is None:
            self.tools_known = False
            return
        kind = event.get("sessionUpdate")
        if kind == "user_message_chunk":
            text = text_content(event.get("content"))
            if text is None:
                self.tools_known = False
                return
            if self.open is not None:
                self.end(None)  # retained accounting, never an invented completion
            self.last_input, self.last_input_at = text, at
            self.start_turn(dict(input=text, started=at, reply="", tools=[]))
            self.execution_seen = True
        elif kind == "agent_message_chunk":
            text = text_content(event.get("content"))
            if text is not None:
                self.ensure_turn()
                self.open["reply"] += text
        elif kind == "agent_thought_chunk":
            self.ensure_turn()
        elif kind == "tool_call":
            self.ensure_turn()
            call = event.get("toolCallId")
            self.open_call(call, event.get("title"), at)
            if event.get("status") in ("completed", "failed"):
                self.close_tool(call, at, event["status"] == "failed")
        elif kind == "tool_call_update":
            self.ensure_turn()
            if event.get("status") in ("completed", "failed"):
                self.close_tool(event.get("toolCallId"), at, event["status"] == "failed")
        elif kind == "turn_completed":
            self.ensure_turn()
            self.end(at, event)
        elif kind == "retry_state":
            if event.get("error_type") == "rate_limited":
                self.walls.append(Wall(f"retry:{len(self.walls)}", at, None, "native rate limit", signature="grok:rate_limited"))
        elif kind == "config_option_update":
            for option in event.get("configOptions", []):
                value = option.get("currentValue")
                if isinstance(value, str) and option.get("id") == "model":
                    self.model = value
                elif isinstance(value, str) and option.get("id") == "reasoning_effort":
                    self.effort = value
        elif kind not in ("plan", "available_commands_update", "session_info_update"):
            self.tools_known = False

    def end(self, at, event=None):
        if self.open is None:
            return
        value = self.close_turn()
        event = event or {}
        self.record_turn(Turn(
            len(self.turns), value["started"], at, value["input"], value["reply"], self.model,
            usage_tokens(event.get("usage")), tuple(value["tools"]), subagent_turns=None,
            timed=at is not None,
        ), at)

    def state(self):
        return replace(super().state(), walls=tuple(self.walls),
                       turn_open=self.open is not None if self.execution_seen else None)
