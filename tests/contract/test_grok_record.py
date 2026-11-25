"""Invented ACP rows for measured 1.0.77 shapes; no native process or live data."""

import json
from pathlib import Path

import pytest

from quarterdeck.harness.grok_record import Reader, inputs, usage_model, usage_tokens
from quarterdeck.harness.json_record import RecordError
from quarterdeck.ledger import Ledger
from quarterdeck.post import Store

FIXTURE = Path(__file__).parents[1] / "fixtures/grok/1.0.77/lifecycle.jsonl"


def synthetic_record(tmp_path, rows=None):
    path = tmp_path / "updates.jsonl"
    path.write_text(FIXTURE.read_text() if rows is None else "".join(json.dumps(row) + "\n" for row in rows))
    return path


def test_consumed_followup_and_cancelled_invocation_are_retained_once():
    state, turns = Reader.read(FIXTURE)
    assert state.turn_open is False and len(turns) == 2
    assert state.pending_tools is None
    assert [item.text for item in inputs(FIXTURE)] == [turn.input for turn in turns]
    assert turns[0].ended == turns[1].started
    assert [(tool.name, tool.seconds, tool.failed) for turn in turns for tool in turn.tools] == [
        ("run_terminal_command", 2, False), ("run_terminal_command", None, None)]
    assert all(turn.model == "grok-4.7-build" for turn in turns)
    assert state.model is None, 'billing keys alone do not establish the running model'
    assert all((turn.tokens.read_cached, turn.tokens.read_uncached, turn.tokens.written) == (20, 60, 12)
               for turn in turns)
    assert all(turn.subagent_turns is None for turn in turns)  # numTurns is not a child count
    assert all(turn.cost is None for turn in turns)  # launch provenance decides admission
    assert turns[0].recorded_cost == pytest.approx(0.0000123456)
    assert turns[1].recorded_cost is None


@pytest.mark.parametrize("terminal,failed", [("completed", False), ("failed", True)])
def test_explicit_terminal_tool_updates_are_not_counted_twice(tmp_path, terminal, failed):
    rows = [json.loads(line) for line in FIXTURE.read_text().splitlines()][:8]
    for row in rows:
        update = row["params"]["update"]
        if update.get("status") == "completed":
            update["status"] = terminal
    turn, = Reader.read(synthetic_record(tmp_path, rows))[1]
    assert len(turn.tools) == 1
    assert turn.tools[0].seconds == 2 and turn.tools[0].failed is failed


def test_ledger_keeps_cancelled_invocation_unknown_and_ingests_once(tmp_path):
    path = synthetic_record(tmp_path)
    turns = Reader.read(path)[1]
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    try:
        store = Store(tmp_path / "post-runtime")
        assert ledger.ingest("synthetic-seat", "synthetic-pool", path, turns, store) == 2
        assert ledger.ingest("synthetic-seat", "synthetic-pool", path, turns, store) == 0
        assert ledger.db.execute("SELECT tool_calls, tool_failures FROM turns ORDER BY turn").fetchall() == [(1, 0), (1, None)]
        assert ledger.tools() == [("run_terminal_command", 2, 2.0, 0, 1, 1)]
    finally:
        ledger.db.close()


def test_late_terminal_evidence_revises_one_call_without_an_extra_turn(tmp_path):
    path = synthetic_record(tmp_path)
    before = Reader.read(path)[1]
    assert before[1].tools[0].seconds is None
    row = {"timestamp": 2208988809, "method": "session/update", "params": {
        "sessionId": "synthetic-lifecycle", "update": {"sessionUpdate": "tool_call_update",
        "toolCallId": "synthetic-call-two", "status": "failed"}}}
    with path.open("a") as stream:
        stream.write(json.dumps(row) + "\n" + json.dumps(row) + "\n")
    state, turns = Reader.read(path)
    assert state.turn_open is False and len(turns) == 2
    assert len(turns[1].tools) == 1 and turns[1].tools[0].seconds == 4
    assert turns[1].tools[0].failed is True
    assert state.completed_turns == tuple(turns)
    assert state.pending_tools == ()


@pytest.mark.parametrize("value", [{}, {"other": 7}, [], {"inputTokens": True},
    {"inputTokens": 10, "outputTokens": 2},
    {"inputTokens": 10, "cachedReadTokens": 11, "cacheCreationTokens": 0, "outputTokens": 2},
    {"inputTokens": 10, "cachedReadTokens": 1, "cacheCreationTokens": 0, "outputTokens": -1},
    {"inputTokens": 10, "cachedReadTokens": 1, "cacheCreationTokens": 2, "outputTokens": 2}])
def test_unknown_usage_does_not_become_measured_zero(value):
    assert usage_tokens(value).unmeasured == 1


@pytest.mark.parametrize("value", [{}, {"modelUsage": {}}, {"modelUsage": {"a": {}, "b": {}}},
    {"modelUsage": {"a": None}}])
def test_absent_or_mixed_model_is_not_guessed(value):
    assert usage_model(value) is None


def test_empty_and_foreign_records_have_distinct_results(tmp_path):
    path = synthetic_record(tmp_path, [])
    state, turns = Reader.read(path)
    assert state.turn_open is None and turns == [] and inputs(path) == []
    path.write_text('{"type":"unrelated-program"}\n')
    with pytest.raises(RecordError, match="another program"):
        Reader.read(path)
    with pytest.raises(RecordError, match="another program"):
        inputs(path)


def test_malformed_and_incomplete_inputs_keep_physical_line_diagnostics(tmp_path):
    rows = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
    missing = {"timestamp": 2208988801, "method": "session/update", "params": {
        "sessionId": "synthetic-lifecycle", "update": {"sessionUpdate": "user_message_chunk"}}}
    bad_time = {**missing, "timestamp": "not-a-time", "params": {**missing["params"],
        "update": {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "Unknown time."}}}}
    path = tmp_path / "updates.jsonl"
    path.write_text(json.dumps(rows[0]) + "\n" + json.dumps(missing) + "\n" + json.dumps(bad_time) + '\n{"method":')
    evidence = inputs(path)
    assert [item.text for item in evidence] == ["Inspect the fictional sample."]
    assert [item["line"] for item in evidence.diagnostics()["examples"]] == [2, 3, 4]
    assert Reader.read(path)[0].pending_tools is None


def test_unknown_native_updates_preserve_known_turns_and_uncertainty(tmp_path):
    rows = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
    rows.append({"timestamp": 2208988810, "method": "session/update", "params": {
        "sessionId": "synthetic-lifecycle", "update": {"sessionUpdate": "future_update"}}})
    state, turns = Reader.read(synthetic_record(tmp_path, rows))
    assert len(turns) == 2 and state.turn_open is False and state.pending_tools is None


def test_known_call_without_a_name_still_counts_as_an_unknown_invocation(tmp_path):
    rows = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
    rows[9]["params"]["update"].pop("title")
    state, turns = Reader.read(synthetic_record(tmp_path, rows))
    assert turns[1].tools[0].name == "?"
    assert turns[1].tools[0].seconds is None and turns[1].tools[0].failed is None
    assert state.pending_tools is None


@pytest.mark.parametrize("changes", [
    {},
    {"costUsdTicks": 0},
    {"costUsdTicks": -1},
    {"costUsdTicks": True},
    {"costUsdTicks": "123456"},
    {"costUsdTicks": 123456.0},
    {"costUsdTicks": 2**63},
    {"costUsdTicks": None},
    {"costUsdTicks": 123456, "costIsPartial": True},
    {"costUsdTicks": 123456, "usageIsIncomplete": True},
    {"costUsdTicks": 123456, "usageIsIncomplete": "false"},
    {"modelUsage": {"a": {"costUsdTicks": 123456}}},
    {"costUsdTicks": 123456, "modelUsage": {"a": {"costUsdTicks": 123455}}},
    {"costUsdTicks": 123456, "modelUsage": {"a": {"costUsdTicks": 123456}, "b": {}}},
    {"costUsdTicks": 123456, "modelUsage": {"a": {"costUsdTicks": 123456, "costIsPartial": True}}},
])
def test_unproved_native_costs_remain_unknown(tmp_path, changes):
    rows = [json.loads(line) for line in FIXTURE.read_text().splitlines()][:8]
    usage = rows[-1]["params"]["update"]["usage"]
    del usage["costUsdTicks"]
    usage.update(changes)
    turn, = Reader.read(synthetic_record(tmp_path, rows))[1]
    assert turn.recorded_cost is None and turn.cost is None
    assert turn.tokens.complete


def test_native_completion_total_is_per_prompt_and_replays_agree(tmp_path):
    rows = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
    for row in rows[-2:]:
        event = row["params"]["update"]
        event["stop_reason"] = "end_turn"
        event["usage"].update(costUsdTicks=3_000_000_001, costIsPartial=False, usageIsIncomplete=False,
                              modelUsage={"a": {"costUsdTicks": 3_000_000_000}, "b": {"costUsdTicks": 1}})
    state, turns = Reader.read(synthetic_record(tmp_path, rows))
    assert state.completed_turns == tuple(turns)
    assert [turn.recorded_cost for turn in turns] == pytest.approx([0.0000123456, 0.3000000001])
    assert turns[1].model is None
