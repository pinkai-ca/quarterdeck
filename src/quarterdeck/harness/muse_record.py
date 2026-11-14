"""Muse event records from safe current-version echo schemas.

No record shape in this module establishes native permissions or isolation.
Unknown fields retain unknown measurements instead of borrowing old defaults.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .json_record import JsonLinesReader, RecordError, object_lines, records_matching, when
from .types import Input, Inputs, Tokens, ToolCall, Turn

FOLDED_INPUT = re.compile(r"\[Pasted Content ([1-9][0-9]*) chars\]")


@dataclass(frozen=True)
class CollapsedInput(Input):
    """Native model text and the original marker's count; no recorded text rewrite."""

    characters: str = ""


def event_time(value):
    """Current native microseconds; ISO remains readable in historical fixtures."""
    if type(value) is int:
        try:
            return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=value)
        except OverflowError:
            return None
    return when(value)


def accepted_text(payload: dict) -> str | None:
    """The model's single exact text block, with its native refill evidence.

    No separator or multi-block rendering rule is established.
    """
    blocks, messages = payload.get("refill_blocks"), payload.get("model_messages")
    if not isinstance(blocks, list) or len(blocks) != 1 or not isinstance(messages, list) or len(messages) != 1:
        return None
    if not isinstance(blocks[0], dict) or not isinstance(messages[0], dict):
        return None
    content = messages[0].get("content")
    if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict):
        return None
    text, model = blocks[0].get("text"), content[0].get("text")
    if (blocks[0].get("kind") != "text" or content[0].get("kind") != "text"
            or not isinstance(text, str) or not isinstance(model, str)):
        return None
    if text == model:
        return model
    marker = FOLDED_INPUT.fullmatch(text)
    # This is the text actually given to the model, not a reconstruction of
    # the original draft. The adapter's receipt projection owns the measured
    # single-final-newline transform and checks the original marker count.
    return model if marker is not None else None


def collapsed_count(payload: dict) -> str | None:
    model = accepted_text(payload)
    blocks = payload.get("refill_blocks")
    if model is None or not isinstance(blocks, list) or len(blocks) != 1:
        return None
    text = blocks[0].get("text")
    marker = FOLDED_INPUT.fullmatch(text) if isinstance(text, str) else None
    return marker[1] if marker is not None and text != model else None


def session_identity(record: Path) -> str | None:
    identities = set()
    for _, row in object_lines(record):
        for event in events(row):
            stream = event.get("stream")
            if isinstance(stream, dict) and stream.get("kind") == "session":
                identity = stream.get("id")
                if not isinstance(identity, str) or not identity:
                    return None
                identities.add(identity)
    return record.parent.name if identities == {record.parent.name} else None


def input_text(row: dict) -> str | None:
    payload = row["payload"]
    if isinstance(row.get("recorded_at"), str):
        return accepted_text(payload) if "refill_blocks" in payload else payload.get("prompt")
    stream = row.get("stream")
    if (type(row.get("schema_version")) is not int or row["schema_version"] != 1 or not isinstance(stream, dict)
            or stream.get("kind") != "session" or not isinstance(stream.get("id"), str)
            or payload.get("source_session_id") != stream["id"]):
        return None
    return accepted_text(payload)


def events(row: dict, skip=None):
    """Native flat rows or one retained-frame transaction, without rewriting it.

    Diagnostics deliberately contain neither event contents nor JSON errors.
    Current echo proved retained frames for native permission events; support
    for other retained payloads is structural, not a new native measurement.
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
    if session_identity(record) is None:
        return None
    for _, row in object_lines(record):
        for event in events(row):
            if native(event):
                return event_time(event.get("recorded_at"))
    return None


def session_id(record: Path) -> str | None:
    if record.name != "session.jsonl" or not record.parent.name:
        return None
    return session_identity(record)


def inputs(record: Path) -> Inputs:
    """Exact accepted text; materialization is not guessed to be a queue.

    Accepted intent is native acceptance evidence. Explicit materialization
    linkage is available separately and never reclassifies acceptance as queue.
    """
    result = Inputs()
    rows = object_lines(record, result.skip)
    if rows and not any(native(row) for _, row in rows):
        raise RecordError("no Muse entries in this record: another program's file")
    for number, row in rows:
        for event in events(row, lambda reason: result.skip(number, "envelope", reason)):
            if event["payload_type"] != "runtime.user_intent.accepted":
                continue
            text = input_text(event)
            at = event_time(event.get("recorded_at"))
            if not isinstance(text, str) or at is None:
                result.skip(number, "runtime.user_intent.accepted", "text or timestamp unreadable")
            else:
                count = collapsed_count(event["payload"])
                result.append(CollapsedInput(text, at, characters=count) if count is not None else Input(text, at))
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
        self.conversation = None
        self.input_id = None
        self.accepted = {}
        self.started = {}
        self.materialized = {}
        super().__init__(record, snapshot)
        self.conversation = session_identity(record)

    def take(self, row: dict) -> None:
        for event in events(row, lambda reason: self.unknown_tools(reason)):
            self.take_event(event)

    def take_event(self, row: dict) -> None:
        kind, payload, at = row["payload_type"], row["payload"], event_time(row.get("recorded_at"))
        if kind == "runtime.user_intent.accepted":
            text = input_text(row)
            if isinstance(text, str):
                self.last_input, self.last_input_at = text, at
            intent = payload.get("intent_id")
            if isinstance(intent, str) and intent:
                self.input_id = intent
                self.accepted[intent] = row
            return
        if kind == "runtime.user_intent.materialized":
            intent = payload.get("intent_id")
            if isinstance(intent, str) and intent:
                self.materialized[intent] = row
            return
        if kind != "runtime.session":
            # Configuration, tooling and other new payloads require
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
            prompt = event.get("prompt")
            if not isinstance(prompt, str) or "task_id" in event:
                # Native reminder tasks also emit started/completed. They do
                # not replace the parent prompt or end its root turn.
                self.tools_known = False
                return
            if self.open is not None:
                if self.run_id == run:
                    return
                self.end(None)  # retain incomplete work, never invent completion
            self.execution_seen, self.run_id = True, run
            self.started[run] = row
            self.start_turn(dict(started=at, input=prompt if isinstance(prompt, str) else "",
                                 reply=[], tools=[], tokens=Tokens.unknown(), model=None, model_seen=False))
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
            model = event.get("model")
            if isinstance(model, str) and model:
                self.model = model
                if not self.open["model_seen"]:
                    self.open["model"] = model
                elif self.open["model"] != model:
                    self.open["model"] = None
                self.open["model_seen"] = True
            # Echo zeros are not provider quantities. Cache/reasoning overlap
            # is unmeasured; never turn these fields into billed token totals.
            self.open["tokens"] = Tokens.unknown()
        elif action == "terminal":
            self.ended_runs.add(run)
            reason, terminal = event.get("reason"), event.get("terminal")
            self.end(at, reason if isinstance(reason, str) else
                     terminal if isinstance(terminal, str) else None)
        else:
            self.tools_known = False

    def end(self, at, reason=None):
        value = self.close_turn()
        self.record_turn(Turn(len(self.turns), value["started"], at, value["input"],
                              "\n".join(value["reply"]), value["model"], value["tokens"],
                              tuple(value["tools"]), subagent_turns=None, timed=at is not None,
                              end_reason=reason), at)

    def materialization(self, intent: str) -> str | None:
        """Positive top-level-start linkage, never a guess from equal intent/run ids."""
        accepted, materialized = self.accepted.get(intent), self.materialized.get(intent)
        if accepted is None or materialized is None:
            return None
        left, right = accepted["payload"], materialized["payload"]
        outcome = right.get("outcome")
        if not isinstance(outcome, dict) or outcome.get("kind") != "top_level_turn_started":
            return None
        started = self.started.get(outcome.get("run_id"))
        stream = accepted.get("stream")
        if (started is None or not isinstance(stream, dict) or stream.get("kind") != "session"
                or materialized.get("stream") != stream or started.get("stream") != stream
                or type(accepted.get("sequence")) is not int or not isinstance(accepted.get("id"), str)
                or right.get("envelope_record_id") != accepted["id"]
                or right.get("envelope_sequence") != accepted["sequence"]
                or left.get("source_session_id") != stream.get("id")
                or right.get("source_session_id") != stream.get("id")
                or outcome.get("run_started_session_record_id") != started.get("id")
                or not isinstance(started.get("payload", {}).get("source_run_record_id"), str)
                or outcome.get("run_started_source_record_id") != started["payload"]["source_run_record_id"]):
            return None
        return "top_level_turn_started"

    def state(self):
        return replace(super().state(), turn_open=self.open is not None if self.execution_seen else None,
                       conversation=self.conversation, input_id=self.input_id, work_id=self.run_id)
