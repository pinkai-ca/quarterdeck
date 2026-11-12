"""Ordinary invented events for a source-backed draft, not native proof."""

import json

import pytest

from quarterdeck.harness.json_record import RecordError
from quarterdeck.harness.muse_record import Reader, first_timestamp, inputs, records


def row(kind, payload, second=0):
    return dict(payload_type=kind, payload=payload, recorded_at=f"2040-01-01T00:00:{second:02d}Z")


def run(action, second, **fields):
    return row("runtime.session", dict(kind="run", run_id="synthetic-run", event=dict(kind=action, **fields)), second)


def save(tmp_path, rows):
    path = tmp_path / "session.jsonl"
    path.write_text("".join(json.dumps(value) + "\n" for value in rows))
    return path


@pytest.mark.parametrize("retained", [False, True])
def test_acceptance_and_ended_turn_remain_distinct_from_queue(tmp_path, retained):
    rows = [row("runtime.user_intent.accepted", dict(prompt="Inspect the fictional sample.")),
            run("started", 1, prompt="Inspect the fictional sample."),
            run("assistant_tool_calls_committed", 2, tool_calls=[dict(name="shell")]),
            run("model_completed", 3, usage=dict(output_tokens=0)),
            run("assistant_message_committed", 4, text="The fictional sample is ready."),
            run("terminal", 5, terminal="completed")]
    if retained:
        rows = [dict(children=[dict(record_json=json.dumps(value)) for value in rows])]
    path = save(tmp_path, rows)
    state, turns = Reader.read(path)
    turn, = turns
    assert state.turn_open is False and state.turns == 1
    assert state.queued == () and state.pending_tools is None
    assert inputs(path)[0].text == turn.input
    assert turn.reply == "The fictional sample is ready." and turn.seconds == 4
    assert turn.tokens.written == 0 and not turn.tokens.complete
    assert turn.model is None and turn.subagent_turns is None and turn.recorded_cost is None
    assert [(tool.name, tool.seconds, tool.failed) for tool in turn.tools] == [("shell", None, None)]


def test_acceptance_alone_does_not_prove_turn_started_or_idle(tmp_path):
    path = save(tmp_path, [row("runtime.user_intent.accepted", dict(prompt="A fictional request."))])
    state, turns = Reader.read(path)
    assert state.turn_open is None and turns == []
    assert len(inputs(path)) == 1


def test_appended_terminal_is_counted_once(tmp_path):
    path = save(tmp_path, [run("started", 1, prompt="A fictional request.")])
    assert Reader.read(path)[0].turn_open is True
    with path.open("a") as stream:
        stream.write((json.dumps(run("terminal", 5, terminal="completed")) + "\n") * 2)
    state, turns = Reader.read(path)
    assert state.turn_open is False and len(turns) == 1 and not turns[0].tokens.complete


def test_input_diagnostics_keep_physical_rows_without_contents(tmp_path):
    path = save(tmp_path, [row("runtime.user_intent.accepted", {}),
                           dict(children=[dict(record_json="invalid fictional JSON")])])
    with path.open("a") as stream:
        stream.write('{"payload_type":')
    evidence = inputs(path)
    assert evidence == []
    assert [item["line"] for item in evidence.diagnostics()["examples"]] == [1, 2, 3]
    assert "fictional" not in json.dumps(evidence.diagnostics())


def test_foreign_and_empty_are_not_native_idle(tmp_path):
    path = save(tmp_path, [])
    assert Reader.read(path)[0].turn_open is None
    assert first_timestamp(path) is None
    path = save(tmp_path, [dict(type="other-program")])
    with pytest.raises(RecordError, match="another program"):
        Reader.read(path)
    with pytest.raises(RecordError, match="another program"):
        inputs(path)


def test_records_enumerate_only_root_store_depth(tmp_path):
    root = tmp_path / "data/muse/sessions/2040/01/01/synthetic-session/session.jsonl"
    child = root.parent / "subagent/synthetic-child/session.jsonl"
    for path in (root, child):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    assert records(tmp_path) == [root]
