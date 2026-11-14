"""Synthetic Muse root-turn boundaries and native allowance units; no program calls."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from quarterdeck.harness.muse_record import Reader
from quarterdeck.harness.muse_usage import decode
from quarterdeck.harness.muse_screen import read


def record(tmp_path, events):
    path = tmp_path / "synthetic-session" / "session.jsonl"
    path.parent.mkdir()
    rows = [dict(schema_version=1, payload_schema_version=1,
                 id=f"synthetic-event-{number}", sequence=number,
                 recorded_at=1893456000000000 + number * 1000000,
                 stream={"kind": "session", "id": "synthetic-session"},
                 payload_type="runtime.session",
                 payload={"kind": "run", "run_id": "synthetic-run", "event": event})
            for number, event in enumerate(events)]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def test_reply_and_reminder_completion_leave_the_root_turn_open(tmp_path):
    path = record(tmp_path, [
        {"kind": "started", "prompt": "hello"},
        {"kind": "started", "task_id": "synthetic-reminder"},
        {"kind": "completed", "task_id": "synthetic-reminder"},
        {"kind": "model_completed", "usage": {"input_tokens": 0, "output_tokens": 0}},
        {"kind": "assistant_message_committed", "text": "reply"},
    ])
    state, turns = Reader.read(path)
    assert state.turn_open is True
    assert state.turns == 0
    assert turns == []


def test_the_root_terminal_finishes_the_original_prompt_after_reminders(tmp_path):
    path = record(tmp_path, [
        {"kind": "started", "prompt": "hello"},
        {"kind": "started", "task_id": "synthetic-reminder"},
        {"kind": "completed", "task_id": "synthetic-reminder"},
        {"kind": "model_completed", "model": "synthetic-model"},
        {"kind": "assistant_message_committed", "text": "reply"},
        {"kind": "terminal", "terminal": "completed", "reason": None},
    ])
    state, turns = Reader.read(path)
    assert state.turn_open is False
    assert state.turns == 1
    assert len(turns) == 1
    assert turns[0].input == "hello"
    assert turns[0].reply == "reply"
    assert turns[0].ended == datetime(2030, 1, 1, 0, 0, 5, tzinfo=timezone.utc)
    assert turns[0].model == state.model == "synthetic-model"


def test_native_milliseconds_and_percentages_keep_their_bucket_names():
    value = decode(json.dumps({"schemaVersion": 1, "allowance": {
        "asOfMs": 1893456000000,
        "window": {"usedPercent": 20, "resetsAtMs": 1893459600000},
        "weekly": {"usedPercent": 40, "resetsAtMs": 1894060800000},
    }, "hint": None, "reason": None}))
    assert value.error is None
    assert [(window.name, window.used) for window in value.windows] == [("window", .2), ("weekly", .4)]
    assert value.windows[0].resets_at == datetime(2030, 1, 1, 1, tzinfo=timezone.utc)
    assert value.windows[1].resets_at == datetime(2030, 1, 8, tzinfo=timezone.utc)


@pytest.mark.parametrize("name,working,queued,box", [
    ("idle-after-turn", False, 0, ""),
    ("draft-echo", False, 0, "First measurement prompt: reply with the same sentence."),
    ("working-echo", True, 0, ""),
    ("queued-echo", True, 1, ""),
    ("working-wonder", True, 0, ""),
])
def test_the_measured_composer_keeps_activity_and_draft_separate(name, working, queued, box):
    fixture = Path(__file__).parents[1] / "fixtures/muse/1.4.20" / f"{name}-120x40.ansi"
    screen = read(fixture.read_text())
    assert screen.unknown is None
    assert screen.interruption is None
    assert screen.working is working
    assert screen.queued == queued
    assert screen.box == box
