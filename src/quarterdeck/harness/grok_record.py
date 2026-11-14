"""The native Grok ACP update stream, read through the shared JSON-lines walk."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from .json_record import JsonLinesReader, RecordError, entries, input_reader, object_lines, records_matching, when
from .types import Input, Inputs, Tokens, ToolCall, Turn

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
    """Join-only creation time: every native row must agree with the UUIDv7 folder."""
    try:
        identity = UUID(record.parent.name)
        if identity.version != 7:
            return None
        created = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=identity.int >> 80)
    except (ValueError, OverflowError):
        return None
    skipped = []
    rows = object_lines(record, lambda *reason: skipped.append(reason))
    if skipped or not rows:
        return None
    for _, row in rows:
        if update(row) is None or row["params"].get("sessionId") != record.parent.name:
            return None
    return created


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
    fields = ("cachedReadTokens", "inputTokens", "cacheCreationTokens", "outputTokens")
    if not isinstance(usage, dict) or not all(field in usage for field in fields):
        return Tokens.unknown()
    measured = Tokens.from_usage(usage, fields)
    if not measured.complete or measured.read_cached > measured.read_uncached:
        return Tokens.unknown()
    if measured.cache_written:
        return Tokens.unknown()  # nonzero creation's relationship to inputTokens is unmeasured
    return replace(measured, read_uncached=measured.read_uncached - measured.read_cached)


def usage_model(usage: object) -> str | None:
    models = usage.get("modelUsage") if isinstance(usage, dict) else None
    if isinstance(models, dict) and len(models) == 1:
        model, value = next(iter(models.items()))
        if isinstance(model, str) and model and isinstance(value, dict):
            return model
    return None  # mixed or absent model evidence is not the previous turn's model


@dataclass
class Call:
    started: datetime | None
    result: ToolCall
    finished: bool = False
    turn: int | None = None
    slot: int = 0


class Reader(JsonLinesReader):
    label = "Grok"

    @classmethod
    def identifies(cls, row: dict) -> bool:
        return update(row) is not None

    def __init__(self, record: Path, snapshot=None):
        self.execution_seen = False
        self.calls: dict[str, Call] = {}
        self.completed_prompts: set[str] = set()
        super().__init__(record, snapshot)

    def start_turn(self, turn: dict) -> None:
        super().start_turn({**turn, "calls": []})

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
            metadata = event.get('_meta')
            model = metadata.get('modelId') if isinstance(metadata, dict) else None
            self.model = model if isinstance(model, str) and model.strip() else None
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
            call, name = event.get("toolCallId"), event.get("title")
            if not isinstance(call, str) or not call:
                self.tools_known = False
                return
            if not isinstance(name, str) or not name:
                name = "?"
                self.tools_known = False
            if call not in self.calls:
                self.ensure_turn()
                self.open_call(call, name, at)
                self.calls[call] = Call(at, ToolCall(name, None, None), slot=len(self.open["calls"]))
                self.open["calls"].append(call)
            elif self.calls[call].result.name != name:
                self.tools_known = False
            if event.get("status") in ("completed", "failed"):
                self.finish_call(call, at, event["status"] == "failed")
        elif kind == "tool_call_update":
            if event.get("status") in ("completed", "failed"):
                self.finish_call(event.get("toolCallId"), at, event["status"] == "failed")
        elif kind == "turn_completed":
            prompt = event.get("prompt_id")
            if isinstance(prompt, str) and prompt:
                if prompt in self.completed_prompts:
                    return
                self.completed_prompts.add(prompt)
            self.ensure_turn()
            self.end(at, event)
        elif kind == "retry_state":
            # A rate-limited retry names no window and no reset, and the program retries it: it does
            # not prove the subscription spent, so it is no wall. Grok's spent allowance is its meter.
            pass
        elif kind == "config_option_update":
            for option in event.get("configOptions", []):
                value = option.get("currentValue")
                if isinstance(value, str) and option.get("id") == "model":
                    self.model = value
                elif isinstance(value, str) and option.get("id") == "reasoning_effort":
                    self.effort = value
        elif kind not in ("plan", "available_commands_update", "session_info_update"):
            self.tools_known = False

    def finish_call(self, call, at, failed):
        item = self.calls.get(call) if isinstance(call, str) else None
        if item is None:
            self.tools_known = False
            return
        if item.finished:
            if item.result.failed != failed:
                self.tools_known = False
            return
        seconds = (at - item.started).total_seconds() if at and item.started else None
        item.result = ToolCall(item.result.name, seconds if seconds is not None and seconds >= 0 else None, failed)
        item.finished = True
        self.live_tools.pop(call, None)
        self.pending.pop(call, None)
        if item.turn is not None:
            previous = self.turns[item.turn]
            tools = list(previous.tools)
            tools[item.slot] = item.result
            revised = replace(previous, tools=tuple(tools))
            self.turns[item.turn] = revised
            self.completed_turns = [revised if turn.index == item.turn else turn for turn in self.completed_turns]

    def end(self, at, event=None):
        if self.open is None:
            return
        value = self.close_turn()
        event = event or {}
        calls = [self.calls[call] for call in value["calls"]]
        for item in calls:
            item.turn = len(self.turns)
        # Billing's modelUsage keys do not name the selected running model.
        model = usage_model(event.get("usage"))
        self.record_turn(Turn(
            len(self.turns), value["started"], at, value["input"], value["reply"], model,
            usage_tokens(event.get("usage")), tuple(item.result for item in calls), subagent_turns=None,
            timed=at is not None,
        ), at)

    def state(self):
        state = super().state()
        if self.open is None and self.live_tools:
            state = replace(state, pending_tools=None)  # ended root does not supply a missing tool result
        return replace(state, turn_open=self.open is not None if self.execution_seen else None)
