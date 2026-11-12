"""The Watch's pure parts: the status line from facts, the disk reading, the age format."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from quarterdeck import watch
from quarterdeck.watch import Facts


def live_pointer_seat(drydock, monkeypatch):
    from types import SimpleNamespace
    from quarterdeck.seat import Seat
    from quarterdeck.ship import load

    ship = load(drydock)
    seat = Seat(ship, ship.seat("worker-1"), SimpleNamespace())
    seat.folder.mkdir(parents=True, exist_ok=True)
    seat.trusted.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(seat, "pane", lambda: SimpleNamespace(dead=False))
    return seat


def test_an_empty_record_pointer_reads_unknown_with_its_reason(drydock, monkeypatch):
    seat = live_pointer_seat(drydock, monkeypatch)
    pointer = seat.trusted / "record"
    pointer.write_text(" \n\t ")
    reason = seat.join().reason
    assert reason.startswith("record pointer empty:")
    assert watch.activity(seat) == watch.Activity("unknown", reason)
    assert pointer.read_text() == " \n\t "


def test_an_unreadable_record_pointer_reads_unknown_with_its_reason(drydock, monkeypatch):
    seat = live_pointer_seat(drydock, monkeypatch)
    pointer = seat.trusted / "record"
    pointer.write_bytes(b"\xff")
    reason = seat.join().reason
    assert reason.startswith("record pointer unreadable:")
    assert watch.activity(seat) == watch.Activity("unknown", reason)
    assert pointer.read_bytes() == b"\xff"


def test_the_status_line_says_everything_that_waits():  # law: nothing-waits-silently
    quiet = Facts(service="ok", waiting=0, oldest=0, blind=0)
    assert quiet.line() == "service ok · 0 waiting · qd status"
    assert Facts(service="busy 23s", waiting=0, oldest=0, blind=0).line() == "service busy 23s · 0 waiting · qd status", "busy is not dead"
    loud = Facts(service="down", waiting=2, oldest=14, blind=1, needs_human=["unit"], unarmed=["desk"],
                 held=["worker"], walls=["claude-primary 7d"], disks=[("root", 94)], login=[("claude-secondary", "11-13 22:24")],
                 queued=1, oldest_queued=31)
    assert loud.line() == ("SERVICE DOWN · unit needs a human · 1 login needed · disk: root 94%"
                           " · worker held at limit · 2 waiting, oldest 14m · 1 queued · 1 blind · 1 unarmed"
                           " · qd status")


def test_disks_names_only_the_nearly_full_filesystems(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(watch, "DISK_WARN", 2.0)
    assert watch.disks([tmp_path, Path("/")]) == []
    monkeypatch.setattr(watch, "DISK_WARN", 0.0)
    found = watch.disks([tmp_path, tmp_path / "again", Path("/nonexistent")])
    assert len(found) == 1 and found[0][1] >= 0, "one filesystem is named once"


def test_ages_read_as_a_person_says_them():
    now = datetime.now(timezone.utc)
    assert watch._ago(now - timedelta(seconds=42), now) == "42s"
    assert watch._ago(now - timedelta(minutes=14, seconds=3), now) == "14m"
    assert watch._ago(None, now) == ""
    assert str(watch.Activity("working", "in Bash 3m")) == "working, in Bash 3m" and str(watch.Activity("idle")) == "idle"


def test_watch_and_seat_count_what_was_read_and_mark_what_was_not(tmp_path: Path):
    import json
    import sqlite3

    from tests.antigravity_shapes import AGY_SCHEMA, AntigravityShape
    from quarterdeck.seat import Seat
    from quarterdeck.ship import Pool, SeatSpec, Session, Ship
    from quarterdeck.tmux import Pane

    pool = Pool("google", "antigravity")
    spec = SeatSpec("maker", "bridge", "worker", "antigravity", ("google",), cwd=tmp_path)
    ship = Ship(tmp_path, "enterprise", "operator", tmp_path, {"google": pool}, {"bridge": Session("bridge", "qd")}, {"maker": spec})
    idle_screen = "─" * 80 + "\n>\n" + "─" * 80 + "\n                                              Gemini 3.1 Pro · high\n"
    working_screen = "─" * 80 + "\n>\n" + "─" * 80 + "\nesc to cancel                                 Gemini 3.1 Pro · high\n"
    state_holder = {"screen": working_screen}

    class FakeTmux:
        def pane(self, pane_id: str):
            return Pane("%1", "qd", "maker", 4242, 80, 24, False, False, None)

        def capture(self, pane_id: str, history: int = 0) -> str:
            return state_holder["screen"]

    seat = Seat(ship, spec, FakeTmux())
    seat.folder.mkdir(parents=True, exist_ok=True)
    seat.trusted.mkdir(parents=True, exist_ok=True)
    (seat.trusted / "launch.json").write_text(json.dumps({
        "pane": "%1", "pid": 4242, "at": datetime.now(timezone.utc).isoformat(),
        "pool": "google", "resume": None, "argv": ["agy"], "version": "1.5.2",
    }))

    # 1. A record that will not read: activity unknown, today 0+ (nothing read, something there), the screen says whether it works
    bad_record = tmp_path / "bad.db"
    bad_record.write_text("not sqlite")
    (seat.trusted / "record").write_text(f"{bad_record}\n")
    assert watch.activity(seat).state == "unknown"  # law: record-first
    assert str(watch.tokens_today(seat)) == "0+", "nothing could be read, and the + says so"
    assert seat.is_working() is True, "when record is unreadable, is_working falls back to screen"
    state_holder["screen"] = idle_screen
    assert seat.is_working() is False
    assert "unknown" in "\n".join(watch.table(ship, FakeTmux())) and " 0+ " in "\n".join(watch.table(ship, FakeTmux()))
    assert "unreadable" in "\n".join(watch.full(seat)) and "0+ tokens today" in "\n".join(watch.full(seat))

    # 2. A readable record with one ended turn: idle, the model from the record, and today's tokens a plain figure
    rec = AntigravityShape(seat.home, tmp_path, "conv-ok", "gemini-3.1-pro-high")
    rec.begin("ping", "gemini-3.1-pro-high", "high")
    rec.end("pong")
    (seat.trusted / "record").write_text(f"{rec.path}\n")
    assert watch.activity(seat).state == "idle"
    assert seat.is_working() is False
    today = watch.tokens_today(seat)
    assert today.known > 0 and today.unmeasured == 1, "the turn is counted in full; the corrupt record is still the seat's, so the + stays"
    full_lines = "\n".join(watch.full(seat))
    assert "1 turns" in full_lines and f"{today.known}+ tokens today" in full_lines

    # 3. A step status the reader does not know: the turn state is unknown, so are the turns, and the figure says 0+
    with sqlite3.connect(rec.path) as con:
        con.executescript(AGY_SCHEMA)
        con.execute("UPDATE steps SET status = 0 WHERE idx = 0")
    assert seat.harness.turns(rec.path) == []
    assert watch.activity(seat) == watch.Activity("unknown", "turn state unknown in record")
    assert str(watch.tokens_today(seat)) == "0+", "no turn could be closed, and the record's own count is uncertain"
    assert "unknown turns" in "\n".join(watch.full(seat)) and "0+ tokens today" in "\n".join(watch.full(seat))

    # 4. A measured turn in a record whose turn count is uncertain: the figure stands, with the + beside it
    from types import SimpleNamespace
    from quarterdeck.harness.base import State, Tokens, Turn
    now = datetime.now(timezone.utc)
    partial_turn = Turn(1, now, now, "in", "out", "m", Tokens(10, 20, 30, 40))
    def read(path):
        if path != rec.path:
            raise OSError("not this record")
        return State(turn_open=False, turns=None, model="m"), [partial_turn]

    seat.harness = SimpleNamespace(home_variable=None, read=read,
                                   state=lambda r: State(turn_open=False, turns=None, model="m"),
                                   turns=lambda r: [partial_turn], records=lambda home: [rec.path])
    assert str(watch.tokens_today(seat)) == "100+", "what was read is counted; the uncertainty is marked, not spread"


def test_check_probe_handles_unknown_turns_without_crashing(tmp_path: Path, monkeypatch):
    from types import SimpleNamespace

    from quarterdeck import check, door as doormod, post
    from quarterdeck.harness.base import State

    store = post.Store(tmp_path)
    fake_harness = SimpleNamespace(
        name="fake", opening_prompt_turn=False,
        state=lambda r: State(turn_open=None, turns=None, model="gemini-3.1-pro-high"),
    )
    fake_seat = SimpleNamespace(
        name="check-agy",
        up=lambda resume=False: "up",
        is_up=True,
        launched=lambda: SimpleNamespace(pane="%1", pid=4242, at=datetime.now(timezone.utc).isoformat()),
        record=lambda: tmp_path / "unknown.db",
        harness=fake_harness,
    )
    fake_ship = SimpleNamespace(runtime=tmp_path)
    fake_seat.ship = fake_ship
    fake_seat.pane = lambda: SimpleNamespace(id="%1", pid=4242)
    monkeypatch.setattr(check, "Door", lambda s: SimpleNamespace(
        reading=lambda: doormod.Reading(True, "empty box", working=False),
        deliver=lambda text, **kwargs: doormod.Delivery("delivered", "in the record"),
    ))

    def fake_wait_for(pred, timeout):
        val = pred()
        if val:
            return val
        for m in store.open_for("check-agy"):
            (tmp_path / "check" / "check-agy" / "check-agy.txt").write_text(" dummy \n")
            return store.new("report", "check-agy", "ship", "written", re=m.id)
        return False

    monkeypatch.setattr(check, "wait_for", fake_wait_for)
    checks = check.probe(fake_ship, fake_seat)
    assert checks[-1].name == "record joined, the turn ended and read"
    assert checks[-1].passed is False and "? turns" in checks[-1].detail
