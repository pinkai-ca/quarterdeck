"""Invented single-block records using the ordinary offline schema relay."""

import json
from datetime import datetime, timezone

import pytest

from quarterdeck.harness.muse_record import CollapsedInput, Reader, first_timestamp, inputs, session_id

MOMENT = 2208988800123456
SESSION = "synthetic-session"


def event(kind, payload, ordinal):
    return dict(schema_version=1, payload_schema_version=1, payload_type=kind,
                payload=payload, recorded_at=MOMENT + ordinal * 1_000_000,
                id=f"synthetic-event-{ordinal}", sequence=ordinal,
                stream=dict(kind="session", id=SESSION))


def accepted(text="A fictional request."):
    return event("runtime.user_intent.accepted", dict(intent_id="synthetic-intent",
                 source_session_id=SESSION, refill_blocks=[dict(kind="text", text=text)],
                 model_messages=[dict(content=[dict(kind="text", text=text)])]), 1)


def run(kind, ordinal, **fields):
    return event("runtime.session", dict(kind="run", run_id="synthetic-run",
                 source_run_record_id=f"synthetic-source-{ordinal}",
                 event=dict(kind=kind, **fields)), ordinal)


def save(tmp_path, rows):
    path = tmp_path / SESSION / "session.jsonl"
    path.parent.mkdir(exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def test_microseconds_exact_input_root_identity_and_task_are_distinct(tmp_path):
    rows = [accepted(), run("started", 2, prompt="A fictional request."),
            event("runtime.session", dict(kind="task", run_id="synthetic-run",
                  task_id="synthetic-task", event=dict(kind="started", task_id="synthetic-task")), 3),
            run("assistant_message_committed", 4, text="Fictional reply."),
            run("model_completed", 5, duration_ms=12, estimated_prompt_tokens=12,
                usage=dict(input_tokens=0, output_tokens=0, cached_tokens=0, reasoning_tokens=0)),
            run("terminal", 6, terminal="completed", turn_duration_ms=4000)]
    path = save(tmp_path, rows)
    state, turns = Reader.read(path)
    turn, = turns
    assert session_id(path) == SESSION and state.conversation == SESSION
    assert first_timestamp(path) == datetime(2040, 1, 1, 0, 0, 1, 123456, tzinfo=timezone.utc)
    assert inputs(path)[0].text == turn.input == "A fictional request."
    assert inputs(path)[0].at.microsecond == 123456
    assert turn.reply == "Fictional reply." and turn.end_reason == "completed"
    assert turn.seconds == 4 and state.turn_open is False
    assert not turn.tokens.complete and turn.subagent_turns is None
    assert state.pending_tools is None and state.work_id == "synthetic-run"


@pytest.mark.parametrize("change", ["many blocks", "different text", "foreign session", "boolean time", "new schema"])
def test_unproved_input_representation_does_not_produce_exact_receipt(tmp_path, change):
    row = accepted()
    if change == "many blocks":
        row["payload"]["refill_blocks"] *= 2
    elif change == "different text":
        row["payload"]["model_messages"][0]["content"][0]["text"] = "Another fictional text."
    elif change == "foreign session":
        row["payload"]["source_session_id"] = "synthetic-other-session"
    elif change == "boolean time":
        row["recorded_at"] = True
    else:
        row["schema_version"] = 2
    evidence = inputs(save(tmp_path, [row]))
    assert evidence == [] and evidence.diagnostics()["total"] == 1


def test_materialization_uses_explicit_links_without_equal_intent_and_run_ids(tmp_path):
    before, started = accepted(), run("started", 2, prompt="A fictional request.")
    outcome = dict(kind="top_level_turn_started", run_id="synthetic-run",
                   run_started_session_record_id=started["id"],
                   run_started_source_record_id=started["payload"]["source_run_record_id"])
    materialized = event("runtime.user_intent.materialized", dict(intent_id="synthetic-intent",
                         source_session_id=SESSION, envelope_record_id=before["id"],
                         envelope_sequence=before["sequence"], outcome=outcome), 3)
    path = save(tmp_path, [before, started, materialized])
    assert Reader(path).materialization("synthetic-intent") == "top_level_turn_started"
    outcome["run_started_source_record_id"] = started["id"]
    path = save(tmp_path, [before, started, materialized])
    assert Reader(path).materialization("synthetic-intent") is None
    assert inputs(path)[0].kind == "accepted"  # no queue or active-run disposition inferred


def test_conflicting_streams_do_not_establish_a_conversation_or_join_time(tmp_path):
    other = run("started", 2, prompt="A fictional request.")
    other["stream"]["id"] = "synthetic-other-session"
    path = save(tmp_path, [accepted(), other])
    assert session_id(path) is None and first_timestamp(path) is None
    assert Reader.read(path)[0].conversation is None


def test_cancelled_terminal_ends_only_the_named_run(tmp_path):
    path = save(tmp_path, [run("started", 1, prompt="A fictional request."),
                          run("terminal", 2, terminal="cancelled", reason="A fictional cancellation.")])
    state, turns = Reader.read(path)
    assert state.turn_open is False and turns[0].end_reason == "cancelled"
    assert not turns[0].tokens.complete and turns[0].failure is None


def test_collapsed_input_keeps_model_text_and_original_count_separate(tmp_path):
    original = "first \nsecond\n\n "
    row = accepted(original)
    row["payload"]["refill_blocks"][0]["text"] = f"[Pasted Content {len(original)} chars]"
    model = "first \nsecond"
    row["payload"]["model_messages"][0]["content"][0]["text"] = model
    path = save(tmp_path, [row])
    item, = inputs(path)
    assert isinstance(item, CollapsedInput)
    assert item.text == "first \nsecond" and item.characters == str(len(original))
    assert item.at.microsecond == 123456 and item.kind == "accepted"


def test_unfolded_input_keeps_boundary_and_internal_whitespace_verbatim(tmp_path):
    original = " first \nsecond\n"
    item, = inputs(save(tmp_path, [accepted(original)]))
    assert type(item) is not CollapsedInput and item.text == original


def test_muse_collapsed_receipt_uses_model_text_and_the_original_form_count(tmp_path):
    from quarterdeck.harness import get
    from quarterdeck.input_receipt import confirmed

    original = "first \nsecond\n"
    row = accepted(original)
    row["payload"]["refill_blocks"][0]["text"] = f"[Pasted Content {len(original)} chars]"
    row["payload"]["model_messages"][0]["content"][0]["text"] = original[:-1]
    path = save(tmp_path, [row])
    harness = get("muse")
    item, = inputs(path)
    assert harness.expected_input(item, original) == "first \nsecond"
    assert confirmed(harness, path, original, first_timestamp(path)) == "delivered"


def test_muse_unfolded_receipt_uses_the_same_single_final_newline_projection(tmp_path):
    from quarterdeck.harness import get
    from quarterdeck.input_receipt import confirmed

    original = "first \nsecond\n"
    path = save(tmp_path, [accepted(original[:-1])])
    harness = get("muse")
    item, = inputs(path)
    assert type(item) is not CollapsedInput
    assert harness.expected_input(item, original) == "first \nsecond"
    assert confirmed(harness, path, original, first_timestamp(path)) == "delivered"


def test_existing_non_muse_receipt_keeps_its_boundary_whitespace_compatibility(tmp_path, monkeypatch):
    from quarterdeck.harness import get
    from quarterdeck.harness.types import Input
    from quarterdeck.input_receipt import confirmed

    harness = get("fake")
    at = datetime(2040, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(harness, "inputs", lambda record: [Input(" A fictional record. ", at)])
    assert confirmed(harness, tmp_path / "unused-record", "\nA fictional record.\n", at) == "delivered"
