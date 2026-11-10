"""A spent window stays a wall until its own reset; a failed or stale read never clears it.

Synthetic Claude pools and seat; no provider is read. Every reader of walls is asked
what it says: the Ledger's walls() and walled(), the door's quota guard, failover's
walled check (the same walled() on the seat's pool) and the status line's wall list.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from quarterdeck import watch
from quarterdeck.door import Door
from quarterdeck.harness.limits import Usage, Window
from quarterdeck.ledger import Ledger
from quarterdeck.seat import Launched, Seat
from quarterdeck.ship import Pool, SeatSpec, Session, Ship

FAILED = "provider usage unavailable"


def claude_seat(tmp_path) -> Seat:
    pools = {name: Pool(name, "claude") for name in ("claude-primary", "claude-secondary")}
    spec = SeatSpec("worker", "crew", "worker", "claude", tuple(pools), cwd=tmp_path)
    ship = Ship(tmp_path, "example", "admiral", tmp_path, pools, {"crew": Session("crew", "crew")}, {"worker": spec})
    seat = Seat(ship, spec, SimpleNamespace(pane=lambda ident: None))
    seat.folder.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    Launched("%1", 4242, (now - timedelta(hours=2)).isoformat(), "claude-primary", None, ["claude"], "synthetic",
             harness="claude").write(seat.folder / "launch.json")
    return seat


def read(seat: Seat, *windows: Window, at: datetime, error: str | None = None) -> None:
    book = Ledger(seat.ship.runtime / "ledger.sqlite")
    try:
        book.record_usage("claude-primary", Usage(tuple(windows), at, error), attempted_at=at)
    finally:
        book.close()


def said(seat: Seat) -> dict:
    """What each reader of walls says about claude-primary now."""
    book = Ledger(seat.ship.runtime / "ledger.sqlite")
    try:
        walls = book.walls(seat.ship)
        walled = book.walled("claude-primary", seat.ship)
    finally:
        book.close()
    return {"walls()": walls, "walled()": walled, "door guard": Door(seat).wall(None),
            "failover walled": walled, "status line": watch.ledger_walls(seat.ship)}


def spent(now: datetime, used: float = 1.0) -> Window:
    return Window("5h", used, now + timedelta(hours=3))


def assert_wall(seat: Seat) -> None:
    seen = said(seat)
    assert [w for w in seen["walls()"] if w.startswith("claude-primary 5h")], seen
    assert seen["walled()"] and seen["failover walled"], seen
    assert seen["door guard"] and seen["door guard"].startswith("claude-primary 5h"), seen
    assert [w for w in seen["status line"] if w.startswith("claude-primary 5h")], seen


def assert_no_wall(seat: Seat) -> None:
    seen = said(seat)
    assert seen["walls()"] == [] and not seen["walled()"] and seen["door guard"] is None and seen["status line"] == [], seen


def test_a_spent_window_is_a_wall_while_its_reading_is_current(tmp_path):
    seat, now = claude_seat(tmp_path), datetime.now(timezone.utc)
    read(seat, spent(now), at=now - timedelta(minutes=5))
    assert_wall(seat)


def test_a_failed_read_after_a_spent_window_keeps_the_wall(tmp_path):
    seat, now = claude_seat(tmp_path), datetime.now(timezone.utc)
    read(seat, spent(now), at=now - timedelta(minutes=5))
    read(seat, at=now - timedelta(minutes=1), error=FAILED)
    assert_wall(seat)


def test_a_stale_read_of_a_spent_window_keeps_the_wall(tmp_path):
    seat, now = claude_seat(tmp_path), datetime.now(timezone.utc)
    read(seat, spent(now), at=now - timedelta(minutes=61))
    assert_wall(seat)


def test_a_failed_read_after_a_stale_spent_window_keeps_the_wall(tmp_path):
    seat, now = claude_seat(tmp_path), datetime.now(timezone.utc)
    read(seat, spent(now), at=now - timedelta(minutes=90))
    read(seat, at=now - timedelta(minutes=1), error=FAILED)
    assert_wall(seat)


def test_a_newer_good_reading_with_room_clears_the_wall(tmp_path):
    seat, now = claude_seat(tmp_path), datetime.now(timezone.utc)
    read(seat, spent(now), at=now - timedelta(minutes=30))
    read(seat, at=now - timedelta(minutes=20), error=FAILED)
    read(seat, spent(now, used=0.4), at=now - timedelta(minutes=1))
    assert_no_wall(seat)


def test_the_reset_passing_clears_the_wall(tmp_path):
    seat, now = claude_seat(tmp_path), datetime.now(timezone.utc)
    read(seat, Window("5h", 1.0, now - timedelta(seconds=1)), at=now - timedelta(hours=2))
    assert_no_wall(seat)
    read(seat, at=now - timedelta(minutes=1), error=FAILED)
    assert_no_wall(seat)
    later = Window("7d", 1.0, now + timedelta(hours=3))
    read(seat, later, at=now - timedelta(minutes=5))
    read(seat, at=now - timedelta(minutes=1), error=FAILED)
    book = Ledger(seat.ship.runtime / "ledger.sqlite")
    try:
        assert [w for w in book.walls(seat.ship) if w.startswith("claude-primary 7d")]
        assert book.walls(seat.ship, now=now + timedelta(hours=3, seconds=1)) == []
    finally:
        book.close()


@pytest.mark.parametrize("age,error", [(5, FAILED), (61, None), (90, FAILED)], ids=["failed", "stale", "stale-then-failed"])
def test_an_unspent_window_is_never_a_wall_whatever_its_freshness(tmp_path, age, error):
    seat, now = claude_seat(tmp_path), datetime.now(timezone.utc)
    read(seat, spent(now, used=0.97), at=now - timedelta(minutes=age))
    if error:
        read(seat, at=now - timedelta(minutes=1), error=error)
    assert_no_wall(seat)


def test_a_last_known_wall_still_says_last_known(tmp_path):
    seat, now = claude_seat(tmp_path), datetime.now(timezone.utc)
    read(seat, spent(now), at=now - timedelta(minutes=5))
    read(seat, at=now - timedelta(minutes=1), error=FAILED)
    book = Ledger(seat.ship.runtime / "ledger.sqlite")
    try:
        [reading] = book.usage.rows("claude-primary")
        assert reading.error and not reading.current(now)
        assert "LAST KNOWN" in reading.describe(now)
        [note] = book.quota.usage_notes()
        assert note.startswith("claude-primary latest usage read: ") and FAILED in note
    finally:
        book.close()
