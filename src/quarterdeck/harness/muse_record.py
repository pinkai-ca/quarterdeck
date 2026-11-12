"""Muse event records; source-backed draft pending current-version echo measurement.

No record shape in this module establishes native permissions or isolation.
Unknown fields retain unknown measurements instead of borrowing old defaults.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from .json_record import JsonLinesReader, RecordError, object_lines, records_matching, when
from .types import Input, Inputs, Tokens, ToolCall, Turn


def events(row: dict, skip=None):
    """Native flat rows or one retained-frame transaction, without rewriting it.

    Diagnostics deliberately contain neither event contents nor JSON errors.
    The historical retained-frame envelope is not current-version proof.
    """
    if isinstance(row.get("payload_type"), str):
        if isinstance(row.get("payload"), dict):
            yield row
        elif skip:
            skip("payload is not an object")
        return
    children = row.get("children")
    if not isinstance(children, list):
        if skip:
            skip("unrecognized envelope")
        return
    for child in children:
        raw = child.get("record_json") if isinstance(child, dict) else None
        try:
            value = json.loads(raw) if isinstance(raw, str) else None
        except ValueError:
            value = None
        if (isinstance(value, dict) and isinstance(value.get("payload_type"), str)
                and isinstance(value.get("payload"), dict)):
            yield value
        elif skip:
            skip("unreadable retained event")


def native(row: dict) -> bool:
    return any(event["payload_type"].startswith(("runtime.", "run.", "session.", "turn.", "task."))
               for event in events(row))


def records(home: Path) -> list[Path]:
    # This pattern excludes native subagent folders structurally.
    return records_matching(home, "data/muse/sessions/*/*/*/*/session.jsonl")


def first_timestamp(record: Path):
    """Creation comes from the first native event, never the file's latest mtime."""
    for _, row in object_lines(record):
        for event in events(row):
            if native(event):
                return when(event.get("recorded_at"))
    return None


def session_id(record: Path) -> str | None:
    # The store's directory identity is historical until validated at 1.4.15.
    if record.name != "session.jsonl" or not record.parent.name:
        return None
    return record.parent.name


def inputs(record: Path) -> Inputs:
    """Exact accepted text; materialization is not guessed to be a queue.

    Accepted intent is native acceptance evidence. Outcome details need the
    current-version materialization schema before they can establish more.
    """
    result = Inputs()
    rows = object_lines(record, result.skip)
    if rows and not any(native(row) for _, row in rows):
        raise RecordError("no Muse entries in this record: another program's file")
    for number, row in rows:
        for event in events(row, lambda reason: result.skip(number, "envelope", reason)):
            if event["payload_type"] != "runtime.user_intent.accepted":
                continue
            text, at = event["payload"].get("prompt"), when(event.get("recorded_at"))
            if not isinstance(text, str) or at is None:
                result.skip(number, "runtime.user_intent.accepted", "text or timestamp unreadable")
            else:
                result.append(Input(text, at))
    return result


class Reader(JsonLinesReader):
    label = "Muse"

    @classmethod
    def identifies(cls, row: dict) -> bool:
        return native(row)

    def __init__(self, record: Path, snapshot=None):
        self.execution_seen = False
        self.ended_runs = set()
        self.run_id = None
        super().__init__(record, snapshot)

    def take(self, row: dict) -> None:
        for event in events(row, lambda reason: self.unknown_tools(reason)):
            self.take_event(event)

    def take_event(self, row: dict) -> None:
        kind, payload, at = row["payload_type"], row["payload"], when(row.get("recorded_at"))
        if kind == "runtime.user_intent.accepted":
            text = payload.get("prompt")
            if isinstance(text, str):
                self.last_input, self.last_input_at = text, at
            return
        if kind != "runtime.session":
            # Configuration, tooling, materialization and new payloads require
            # native schemas; absence of recognized tool evidence is unknown.
            self.tools_known = False
            return
        if payload.get("kind") != "run":
            self.tools_known = False
            return
        event, run = payload.get("event"), payload.get("run_id")
        if not isinstance(event, dict) or not isinstance(run, str) or not run:
            self.tools_known = False
            return
        action = event.get("kind")
        if run in self.ended_runs:
            return
        if action == "started":
            if self.open is not None:
                if self.run_id == run:
                    return
                self.end(None)  # retain incomplete work, never invent completion
            prompt = event.get("prompt")
            self.execution_seen, self.run_id = True, run
            self.start_turn(dict(started=at, input=prompt if isinstance(prompt, str) else "",
                                 reply=[], tools=[], tokens=Tokens.unknown()))
            return
        if self.open is None or self.run_id != run:
            self.tools_known = False
            return
        if action == "assistant_message_committed":
            if isinstance(event.get("text"), str):
                self.open["reply"].append(event["text"])
        elif action == "assistant_tool_calls_committed":
            calls = event.get("tool_calls")
            if not isinstance(calls, list):
                self.tools_known = False
                return
            for call in calls:
                name = call.get("name") if isinstance(call, dict) else None
                self.open["tools"].append(ToolCall(name if isinstance(name, str) and name else "?", None, None))
            self.tools_known = False  # names alone cannot prove outstanding calls
        elif action == "model_completed":
            usage = event.get("usage")
            count = usage.get("output_tokens") if isinstance(usage, dict) else None
            if type(count) is int and count >= 0:
                previous = self.open["tokens"].written
                # No evidence yet establishes other token classes or input totals.
                self.open["tokens"] = Tokens(written=previous + count, unmeasured=1)
        elif action == "terminal":
            if isinstance(event.get("terminal"), str) and event["terminal"]:
                self.ended_runs.add(run)
                self.end(at)
            else:
                self.tools_known = False
        else:
            self.tools_known = False

    def end(self, at):
        value = self.close_turn()
        self.record_turn(Turn(len(self.turns), value["started"], at, value["input"],
                              "\n".join(value["reply"]), None, value["tokens"],
                              tuple(value["tools"]), subagent_turns=None, timed=at is not None), at)

    def state(self):
        return replace(super().state(), turn_open=self.open is not None if self.execution_seen else None)
