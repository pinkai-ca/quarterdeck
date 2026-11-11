"""Synthetic native-format examples; no account, credential or captured runtime data."""

import json
from datetime import datetime, timezone
from pathlib import Path

from quarterdeck.harness.grok_record import Reader, first_timestamp, inputs, session_id
from quarterdeck.harness.grok_usage import decode

FIXTURES = Path(__file__).parents[1] / "fixtures" / "grok" / "1.0.77"


def test_ended_turn_stream_and_tool_duration():
    record = FIXTURES / "turn.jsonl"
    state, turns = Reader.read(record)
    turn, = turns
    assert state.turn_open is False
    assert turn.input == "Count the sample rows."
    assert turn.reply == "Three rows."
    assert turn.seconds == 2
    assert turn.tokens.read_uncached == 60
    assert turn.tokens.read_cached == 20
    assert turn.tokens.written == 12
    assert [(tool.name, tool.seconds, tool.failed) for tool in turn.tools] == [("Shell", 1, False)]
    assert turn.subagent_turns is None
    assert state.completed_turns == (turn,)


def test_native_input_and_origin():
    record = FIXTURES / "turn.jsonl"
    evidence, = inputs(record)
    assert evidence.text == "Count the sample rows."
    assert evidence.at == first_timestamp(record)
    assert session_id(record) == "synthetic-session"


def test_weekly_billing_fraction_and_reset():
    at = datetime(2040, 1, 2, tzinfo=timezone.utc)
    usage = decode(json.loads((FIXTURES / "billing.json").read_text()), at)
    window, = usage.windows
    assert window.name == "7d"
    assert window.used == .375
    assert window.resets_at == datetime(2040, 1, 8, tzinfo=timezone.utc)
    assert usage.error is None


def test_absent_percentage_stays_unknown():
    payload = json.loads((FIXTURES / "billing.json").read_text())
    del payload["config"]["creditUsagePercent"]
    usage = decode(payload, datetime(2040, 1, 2, tzinfo=timezone.utc))
    assert usage.windows[0].used is None
    assert usage.windows[0].resets_at is not None
    assert usage.error == "billing percentage absent or unreadable"


def test_live_tier_is_separate_from_weekly_measurement():
    payload = json.loads((FIXTURES / "billing.json").read_text())
    payload["subscription_tier"] = "SuperGrok"
    usage = decode(payload, datetime(2040, 1, 2, tzinfo=timezone.utc))
    assert usage.diagnostics == ("native subscription tier: SuperGrok",)
    assert usage.windows[0].used == .375


def test_explicit_zero_is_a_measured_weekly_window():
    payload = json.loads((FIXTURES / "billing.json").read_text())
    payload["config"]["creditUsagePercent"] = 0
    usage = decode(payload, datetime(2040, 1, 2, tzinfo=timezone.utc))
    assert usage.windows[0].used == 0
    assert usage.error is None
