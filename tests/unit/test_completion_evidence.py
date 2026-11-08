"""Completion proof stays separate from accounting's inferred boundaries."""

import json

import pytest

from quarterdeck.harness import claude, codex
from .test_fresh_after_order import at


@pytest.mark.parametrize("program", [claude, codex])
@pytest.mark.parametrize("timestamp", [None, "invalid", at(40).isoformat()])
def test_only_a_timestamped_terminal_entry_proves_completion(tmp_path, program, timestamp):
    def entry(ms, kind, payload):
        row = {"timestamp": at(ms).isoformat(), "type": kind}
        row.update(payload)
        return row

    if program is claude:
        rows = [entry(10, "user", {"message": {"content": "Closing work"}}),
                entry(30, "user", {"message": {"content": "Later work"}})]
        final = {"type": "system", "subtype": "turn_duration", "timestamp": timestamp}
    else:
        rows = [entry(10, "event_msg", {"payload": {"type": "task_started"}}),
                entry(30, "event_msg", {"payload": {"type": "task_started"}})]
        final = {"type": "event_msg", "payload": {"type": "task_complete"}, "timestamp": timestamp}
    record = tmp_path / "synthetic.jsonl"
    record.write_text("".join(json.dumps(row) + "\n" for row in [*rows, final]))
    state, turns = program.read(record)
    assert not state.turn_open and len(turns) == 2, "accounting still retains both turns"
    assert state.completed_turns == ((turns[-1],) if timestamp == at(40).isoformat() else ())
