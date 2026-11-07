"""The record readers, on records the fake harness writes in each real program's shape."""

from __future__ import annotations

from pathlib import Path

import pytest

from quarterdeck.fakerecord import AntigravityShape, ClaudeShape, CodexShape, tokens_for
from quarterdeck.harness import antigravity, claude, codex
from quarterdeck.harness.base import Tokens

SHAPES = [(ClaudeShape, claude), (CodexShape, codex), (AntigravityShape, antigravity)]


@pytest.mark.parametrize("shape,reader", SHAPES, ids=["claude", "codex", "antigravity"])
def test_an_ended_turn_sums_its_steps(tmp_path: Path, shape, reader):
    record = shape(tmp_path, Path("/work/here"), "sess-1", "fake-1")
    record.begin("hello there", "fake-1", "high")
    record.tool("Bash", "echo hi", "hi", False)
    record.tool("Bash", "false", "", True)
    record.end("done: hello there")
    turns = reader.turns(record.path)
    assert len(turns) == 1
    turn = turns[0]
    assert turn.input == "hello there" and turn.reply == "done: hello there" and turn.model == "fake-1"
    assert [t.name for t in turn.tools] == (["shell", "shell"] if reader is codex else ["Bash", "Bash"])
    assert [t.failed for t in turn.tools] == [False, True]
    assert all(t.seconds is not None and t.seconds >= 0 for t in turn.tools)
    expected = tokens_for("hello there", "done: hello there", 0)
    steps = 3  # two tool calls and the reply each carry a usage
    assert turn.tokens.read_cached == expected["read_cached"] * steps
    assert turn.tokens.written > 0
    assert turn.tokens.complete, "every step of the turn carried its usage"
    assert turn.tokens.total == turn.tokens.read_cached + turn.tokens.read_uncached + turn.tokens.cache_written + turn.tokens.written
    if reader is antigravity:
        assert turn.tokens.cache_written == 0 and turn.subagent_turns is None, "a class the provider never reports is zero"
    state = reader.state(record.path)
    assert not state.turn_open and state.turns == 1 and state.model == "fake-1"
    assert state.effort == (None if reader is antigravity else "high")
    assert state.last_input == "hello there" and state.open_tool is None


@pytest.mark.parametrize("shape,reader", SHAPES, ids=["claude", "codex", "antigravity"])
def test_an_open_turn_names_its_open_tool(tmp_path: Path, shape, reader):
    record = shape(tmp_path, Path("/work"), "sess-2", "fake-1")
    record.begin("go", "fake-1", "low")
    state = reader.state(record.path)
    assert state.turn_open and state.open_tool is None and state.turns == 0
    # a tool call without its result: the open tool is named, with when it started
    if reader is claude:
        record.assistant([{"type": "tool_use", "id": "toolu_x", "name": "Bash", "input": {}}], "tool_use")
    elif reader is codex:
        record.line("response_item", {"type": "function_call", "name": "shell", "arguments": "{}", "call_id": "c1"})
    else:
        record.tool_start("Bash", "echo hi")
    state = reader.state(record.path)
    assert state.turn_open and state.open_tool in ("Bash", "shell") and state.open_tool_since is not None
    assert reader.turns(record.path) == []


@pytest.mark.parametrize("shape,reader", SHAPES, ids=["claude", "codex", "antigravity"])
def test_an_interrupted_turn_closes(tmp_path: Path, shape, reader):
    record = shape(tmp_path, Path("/work"), "sess-3", "fake-1")
    record.begin("go", "fake-1", "low")
    record.abort()
    assert not reader.state(record.path).turn_open


@pytest.mark.parametrize("shape,reader", SHAPES, ids=["claude", "codex", "antigravity"])
def test_a_tool_left_open_by_an_interrupted_turn_does_not_haunt_the_next(tmp_path: Path, shape, reader):
    record = shape(tmp_path, Path("/work"), "sess-7", "fake-1")
    record.begin("turn 1", "fake-1", "high")
    record.tool_start("Bash", "sleep 100")
    record.abort()
    record.begin("turn 2", "fake-1", "high")
    state = reader.state(record.path)
    assert state.turn_open and state.open_tool is None and state.open_tool_since is None


def test_claude_records_queued_messages_and_local_commands(tmp_path: Path):
    record = ClaudeShape(tmp_path, Path("/work"), "sess-4", "fake-1")
    record.begin("first", "fake-1", "low")
    record.queued("second")
    state = claude.state(record.path)
    assert state.turn_open and state.queued == ("second",)
    record.end("done")
    record.begin("second", "fake-1", "low")
    assert claude.state(record.path).queued == ()
    record.end("done")
    record.command("/model fake-2", "model set to fake-2")
    state = claude.state(record.path)
    assert not state.turn_open and state.turns == 2, "a local command opens no turn"
    assert state.last_input == "second"


def test_claude_unwraps_a_folded_paste_and_keeps_what_a_turn_took_from_its_queue(tmp_path: Path):
    record = ClaudeShape(tmp_path, Path("/work"), "sess-8", "fake-1")
    record.begin("first order", "fake-1", "low")
    wrapped = '<pasted_content id="758d">\nqd > order from ship · ord-70e2 · qd check\nWrite the file.\n</pasted_content id="758d">'
    record.write({"type": "queue-operation", "operation": "enqueue", "content": wrapped, "sessionId": "sess-8"})
    state = claude.state(record.path)
    assert state.queued == ("qd > order from ship · ord-70e2 · qd check\nWrite the file.",), "the tags are not the message"
    record.write({"type": "queue-operation", "operation": "remove", "content": wrapped, "sessionId": "sess-8"})
    state = claude.state(record.path)
    assert state.queued == () and state.consumed == ("qd > order from ship · ord-70e2 · qd check\nWrite the file.",)
    record.end("did both")
    record.entry("user", {"role": "user", "content": wrapped})
    assert claude.state(record.path).last_input == "qd > order from ship · ord-70e2 · qd check\nWrite the file."
    assert claude.unwrap("plain text") == "plain text"
    submitted = '\n\n<pasted_content id="24aa">\nqd > order from ship · ord-ab12 · qd check\nWrite the file.\n</pasted_content id="24aa">\n'
    assert claude.unwrap(submitted) == "qd > order from ship · ord-ab12 · qd check\nWrite the file.", "a long paste submitted at idle is wrapped with blank lines around it"


def test_a_resumed_record_continues_its_turn_count(tmp_path: Path):
    for shape, reader in SHAPES:
        record = shape(tmp_path, Path("/work"), "sess-5", "fake-1")
        record.begin("one", "fake-1", "low")
        record.end("done")
        again = shape(tmp_path, Path("/work"), "sess-5", "fake-1")
        assert again.path == record.path and again.turns_before() == 1
        again.begin("two", "fake-1", "low")
        again.end("done")
        assert [t.input for t in reader.turns(record.path)] == ["one", "two"]
        assert reader.session_id(record.path) == "sess-5"
        assert reader.first_timestamp(record.path) is not None
        assert reader.records(tmp_path) == [record.path]


def test_a_seat_joins_the_first_record_after_its_launch_and_an_ambiguous_join_is_reported(tmp_path: Path):
    from datetime import datetime, timedelta, timezone

    from quarterdeck.harness.fake import FakeHarness

    harness = FakeHarness()
    launched = datetime.now(timezone.utc) - timedelta(seconds=1)
    before = ClaudeShape(tmp_path, Path("/work"), "old", "fake-1")
    before.write({"type": "user", "timestamp": (launched - timedelta(minutes=5)).isoformat(), "sessionId": "old",
                  "message": {"role": "user", "content": "an older session"}})
    assert harness.join(tmp_path, launched).reason == "no record yet", "a record from before the launch is not this seat's"
    first = ClaudeShape(tmp_path, Path("/work"), "new", "fake-1")
    first.begin("hello", "fake-1", "low")
    assert harness.join(tmp_path, launched).record == first.path  # law: one-record
    twin = ClaudeShape(tmp_path, Path("/work"), "twin", "fake-1")
    stamp = claude.entries(first.path)[0]["timestamp"]
    twin.write({"type": "user", "timestamp": stamp, "sessionId": "twin", "message": {"role": "user", "content": "hello"}})
    join = harness.join(tmp_path, launched)
    assert join.record is None and join.reason.startswith("ambiguous:"), "two records at the same moment: reported, never guessed"


def test_claude_ends_a_turn_only_at_its_duration_entry_and_keeps_every_block(tmp_path: Path):
    import json

    record = tmp_path / "c.jsonl"
    stamp = "2026-11-07T04:00:0{}Z"
    entries = [
        {"type": "user", "timestamp": stamp.format(0), "message": {"role": "user", "content": "first"}},
        {"type": "queue-operation", "operation": "enqueue", "content": "second", "timestamp": stamp.format(1)},
        {"type": "assistant", "timestamp": stamp.format(2), "message": {"id": "m1", "model": "claude-opus-5-5", "stop_reason": "end_turn",
                                                                          "usage": {"output_tokens": 50}, "content": [{"type": "thinking", "thinking": "…"}]}},
        {"type": "assistant", "timestamp": stamp.format(3), "message": {"id": "m1", "model": "claude-opus-5-5", "stop_reason": "end_turn",
                                                                          "usage": {"output_tokens": 50}, "content": [{"type": "text", "text": "first reply"}]}},
        {"type": "queue-operation", "operation": "remove", "content": "second", "timestamp": stamp.format(4)},
        {"type": "assistant", "timestamp": stamp.format(5), "message": {"id": "m2", "model": "claude-opus-5-5", "stop_reason": "tool_use",
                                                                          "usage": {"output_tokens": 20}, "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]}},
        {"type": "assistant", "timestamp": stamp.format(5), "isSidechain": True, "message": {"id": "s1", "stop_reason": "end_turn", "content": [{"type": "text", "text": "a"}]}},
        {"type": "assistant", "timestamp": stamp.format(5), "isSidechain": True, "message": {"id": "s1", "stop_reason": "end_turn", "content": [{"type": "text", "text": "b"}]}},
    ]
    record.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    state, turns = claude.read(record)
    assert state.turn_open and state.open_tool == "Bash" and turns == [], "an end_turn block does not end the turn: the queued message was taken into it"
    assert state.consumed == ("second",)
    entries += [
        {"type": "user", "timestamp": stamp.format(6), "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
        {"type": "assistant", "timestamp": stamp.format(7), "message": {"id": "m3", "model": "claude-opus-5-5", "stop_reason": "end_turn",
                                                                          "usage": {"output_tokens": 5}, "content": [{"type": "text", "text": "second reply"}]}},
        {"type": "system", "subtype": "turn_duration", "durationMs": 9000, "timestamp": stamp.format(9)},
    ]
    record.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    state, turns = claude.read(record)
    assert not state.turn_open and len(turns) == 1
    turn = turns[0]
    assert turn.tokens.written == 50 + 20 + 5, "one usage per message, repeated on its entries, every message of the turn"
    assert turn.reply == "first reply\n\nsecond reply" and turn.seconds == 9 and turn.subagent_turns == 1
    assert [t.name for t in turn.tools] == ["Bash"]


def test_a_partial_last_line_is_skipped(tmp_path: Path):
    record = ClaudeShape(tmp_path, Path("/work"), "sess-6", "fake-1")
    record.begin("one", "fake-1", "low")
    record.end("done")
    with record.path.open("a") as f:
        f.write('{"type": "assistant", "mess')
    assert claude.state(record.path).turns == 1


def test_tokens_add_and_count_the_steps_they_could_not_measure():
    assert Tokens(1, 2, 3, 4) + Tokens(10, 20, 30, 40) == Tokens(11, 22, 33, 44)
    assert Tokens(1, 2, 3, 4).total == 10 and Tokens(1, 2, 3, 4).complete
    partly = Tokens(1, 2, 3, 4) + Tokens.unknown()
    assert partly == Tokens(1, 2, 3, 4, unmeasured=1) and partly.total == 10 and not partly.complete, \
        "the known part is summed and the gap is counted, never hidden and never guessed"
    assert Tokens.unknown().total == 0 and Tokens.unknown().unmeasured == 1


@pytest.mark.parametrize("shape,reader", SHAPES[:2], ids=["claude", "codex"])
def test_a_line_the_reader_does_not_know_is_skipped_and_the_rest_still_read(tmp_path: Path, shape, reader):
    record = shape(tmp_path, Path("/work"), "sess-9", "fake-1")
    record.begin("one", "fake-1", "low")
    record.end("done")
    with record.path.open("a") as f:
        f.write('{"type": "a-shape-from-a-newer-version", "payload": 7}\nnot json at all\n[1, 2, 3]\n')
    record.begin("two", "fake-1", "low")
    record.end("done again")
    state, turns = reader.read(record.path)
    assert [t.input for t in turns] == ["one", "two"] and not state.turn_open and state.turns == 2
    assert state.last_input == "two", "the seat stays readable, so a delivery can still be confirmed"


def test_another_programs_record_is_refused_by_name(tmp_path: Path):
    from quarterdeck.harness.base import RecordError

    record = tmp_path / "other.jsonl"
    record.write_text('{"kind": "nothing a reader knows"}\n')
    with pytest.raises(RecordError, match="another program's file"):
        claude.state(record)
    with pytest.raises(RecordError, match="another program's file"):
        codex.state(record)
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    assert claude.state(empty).turns == 0 and not claude.state(empty).turn_open, "a record with nothing in it yet is idle, not an error"
