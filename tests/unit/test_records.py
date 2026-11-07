"""The record readers, on records the fake harness writes in each real program's shape."""

from __future__ import annotations

from pathlib import Path

import pytest

from quarterdeck.fakerecord import ClaudeShape, CodexShape, tokens_for
from quarterdeck.harness import claude, codex
from quarterdeck.harness.base import Tokens

SHAPES = [(ClaudeShape, claude), (CodexShape, codex)]


@pytest.mark.parametrize("shape,reader", SHAPES, ids=["claude", "codex"])
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
    assert [t.name for t in turn.tools] == ["Bash", "Bash"] if reader is claude else ["shell", "shell"]
    assert [t.failed for t in turn.tools] == [False, True]
    assert all(t.seconds is not None and t.seconds >= 0 for t in turn.tools)
    expected = tokens_for("hello there", "done: hello there", 0)
    steps = 3  # two tool calls and the reply each carry a usage
    assert turn.tokens.read_cached == expected["read_cached"] * steps
    assert turn.tokens.written > 0 and turn.tokens.total == turn.tokens.read_cached + turn.tokens.read_uncached + turn.tokens.cache_written + turn.tokens.written
    state = reader.state(record.path)
    assert not state.turn_open and state.turns == 1 and state.model == "fake-1" and state.effort == "high"
    assert state.last_input == "hello there" and state.open_tool is None


@pytest.mark.parametrize("shape,reader", SHAPES, ids=["claude", "codex"])
def test_an_open_turn_names_its_open_tool(tmp_path: Path, shape, reader):
    record = shape(tmp_path, Path("/work"), "sess-2", "fake-1")
    record.begin("go", "fake-1", "low")
    state = reader.state(record.path)
    assert state.turn_open and state.open_tool is None and state.turns == 0
    # a tool call without its result: the open tool is named, with when it started
    if reader is claude:
        record.assistant([{"type": "tool_use", "id": "toolu_x", "name": "Bash", "input": {}}], "tool_use")
    else:
        record.line("response_item", {"type": "function_call", "name": "shell", "arguments": "{}", "call_id": "c1"})
    state = reader.state(record.path)
    assert state.turn_open and state.open_tool in ("Bash", "shell") and state.open_tool_since is not None
    assert reader.turns(record.path) == []


@pytest.mark.parametrize("shape,reader", SHAPES, ids=["claude", "codex"])
def test_an_interrupted_turn_closes(tmp_path: Path, shape, reader):
    record = shape(tmp_path, Path("/work"), "sess-3", "fake-1")
    record.begin("go", "fake-1", "low")
    record.abort()
    assert not reader.state(record.path).turn_open


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


def test_a_partial_last_line_is_skipped(tmp_path: Path):
    record = ClaudeShape(tmp_path, Path("/work"), "sess-6", "fake-1")
    record.begin("one", "fake-1", "low")
    record.end("done")
    with record.path.open("a") as f:
        f.write('{"type": "assistant", "mess')
    assert claude.state(record.path).turns == 1


def test_tokens_add():
    assert Tokens(1, 2, 3, 4) + Tokens(10, 20, 30, 40) == Tokens(11, 22, 33, 44)
    assert Tokens(1, 2, 3, 4).total == 10
