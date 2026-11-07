"""Antigravity 1.5.2 against the screens saved from the spike of 7 November 2026, best effort by design."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from quarterdeck.door import echoed
from quarterdeck.fakerecord import (AGY_SCHEMA, AntigravityShape, _pb_bytes, _pb_str, _pb_ts, _pb_uint, tokens_for)
from quarterdeck.harness import antigravity
from quarterdeck.harness.antigravity import AntigravityHarness
from quarterdeck.harness.base import RecordError, Tokens, same_name
from quarterdeck.seat import Seat
from quarterdeck.ship import Pool, SeatSpec, Session, Ship
from quarterdeck.tmux import Pane

from tests.conftest import FIXTURES

VERSION = "1.5.2"
SCREENS = FIXTURES / "antigravity" / VERSION


def screen(name: str):
    return AntigravityHarness().screen((SCREENS / f"{name}.txt").read_text())


def test_every_fixture_set_has_the_screens_the_adapter_reads():
    names = {p.stem for p in SCREENS.glob("*.txt")}
    assert {"idle", "paste-shown", "paste-long-folded", "paste-long-lines-folded", "working", "working-queued", "interrupted"} <= names


def test_an_idle_seat_shows_an_empty_box_and_its_model_and_effort():
    s = screen("idle")
    assert s.box == "" and not s.working and s.interruption is None
    assert s.shows == {"model": "Gemini 4 Argon", "effort": "high"} and same_name(s.shows["model"], "gemini-4-argon-high")
    assert any(e.startswith("You are seat laan on ship example") for e in s.echoes), "the first message is echoed in full"


def test_a_short_paste_shows_whole_and_a_long_one_folds_to_its_length():
    """Probed 7 November 2026: four short lines show whole; 2,003 characters fold to a count that leaves out the trailing newline."""
    harness = AntigravityHarness()
    four = "qd > order from desk · ord-1234 · Four\nline two\nline three\nqd > reply: qd post report --re ord-1234"
    s = screen("paste-shown")
    assert harness.box(four).matches(s.box), s.box
    long = "qd probe B: first line\n" + " ".join(f"word{i}" for i in range(260)) + "\nlast line\n"
    assert len(long) == 2003
    folded = screen("paste-long-folded")
    assert folded.box == "[Pasted text #3 2002 chars]" and harness.box(long).matches(folded.box)
    assert not harness.box(long + "x\n").matches(folded.box), "the count is this paste's own"
    many = "\n".join(f"line {i} of a long order" for i in range(42)) + "\n"
    lines = screen("paste-long-lines-folded")
    assert lines.box == "[Pasted text #5 +42 lines]" and harness.box(many).matches(lines.box), "many lines fold by their count"
    assert not harness.box(many + "one more\n").matches(lines.box)


def test_the_join_takes_the_one_conversation_that_appeared_since_the_launch(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(antigravity, "STATE_DIR", tmp_path / "state")
    conversations = tmp_path / "state" / "conversations"
    conversations.mkdir(parents=True)
    (conversations / "older.db").write_text("the user's own, from before")
    harness = AntigravityHarness()
    seat = SeatSpec("laan", "bridge", "navigator", "antigravity", ("google",), cwd=tmp_path)
    home, brief = tmp_path / "home", tmp_path / "brief.md"
    home.mkdir()
    brief.write_text("# Standing orders\n")
    harness.launch(seat, Pool("google", "antigravity"), home, brief, None, None)
    assert (home / antigravity.BEFORE).read_text() == "older.db\n"
    launched_at = datetime.now(timezone.utc)
    assert harness.join(home, launched_at).reason == "no record yet"
    (conversations / "older.db").write_text("written to after the launch: its ctime moved, and it is still not ours")
    assert harness.join(home, launched_at).reason == "no record yet"
    (conversations / "ours.db").write_text("new")
    assert harness.join(home, launched_at).record == conversations / "ours.db"
    (conversations / "theirs.db").write_text("the user started one too")
    join = harness.join(home, launched_at)
    assert join.record is None and join.reason == "ambiguous: ours.db, theirs.db appeared since the launch"  # law: one-record
    harness.launch(seat, Pool("google", "antigravity"), home, brief, "ours", None)
    assert (home / antigravity.BEFORE).read_text() == "older.db\n", "a resumed launch keeps what the first noted"


def test_a_working_seat_and_a_queued_message():
    s = screen("working")
    assert s.working and s.box == "" and s.interruption is None
    assert s.shows == {"model": "Gemini 4 Argon", "effort": "high"}
    q = screen("working-queued")
    assert q.working and q.box == "" and q.queued == 1, "the placeholder is not text in the box"
    assert q.shows == {"model": "Gemini 4 Argon", "effort": "high"}, "queued hint on left must not pollute model"
    typed = "qd > order from desk · ord-0002 · Second\nThis arrived mid-turn. Say SECOND-SEEN.\nqd > reply: qd post report --re ord-0002\n"
    assert any(echoed(e, " ".join(typed.split())) for e in q.echoes), q.echoes
    assert AntigravityHarness().keys.send_now == ("M-m",), "alt+m sends a queued message immediately"

    # Combined queued-message hint on left + background-task suffix on right + trailing blank rows
    combined = (
        "─" * 80 + "\n>\n" + "─" * 80 + "\n  ● [12:00:00] pytest -q running\n" + "─" * 80 + "\n"
        "  Press up to edit queued messages                         Gemini 4 Argon · high · 1 task(s) · /tasks\n\n\n"
    )
    cs = AntigravityHarness().screen(combined)
    assert cs.box == "" and cs.queued == 1 and cs.shows == {"model": "Gemini 4 Argon", "effort": "high"}


def test_background_task_screen_recognizes_empty_or_nonempty_box_and_fails_closed(tmp_path: Path):
    from quarterdeck.door import Door

    harness = AntigravityHarness()
    bg = screen("working-background-task")
    assert bg.working and bg.box == "" and bg.interruption is None
    assert bg.shows == {"model": "Gemini 4 Argon", "effort": "high"}
    assert any(echoed(e, "qd > order from desk · ord-1234 · Four line two line three Run a background task and wait.") for e in bg.echoes)

    # Verify Door.reading() permits normal guarded delivery when box is empty above a background job,
    # obstructs when real user text or a folded paste is in the box above a background job,
    # and fails closed on an unrecognized layout.
    pool = Pool("google", "antigravity")
    spec = SeatSpec("laan", "bridge", "worker", "antigravity", ("google",), cwd=tmp_path)
    ship = Ship(tmp_path, "example", "admiral", tmp_path, {"google": pool}, {"bridge": Session("bridge", "qd")}, {"laan": spec})
    holder = {"text": (SCREENS / "working-background-task.txt").read_text()}
    fake_tmux = type("FakeTmux", (), {
        "pane": lambda self, pid: Pane("%1", "qd", "laan", 4242, 80, 24, False, False, None),
        "capture": lambda self, pid, history=0: holder["text"],
    })()
    seat = Seat(ship, spec, fake_tmux)
    seat.folder.mkdir(parents=True, exist_ok=True)
    (seat.folder / "launch.json").write_text(json.dumps({
        "pane": "%1", "pid": 4242, "at": datetime.now(timezone.utc).isoformat(),
        "pool": "google", "resume": None, "argv": ["agy"], "version": "1.5.2",
    }))
    assert Door(seat).reading().open is True, "empty box above background task permits normal guarded delivery"

    # Real user text in input box above background task obstructs delivery
    holder["text"] = (
        "─" * 80 + "\n> draft text typed by user\n  second line\n" + "─" * 80 + "\n"
        "  ● [12:00:00] sleep 30; echo done running\n" + "─" * 80 + "\n"
        "esc to cancel                                              Gemini 4 Argon · high · 1 task(s) · /tasks\n"
    )
    s_text = harness.screen(holder["text"])
    assert s_text.box == "draft text typed by user\nsecond line"
    r_text = Door(seat).reading()
    assert not r_text.open and r_text.clears == "needs a human" and r_text.reason == "text in the box"

    # Folded paste in input box above background task remains text and obstructs delivery
    holder["text"] = (
        "─" * 80 + "\n> [Pasted text #2 +18 lines]\n" + "─" * 80 + "\n"
        "  ● [12:00:00] sleep 30; echo done running\n" + "─" * 80 + "\n"
        "                                                           Gemini 4 Argon · high · 1 task(s) · /tasks\n"
    )
    s_folded = harness.screen(holder["text"])
    assert s_folded.box == "[Pasted text #2 +18 lines]"
    assert not Door(seat).reading().open

    # Unrecognized layout between rules (no '>' prompt) fails closed with box=None
    holder["text"] = (
        "─" * 80 + "\n  unrecognized modal banner without prompt\n" + "─" * 80 + "\n"
        "                                                           Gemini 4 Argon · high\n"
    )
    assert harness.screen(holder["text"]).box is None
    assert not Door(seat).reading().open


def test_an_interrupted_seat_is_idle_and_the_turns_prompt_is_echoed():
    s = screen("interrupted")
    assert not s.working and s.box == ""
    assert any(echoed(e, "Run exactly this shell command: sleep 30; echo late") for e in s.echoes)


def test_an_echo_keeps_a_paragraph_break_and_stops_at_the_programs_own_line():
    msg = "qd > order from desk · ord-1234\nFirst paragraph.\n\nSecond paragraph.\nqd > reply: qd post report --re ord-1234"
    text = ("─" * 60 + "\n> qd > order from desk · ord-1234\n  First paragraph.\n\n  Second paragraph.\n  qd > reply: qd post report --re ord-1234\n"
            "  ⎿  Interrupted · What should Antigravity CLI do instead?\n" + "─" * 80 + "\n>\n" + "─" * 80 + "\n"
            "esc to cancel                                              Gemini 4 Argon · high\n")
    s = AntigravityHarness().screen(text)
    assert any(echoed(e, " ".join(msg.split())) for e in s.echoes), s.echoes
    assert all("Interrupted" not in e for e in s.echoes)


def test_the_record_confirms_and_the_controls_are_launch_only():
    harness = AntigravityHarness()
    assert harness.record_confirms is True and harness.home_variable is None
    assert harness.keys.empty is None, "no key empties the whole box: the door says what it leaves behind"
    assert all(c.session is None for c in harness.controls()), "model and effort change at launch"


def test_record_decodes_turns_tools_and_state_and_counts_an_unreported_class_as_zero(tmp_path: Path):
    home = tmp_path / "home"
    rec = AntigravityShape(home, tmp_path, "conv-1", "gemini-3.1-pro-high")
    rec.begin("qd > order from pike · ord-1234\nCheck status", "gemini-3.1-pro-high", "high")
    rec.consumed("qd > note from pike · not-5678\nExtra note")
    rec.tool("run_command", "qd status", "service ok", False)
    rec.tool("run_command", "false", "failed", True)
    # Mark a step with has_subtrajectory=1 and an invoke_subagent tool call: subagent_turns must still be None
    with sqlite3.connect(rec.path) as con:
        con.execute("UPDATE steps SET has_subtrajectory = 1 WHERE idx = 0")
    rec.tool("invoke_subagent", "helper", "done", False)
    rec.end("Status checked and reported.")

    state, turns = antigravity.read(rec.path)
    assert antigravity.session_id(rec.path) == "conv-1"
    assert antigravity.first_timestamp(rec.path) is not None
    assert state.turn_open is False and state.turns == 1 and state.open_tool is None
    assert state.model == "gemini-3.1-pro-high" and state.effort is None
    assert state.last_input == "qd > note from pike · not-5678\nExtra note"
    assert state.consumed == ("qd > note from pike · not-5678\nExtra note",)

    assert len(turns) == 1
    turn = turns[0]
    assert turn.input == "qd > order from pike · ord-1234\nCheck status"
    assert turn.reply == "Status checked and reported."
    assert turn.model == "gemini-3.1-pro-high"
    assert [t.name for t in turn.tools] == ["run_command", "run_command", "invoke_subagent"]
    assert [t.failed for t in turn.tools] == [False, True, False]
    assert all(t.seconds is not None and t.seconds >= 0 for t in turn.tools)
    expected = tokens_for(turn.input, turn.reply, 0)
    assert turn.tokens.read_cached == expected["read_cached"] * 4
    assert turn.tokens.read_uncached > 0 and turn.tokens.written > 0
    assert turn.tokens.cache_written == 0 and turn.tokens.complete, "a class this provider never reports is zero, not unknown"
    assert turn.tokens.total == turn.tokens.read_cached + turn.tokens.read_uncached + turn.tokens.written
    assert turn.cost is None and turn.subagent_turns is None


def test_snapshot_reader_never_creates_sidecars_beside_source_or_writes_to_tmp(tmp_path: Path, monkeypatch):
    import tempfile

    conversations = tmp_path / "conversations"
    conversations.mkdir()
    rec = AntigravityShape(tmp_path / "home", tmp_path, "closed-wal", "gemini-3.1-pro-high", path=conversations / "closed-wal.db")
    rec.begin("hello", "gemini-3.1-pro-high", "high")
    rec.end("world")
    # Close writer: SQLite leaves only closed-wal.db with WAL header (0x02, 0x02 at bytes 18..20)
    for sidecar in (Path(f"{rec.path}-wal"), Path(f"{rec.path}-shm")):
        sidecar.unlink(missing_ok=True)
    header = rec.path.read_bytes()[18:20]
    assert header == b"\x02\x02" and list(conversations.iterdir()) == [rec.path]

    # Forbid any tempfile creation during record reads: snapshot must be purely in-memory
    monkeypatch.setattr(tempfile, "TemporaryDirectory", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no /tmp writes allowed")))

    state = antigravity.state(rec.path)
    turns = antigravity.turns(rec.path)
    assert state.turns == 1 and len(turns) == 1 and turns[0].reply == "world"
    assert list(conversations.iterdir()) == [rec.path], "reading a closed WAL db must never create -wal or -shm beside source"  # law: one-folder

    # Now hold a writer connection open so uncheckpointed frames live only in .db-wal
    writer = sqlite3.connect(rec.path)
    try:
        stamp = datetime.now(timezone.utc)
        meta = _pb_ts(1, stamp) + _pb_str(12, "exec-open")
        payload = _pb_bytes(5, meta) + _pb_bytes(19, _pb_str(2, "wal-only prompt"))
        writer.execute("INSERT INTO steps VALUES (99, 14, 1, 0, ?, ?, ?)", (meta, b"", payload))
        writer.commit()
        assert Path(f"{rec.path}-wal").is_file() and Path(f"{rec.path}-wal").stat().st_size > 0
        st = antigravity.state(rec.path)
        assert st.turn_open is True and st.last_input == "wal-only prompt"
    finally:
        writer.close()


def test_delayed_terminal_insertion_preserves_stable_turn_identity_and_idempotent_reread(tmp_path: Path):
    from quarterdeck.ledger import Ledger
    from quarterdeck.post import Store

    db_path = tmp_path / "delayed.db"
    stamp = datetime.now(timezone.utc)
    with sqlite3.connect(db_path) as con:
        con.executescript(AGY_SCHEMA)
        # Input 0 (execution-0, uncached 10) and Input 1 (execution-1, uncached 11); only execution-1 is terminal initially
        m0 = _pb_ts(1, stamp) + _pb_str(12, "e0")
        u0 = _pb_uint(1, 0) + _pb_uint(2, 10) + _pb_uint(3, 5)
        con.execute("INSERT INTO steps VALUES (0, 14, 3, 0, ?, ?, ?)", (m0, b"", _pb_bytes(19, _pb_str(2, "input-0"))))
        con.execute("INSERT INTO steps VALUES (1, 15, 3, 0, ?, ?, ?)", (m0 + _pb_bytes(9, u0), b"", _pb_bytes(20, _pb_str(8, "reply-0"))))

        m1 = _pb_ts(1, stamp) + _pb_str(12, "e1")
        u1 = _pb_uint(1, 0) + _pb_uint(2, 11) + _pb_uint(3, 6)
        con.execute("INSERT INTO steps VALUES (2, 14, 3, 0, ?, ?, ?)", (m1, b"", _pb_bytes(19, _pb_str(2, "input-1"))))
        con.execute("INSERT INTO steps VALUES (3, 15, 3, 0, ?, ?, ?)", (m1 + _pb_bytes(9, u1), b"", _pb_bytes(20, _pb_str(8, "reply-1"))))
        em1 = _pb_uint(1, 4) + _pb_uint(2, 1) + _pb_uint(3, 3) + _pb_str(9, "e1")
        con.execute("INSERT INTO executor_metadata VALUES (0, ?)", (em1,))

    turns1 = antigravity.turns(db_path)
    assert [(t.index, t.input, t.tokens.read_uncached) for t in turns1] == [(2, "input-1", 11)]

    book = Ledger(tmp_path / "ledger.sqlite")
    store = Store(tmp_path)
    assert book.ingest("scott", "google", db_path, turns1, store) == 1

    # Now execution-0 gains its terminal executor_metadata row
    with sqlite3.connect(db_path) as con:
        em0 = _pb_uint(1, 4) + _pb_uint(2, 1) + _pb_uint(3, 1) + _pb_str(9, "e0")
        con.execute("INSERT INTO executor_metadata VALUES (1, ?)", (em0,))

    turns2 = antigravity.turns(db_path)
    assert [(t.index, t.input, t.tokens.read_uncached) for t in turns2] == [(0, "input-0", 10), (2, "input-1", 11)]
    assert book.ingest("scott", "google", db_path, turns2, store) == 1
    assert book.ingest("scott", "google", db_path, turns2, store) == 0, "idempotent reread writes nothing"
    rows = book.db.execute("SELECT turn, read_uncached FROM turns ORDER BY turn").fetchall()
    assert rows == [(0, 10), (2, 11)], "input-0 is present and input-1 is never charged twice"
    book.close()


def test_a_turn_without_a_completion_timestamp_ends_at_its_last_step_with_its_duration_unknown(tmp_path: Path):
    from datetime import timedelta

    db_path = tmp_path / "times.db"
    t0 = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(seconds=12.5)
    with sqlite3.connect(db_path) as con:
        con.executescript(AGY_SCHEMA)
        # Turn 0: canceled USER_INPUT (status 6) with only created_at (field 1) and no completion/termination timestamp
        m0 = _pb_ts(1, t0) + _pb_str(12, "e-cancel")
        con.execute("INSERT INTO steps VALUES (0, 14, 6, 0, ?, ?, ?)", (m0, b"", _pb_bytes(19, _pb_str(2, "canceled input"))))

        # Turn 1: executor invocation count is not a timestamp; only the final step completion supplies the end
        m1 = _pb_ts(1, t0) + _pb_str(12, "e-exec-ts")
        con.execute("INSERT INTO steps VALUES (1, 14, 3, 0, ?, ?, ?)", (m1, b"", _pb_bytes(19, _pb_str(2, "exec timed input"))))
        con.execute("INSERT INTO steps VALUES (2, 15, 3, 0, ?, ?, ?)", (m1 + _pb_ts(8, t1), b"", _pb_bytes(20, _pb_str(8, "reply"))))
        em1 = _pb_uint(1, 4) + _pb_uint(2, 1) + _pb_uint(3, 2) + _pb_str(9, "e-exec-ts")
        con.execute("INSERT INTO executor_metadata VALUES (0, ?)", (em1,))

        # Turn 2: executor marker with no final step completion time
        m2 = _pb_ts(1, t0) + _pb_str(12, "e-no-ts")
        con.execute("INSERT INTO steps VALUES (3, 14, 3, 0, ?, ?, ?)", (m2, b"", _pb_bytes(19, _pb_str(2, "no end time"))))
        con.execute("INSERT INTO steps VALUES (4, 15, 3, 0, ?, ?, ?)", (m2, b"", _pb_bytes(20, _pb_str(8, "reply 2"))))
        em2 = _pb_uint(1, 4) + _pb_uint(3, 4) + _pb_str(9, "e-no-ts")
        con.execute("INSERT INTO executor_metadata VALUES (1, ?)", (em2,))

    state, turns = antigravity.read(db_path)
    assert len(turns) == 3
    assert turns[0].started == t0 and turns[0].ended == t0 and turns[0].seconds is None, \
        "a canceled input ends at its own step: it has a place in time, and no duration is invented"
    assert turns[1].started == t0 and turns[1].ended == t1 and turns[1].seconds == 12.5, "completed_at of the final step supplies end"
    assert turns[2].started == t0 and turns[2].ended == t0 and turns[2].seconds is None and not turns[2].timed, \
        "the last step's time places the turn; the duration stays unknown"
    assert state.last_write is not None and state.last_write >= t0, "last activity time is tracked separately"


def test_records_returns_only_proven_seat_owned_conversations_and_never_foreign_ones(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(antigravity, "STATE_DIR", tmp_path / "state")
    conversations = tmp_path / "state" / "conversations"
    conversations.mkdir(parents=True)
    older = AntigravityShape(tmp_path / "other", tmp_path, "older", "gemini-3.1-pro-high", path=conversations / "older.db")
    older.begin("user session", "gemini-3.1-pro-high", "high")

    pool = Pool("google", "antigravity")
    spec = SeatSpec("scott", "bridge", "worker", "antigravity", ("google",), cwd=tmp_path)
    ship = Ship(tmp_path, "enterprise", "admiral", tmp_path, {"google": pool}, {"bridge": Session("bridge", "qd")}, {"scott": spec})
    fake_tmux = type("FakeTmux", (), {"pane": lambda self, pid: Pane("%1", "qd", "scott", 4242, 80, 24, False, False, None)})()
    seat = Seat(ship, spec, fake_tmux)
    seat.home.mkdir(parents=True, exist_ok=True)
    brief = tmp_path / "brief.md"
    brief.write_text("# Standing orders\n")
    seat.harness.launch(spec, pool, seat.home, brief, None, None)
    (seat.folder / "launch.json").write_text(json.dumps({
        "pane": "%1", "pid": 4242, "at": datetime.now(timezone.utc).isoformat(),
        "pool": "google", "resume": None, "argv": ["agy"], "version": "1.5.2",
    }))

    # Two conversations appear after launch (ambiguous join): neither may be owned or returned by records()
    ours = AntigravityShape(tmp_path / "unused1", tmp_path, "ours", "gemini-3.1-pro-high", path=conversations / "ours.db")
    foreign = AntigravityShape(tmp_path / "unused2", tmp_path, "foreign", "gemini-3.1-pro-high", path=conversations / "foreign.db")
    assert seat.join().record is None  # law: one-record
    assert seat.harness.records(seat.home) == []

    # Remove the foreign conversation so the join succeeds unambiguously on ours.db
    foreign.path.unlink()
    assert seat.join().record == ours.path
    assert seat.harness.records(seat.home) == [ours.path]

    # Even if another foreign conversation appears later or the seat unlinks its current record on fresh start,
    # records() still returns only ours.db and never foreign.db
    AntigravityShape(tmp_path / "unused3", tmp_path, "foreign2", "gemini-3.1-pro-high", path=conversations / "foreign2.db")
    (seat.folder / "record").unlink()
    assert seat.harness.records(seat.home) == [ours.path]


def test_proto3_absent_zero_tokens_vs_missing_usage_message(tmp_path: Path):
    db_path = tmp_path / "tokens.db"
    stamp = datetime.now(timezone.utc)
    with sqlite3.connect(db_path) as con:
        con.executescript(AGY_SCHEMA)
        m0 = _pb_ts(1, stamp) + _pb_str(12, "e1")
        con.execute("INSERT INTO steps VALUES (0, 14, 3, 0, ?, ?, ?)", (m0, b"", _pb_bytes(19, _pb_str(2, "q1"))))
        # Step 1: model_usage present with input=120, output=30, cache_read=0 (omitted in proto3)
        u1 = _pb_uint(1, 0) + _pb_uint(2, 120) + _pb_uint(3, 30)
        m1 = _pb_ts(1, stamp) + _pb_bytes(9, u1) + _pb_str(12, "e1")
        con.execute("INSERT INTO steps VALUES (1, 15, 3, 0, ?, ?, ?)", (m1, b"", _pb_bytes(20, _pb_str(8, "r1"))))
        em1 = _pb_uint(1, 4) + _pb_uint(2, 1) + _pb_uint(3, 1) + _pb_str(9, "e1")
        con.execute("INSERT INTO executor_metadata VALUES (0, ?)", (em1,))

        # Turn 2: planner response with NO model_usage message at all
        m2 = _pb_ts(1, stamp) + _pb_str(12, "e2")
        con.execute("INSERT INTO steps VALUES (2, 14, 3, 0, ?, ?, ?)", (m2, b"", _pb_bytes(19, _pb_str(2, "q2"))))
        con.execute("INSERT INTO steps VALUES (3, 15, 3, 0, ?, ?, ?)", (m2, b"", _pb_bytes(20, _pb_str(8, "r2"))))
        em2 = _pb_uint(1, 4) + _pb_uint(2, 1) + _pb_uint(3, 3) + _pb_str(9, "e2")
        con.execute("INSERT INTO executor_metadata VALUES (1, ?)", (em2,))

    turns = antigravity.turns(db_path)
    assert len(turns) == 2
    assert turns[0].tokens == Tokens(read_cached=0, read_uncached=120, cache_written=0, written=30), "an absent proto3 scalar is zero"
    assert turns[1].tokens == Tokens.unknown() and turns[1].tokens.unmeasured == 1, "no usage message at all: one unmeasured step"
    assert (turns[0].tokens + turns[1].tokens) == Tokens(0, 120, 0, 30, unmeasured=1), "the known part is kept, the gap counted"


def test_unknown_step_status_and_unclosed_prior_turn_before_newer_user_input(tmp_path: Path):
    db_path = tmp_path / "edge.db"
    stamp = datetime.now(timezone.utc)
    with sqlite3.connect(db_path) as con:
        con.executescript(AGY_SCHEMA)
        # Turn 0 in execution e1: DONE user input + DONE planner response, but NO executor_metadata termination
        m1 = _pb_ts(1, stamp) + _pb_str(12, "e1")
        con.execute("INSERT INTO steps VALUES (0, 14, 3, 0, ?, ?, ?)", (m1, b"", _pb_bytes(19, _pb_str(2, "turn 0"))))
        con.execute("INSERT INTO steps VALUES (1, 15, 3, 0, ?, ?, ?)", (m1, b"", _pb_bytes(20, _pb_str(8, "partial"))))
        # Turn 1 in execution e2 arrives without e1 ever closing, and ends normally
        m2 = _pb_ts(1, stamp) + _pb_str(12, "e2")
        con.execute("INSERT INTO steps VALUES (2, 14, 3, 0, ?, ?, ?)", (m2, b"", _pb_bytes(19, _pb_str(2, "turn 1"))))
        con.execute("INSERT INTO steps VALUES (3, 15, 3, 0, ?, ?, ?)", (m2, b"", _pb_bytes(20, _pb_str(8, "complete"))))
        em2 = _pb_uint(1, 4) + _pb_uint(2, 1) + _pb_uint(3, 3) + _pb_str(9, "e2")
        con.execute("INSERT INTO executor_metadata VALUES (0, ?)", (em2,))

    state, turns = antigravity.read(db_path)
    assert [t.input for t in turns] == ["turn 1"], "a newer USER_INPUT alone must never close the prior turn"
    assert state.turns is None and state.turn_open is None

    # Now add a step with unknown enum status (0 = UNSPECIFIED)
    with sqlite3.connect(db_path) as con:
        con.execute("UPDATE steps SET status = 0 WHERE idx = 3")
    state2, _ = antigravity.read(db_path)
    assert state2.turn_open is None and state2.turns is None


def test_corrupt_or_non_sqlite_record_raises_record_error(tmp_path: Path):
    bad = tmp_path / "bad.db"
    bad.write_text("not a sqlite file")
    with pytest.raises(RecordError):
        antigravity.state(bad)

    empty_db = tmp_path / "empty.db"
    with sqlite3.connect(empty_db) as con:
        con.execute("CREATE TABLE something_else (id INTEGER)")
    with pytest.raises(RecordError, match="missing steps table"):
        antigravity.turns(empty_db)

    corrupt_pb = tmp_path / "corrupt_pb.db"
    with sqlite3.connect(corrupt_pb) as con:
        con.executescript(AGY_SCHEMA)
        con.execute("INSERT INTO steps VALUES (0, 14, 3, 0, ?, ?, ?)", (b"\x80\x80\x80", b"", b""))
    with pytest.raises(RecordError):
        antigravity.state(corrupt_pb)


def test_the_launch_carries_the_brief_as_the_first_message_and_our_settings(tmp_path: Path):
    harness = AntigravityHarness()
    seat = SeatSpec("laan", "bridge", "navigator", "antigravity", ("google",), model="gemini-4-argon-high", effort="high",
                    cwd=tmp_path / "work", settings={"theme": "dark"})
    home = tmp_path / "home"
    assert harness.build_home(home, seat, Pool("google", "antigravity"), None) is None
    settings = json.loads((home / "settings.json").read_text())
    assert settings == {"trustedWorkspaces": [str(tmp_path / "work")], "theme": "dark"}
    brief = tmp_path / "brief.md"
    brief.write_text("# Standing orders\nBe brief.\n")
    launch = harness.launch(seat, Pool("google", "antigravity"), home, brief, None, "qd > briefed as laan")
    assert launch.argv[:3] == ("agy", "--dangerously-skip-permissions", "--settings")
    assert launch.argv[-2:] == ("--prompt-interactive", "# Standing orders\nBe brief.\n\nqd > briefed as laan")
    assert launch.env == {"AGY_CLI_DISABLE_AUTO_UPDATE": "true"}
    resumed = harness.launch(seat, Pool("google", "antigravity"), home, brief, "abc", None)
    assert "--conversation" in resumed.argv and "--prompt-interactive" not in resumed.argv, "a resumed conversation is not briefed twice"
