"""Captain-authorized ordinary native timestamp/receipt regressions.

These synthetic program rows exercise declared precision and unchanged legacy
readings. No native process or framework protection validation is performed.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from quarterdeck.harness import get
from quarterdeck.input_receipt import confirmed

FIXTURES = Path(__file__).parents[1] / "fixtures/receipt-time"
PAYLOAD = "qd > answer from synthetic · ans-a1b2\nReturn a synthetic greeting."
SECOND = datetime(2040, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


@pytest.mark.parametrize("name,expected", [("grok-same-second", "delivered"), ("grok-previous-second", None)])
def test_grok_native_whole_second_input_comparison(name, expected):
    adapter = get("grok")
    assert adapter.record_time_resolution == 1.0
    record = FIXTURES / f"{name}.jsonl"
    input_, = adapter.inputs(record)
    assert input_.text == PAYLOAD
    assert input_.at.microsecond == 0
    assert confirmed(adapter, record, PAYLOAD, SECOND.replace(microsecond=789123)) == expected


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_existing_fixture_readings_and_exact_receipt_times_unchanged(name):
    adapter = get(name)
    assert adapter.record_time_resolution == 0.0
    record = FIXTURES / f"{name}-fractional.jsonl"
    at = SECOND.replace(microsecond=456000)
    input_, = adapter.inputs(record)
    assert input_.text == PAYLOAD and input_.at == at and input_.kind == "accepted"
    state, turns = adapter.read(record)
    assert state.last_input == PAYLOAD and state.last_input_at == at
    assert state.turn_open is True and not turns
    assert confirmed(adapter, record, PAYLOAD, at) == "delivered"
    assert confirmed(adapter, record, PAYLOAD, at + timedelta(microseconds=1)) is None
