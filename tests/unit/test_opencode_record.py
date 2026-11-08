"""Synthetic OpenCode sessions and unfamiliar rows, through watch and service accounting."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from quarterdeck import input_receipt, service, watch
from quarterdeck.harness.base import Input
from quarterdeck.harness import opencode
from quarterdeck.harness.base import Join
from quarterdeck.post import Store
from quarterdeck.ledger import Ledger
from quarterdeck.seat import Launched, Seat
from quarterdeck.service import Service
from quarterdeck.ship import Pool, SeatSpec, Session, Ship
from tests.contract.test_opencode import SCHEMA

AT = datetime(2030, 1, 1, tzinfo=timezone.utc)
MS = int(AT.timestamp() * 1000)


def database(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    for statement in SCHEMA:
        con.execute(statement)
    return con


def session(con, name="root", at=0, parent=None):
    con.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (name, "synthetic-project", "synthetic", "/synthetic", "Synthetic session", "synthetic",
                 MS + at, MS + at, None, None, parent))


def message(con, name, sid, at, data):
    con.execute("INSERT INTO message VALUES (?,?,?,?,?)", (name, sid, MS + at, MS + at, json.dumps(data)))


def part(con, name, mid, sid, at, data):
    con.execute("INSERT INTO part VALUES (?,?,?,?,?,?)", (name, mid, sid, MS + at, MS + at, json.dumps(data)))


def user(con, name, sid, at, text):
    message(con, name, sid, at, {"role": "user", "time": {"created": MS + at}, "model": {"modelID": f"{sid}-model"}})
    part(con, f"{name}-text", name, sid, at, {"type": "text", "text": text})


def assistant(con, name, parent, sid, at, *, completed=True, finish="stop", tokens=None):
    data = {"role": "assistant", "parentID": parent, "time": {"created": MS + at},
            "tokens": tokens if tokens is not None else {"total": 5, "input": 3, "output": 2},
            "cost": 0, "modelID": f"{sid}-model", "finish": finish}
    if completed:
        data["time"]["completed"] = MS + at + 1
    message(con, name, sid, at, data)
    return data


def root_and_child(con):
    session(con)
    user(con, "root-input", "root", 10, "Run a synthetic task")
    assistant(con, "root-step", "root-input", "root", 20, finish="tool-calls")
    part(con, "task", "root-step", "root", 21, {"type": "tool", "tool": "task",
                                            "state": {"status": "running", "time": {"start": MS + 21}}})
    session(con, "child", 30, "root")
    user(con, "child-input", "child", 40, "Synthetic child instructions")
    assistant(con, "child-answer", "child-input", "child", 50)
    part(con, "child-text", "child-answer", "child", 51, {"type": "text", "text": "Synthetic child result"})
    con.commit()


def test_child_answer_cannot_close_root_or_change_state_resume_or_receipts(tmp_path: Path) -> None:
    record = tmp_path / "home" / "data-001" / "opencode" / "opencode.db"
    con = database(record)
    root_and_child(con)
    state, turns = opencode.read(record)
    assert opencode.session_id(record) == "root"
    assert opencode.OpenCodeHarness._data_dir(tmp_path / "home", "root", "worker") == record.parents[1]
    assert state.turn_open and state.open_tool == "task" and state.open_tool_since == AT + timedelta(milliseconds=21)
    assert state.model == "root-model" and state.last_input == "Run a synthetic task" and state.queued == ()
    assert state.last_write == AT + timedelta(milliseconds=51), "the child is progress, but never root completion"
    assert [(t.index, t.input, t.reply, t.tokens.total) for t in turns] == [(1, "Synthetic child instructions", "Synthetic child result", 5)]
    assert input_receipt.confirmed("opencode", record, "Synthetic child instructions", AT) is None
    assert input_receipt.confirmed("opencode", record, "Run a synthetic task", AT) == "delivered"
    # A later root completion keeps the child's index: it must not be lost or charged twice.
    book = Ledger(tmp_path / "ledger.sqlite")
    store = Store(tmp_path / "post-runtime")
    book.ingest("worker", "local", record, turns, store)
    con.execute("UPDATE part SET data=? WHERE id='task'", (json.dumps({"type": "tool", "tool": "task",
                 "state": {"status": "completed", "time": {"start": MS + 21, "end": MS + 60}}}),))
    assistant(con, "root-answer", "root-input", "root", 70)
    con.commit()
    state, turns = opencode.read(record)
    assert not state.turn_open and state.last_input == "Run a synthetic task"
    assert [(t.index, t.tokens.total) for t in turns] == [(0, 10), (1, 5)]
    book.ingest("worker", "local", record, turns, store)
    book.ingest("worker", "local", record, turns, store)
    assert book.db.execute("SELECT turn,read_uncached,written FROM turns ORDER BY turn").fetchall() == [(0, 6, 4), (1, 3, 2)]
    book.close()
    con.close()


def test_same_session_answer_moves_on_without_inventing_completion_time(tmp_path: Path) -> None:
    record = tmp_path / "synthetic.db"
    con = database(record)
    root_and_child(con)
    # Make the root's first step lack a completion timestamp, as with queued input consumption.
    con.execute("UPDATE message SET data=json_remove(data,'$.time.completed') WHERE id='root-step'")
    user(con, "root-next", "root", 80, "A later root input")
    assistant(con, "root-next-answer", "root-next", "root", 90)
    con.commit()
    state, turns = opencode.read(record)
    assert not state.turn_open and state.last_input == "A later root input"
    assert [t.index for t in turns] == [0, 1, 2]
    assert turns[0].timed is False and turns[0].seconds is None
    assert turns[0].tools[0].name == "task" and turns[0].tools[0].seconds is None
    assert turns[2].timed and turns[2].input == "A later root input"
    con.close()


def test_latest_root_owns_state_even_when_an_older_roots_child_is_newer(tmp_path: Path) -> None:
    record = tmp_path / "synthetic.db"
    con = database(record)
    root_and_child(con)
    session(con, "next-root", 100)
    user(con, "next-input", "next-root", 110, "Fresh root input")
    session(con, "late-child", 120, "root")
    user(con, "late-input", "late-child", 130, "Other task input")
    assistant(con, "late-answer", "late-input", "late-child", 140)
    con.commit()
    state, turns = opencode.read(record)
    assert opencode.session_id(record) == "next-root" and state.turn_open
    assert state.last_input == "Fresh root input" and state.model == "next-root-model"
    assert state.last_write == AT + timedelta(milliseconds=110)
    assert len(turns) == 2 and all("task" in t.input.lower() or "child" in t.input.lower() for t in turns)
    assert opencode.inputs(record) == [Input("Fresh root input", AT + timedelta(milliseconds=110))]
    con.close()


@pytest.mark.parametrize("bad_tokens", [
    {"total": 3, "cache": ["unrecognized"]}, ["unrecognized"], {"total": 3, "cache": "unknown"},
    {"total": 3, "input": {}}, {"total": float("nan")}, {"total": 3, "output": True},
])
def test_unknown_nested_usage_is_unmeasured_with_siblings_through_watch_and_account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_tokens) -> None:
    pool = Pool("local", "local", endpoint="http://synthetic.invalid/v1")
    spec = SeatSpec("worker", "crew", "worker", "opencode", ("local",), cwd=tmp_path)
    ship = Ship(tmp_path / "ship", "synthetic", "operator", tmp_path, {"local": pool},
                {"crew": Session("crew", "crew")}, {"worker": spec})
    tmux = SimpleNamespace(pane=lambda ident: None)
    seat = Seat(ship, spec, tmux)
    record = seat.home / "data-001" / "opencode" / "opencode.db"
    con = database(record)
    session(con)
    user(con, "input", "root", 10, "Synthetic input")
    assistant(con, "unknown-usage", "input", "root", 20, tokens=bad_tokens, finish="tool-calls")
    assistant(con, "good-answer", "input", "root", 30)
    part(con, "text", "good-answer", "root", 31, {"type": "text", "text": "Readable sibling reply"})
    con.commit()
    con.close()
    (seat.folder / "record").write_text(str(record))
    launched = Launched("%999", 999, AT.isoformat(), "local", "root", ["opencode"], "synthetic", harness="opencode")
    launched.write(seat.folder / "launch.json")
    observed = SimpleNamespace(pane=lambda: SimpleNamespace(dead=False), harness=seat.harness, join=lambda: Join(record))
    assert watch.activity(observed, AT + timedelta(seconds=1)).state == "idle"
    turns = seat.harness.turns(record)
    assert len(turns) == 1 and turns[0].reply == "Readable sibling reply"
    assert turns[0].tokens.total == 5 and turns[0].tokens.unmeasured == 1
    monkeypatch.setattr(service.machine, "processes", lambda: {})
    svc = Service(ship, tmux)
    svc.account()
    svc.account()
    assert svc.ledger.db.execute("SELECT read_uncached,written,unmeasured FROM turns").fetchall() == [(3, 2, 1)]
    assert svc.ledger.rollup("pool")[0].unmeasured
    svc.ledger.close()


def test_other_unfamiliar_nested_fields_do_not_hide_valid_text_or_turns(tmp_path: Path) -> None:
    record = tmp_path / "synthetic.db"
    con = database(record)
    session(con)
    message(con, "bad-user", "root", 10, {"role": "user", "time": [], "model": []})
    part(con, "bad-text", "bad-user", "root", 10, {"type": "text", "text": []})
    bad = assistant(con, "bad-step", "bad-user", "root", 20)
    bad.update(time=["unrecognized"], cost=10**400)
    con.execute("UPDATE message SET data=? WHERE id=?", (json.dumps(bad), "bad-step"))
    for index, state in enumerate(([], {"status": []}, {"status": "completed", "time": [], "metadata": []})):
        part(con, f"bad-tool-{index}", "bad-step", "root", 21 + index, {"type": "tool", "state": state})
    user(con, "good-user", "root", 30, "Readable input")
    assistant(con, "good-answer", "good-user", "root", 40)
    con.commit()
    state, turns = opencode.read(record)
    assert state.last_input == "Readable input" and not state.turn_open
    assert turns[0].cost is None and turns[0].seconds is None
    assert turns[1].tokens.total == 5
    assert opencode.inputs(record)[-1] == Input("Readable input", AT + timedelta(milliseconds=30))
    con.close()
