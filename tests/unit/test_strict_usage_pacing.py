"""Claude-owned synthetic enforcement of strict native usage pacing and durable refusal holds.

Settled rules under test: `strict_usage_interval` defaults false; a strict reader's
floor (Muse: 1800 seconds) is never shortened by a new wall, a restart or a clock
set back; `usage_reads` keeps exactly three columns with refusal deadlines in the
`usage_holds` side table; any write to an attempt invalidates older side evidence;
the attempt and its hold commit together; a supplied delay is anchored to the
attempt and bounded to [interval, 3600]; a malformed or nonfinite delay is an
ordinary refusal held 3600 seconds under one fixed safe diagnostic; after expiry
only the ordinary floor applies; a corrupt stored hold refuses and alerts a human.

Synthetic readers, ledgers and clocks only: no native client, provider request,
credential or service process.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from quarterdeck import harness as harnesses, headlines, quota, watch
from quarterdeck.harness.limits import Usage, Wall, Window
from quarterdeck.ledger import Ledger
from quarterdeck.quota import BACKOFF_MAX, Quotas
from quarterdeck.service import Service
from quarterdeck.ship import Pool, Session, Ship

POOL = "synthetic-pool"
FLOOR = 1800
SECRET = "synthetic remote body that must never be logged"
FIXED = "native usage refused; retry delay unavailable; hold 3600 seconds"
T0 = datetime(2040, 1, 1, 12, 0, tzinfo=timezone.utc)


class Clock:
    """One wall clock and one monotonic clock that advance together, or apart for a rollback."""

    def __init__(self, monkeypatch, start=T0):
        self.wall, self.mono = start, 10_000.0
        clock = self

        class Now(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock.wall if tz is None else clock.wall.astimezone(tz)

        monkeypatch.setattr(quota, "datetime", Now)
        monkeypatch.setattr(quota, "time", SimpleNamespace(monotonic=lambda: clock.mono))

    def advance(self, seconds):
        self.wall += timedelta(seconds=seconds)
        self.mono += seconds


class Reader:
    name = "synthetic"
    background_usage = True
    reads_usage = True
    usage_from_records = None
    private_usage_home = False

    def __init__(self, strict=True, interval=FLOOR):
        self.strict_usage_interval, self.usage_interval = strict, interval
        self.calls, self.answers = 0, []

    def shared_usage_key(self, pool):
        return None

    def usage(self, pool):
        self.calls += 1
        return self.answers.pop(0) if self.answers else Usage((Window("5h", .1, T0 + timedelta(hours=5)),), quota.datetime.now(timezone.utc))


@pytest.fixture
def reader(monkeypatch):
    found = Reader()
    monkeypatch.setattr(harnesses, "native_for", lambda provider: found)
    return found


def service(tmp_path) -> Service:
    ship = Ship(tmp_path, "synthetic", "operator", tmp_path, {POOL: Pool(POOL, "openai")},
                {"crew": Session("crew", "crew")}, {})
    return Service(ship, SimpleNamespace())


def usage_log(svc) -> list[dict]:
    if not svc.log_path.exists():
        return []
    return [e for e in map(json.loads, svc.log_path.read_text().splitlines()) if e["event"] == "usage"]


def refused(delay):
    return Usage((), quota.datetime.now(timezone.utc), SECRET, retry_after=delay)


def side(svc):
    return svc.ledger.db.execute("SELECT retry_at,set_at,reason FROM usage_holds WHERE pool=?", (POOL,)).fetchone()


def alerted(svc, now) -> bool:
    """The human alert, wherever a person looks: the status line and the detailed status table."""
    alerts = quota.pacing_alerts(svc.ship, now=now)
    facts = watch.Facts(service="ok", waiting=0, oldest=0, blind=0, usage_errors=alerts)
    line = facts.line()
    table = "\n".join(watch.render_table(dict(service="ok", seats=[], open_orders=[], facts=facts, usage=[])))
    # Reserve enough space to fit the usage count but not the pool-specific detail.
    reserved = "synthetic status note " + "x" * 80
    overflowed = headlines.status_line(facts, notes=(headlines.note(reserved, reserved),))
    found = [POOL in text and "human" in text for text in (line, table)] + [any(POOL in a and "human" in a for a in alerts)]
    assert len(set(found)) == 1, f"the alert must agree everywhere: {found}"
    assert ("usage evidence needs human" in overflowed) == found[0], "an overflowing status line still counts it"
    return found[0]


def live_seat(svc):
    return SimpleNamespace(name="synthetic-seat", harness=None, pool=svc.ship.pools[POOL])


def new_wall(svc, now):
    svc.ledger.quota.observe("synthetic-record", "synthetic-seat", POOL,
                             Wall("synthetic-wall", now, now + timedelta(hours=2)), now)


# -- the strict floor -------------------------------------------------------------------------


def test_the_capability_defaults_false_on_the_base_adapter():
    from quarterdeck.harness.base import Harness
    assert Harness.strict_usage_interval is False


def test_a_new_wall_cannot_shorten_the_strict_floor(tmp_path, monkeypatch, reader):
    clock = Clock(monkeypatch)
    svc = service(tmp_path)
    svc.ledger.quota.usage_read(POOL, clock.wall - timedelta(seconds=60), None)
    new_wall(svc, clock.wall)
    assert svc.ledger.quota.due(POOL, clock.wall, FLOOR), "precondition: the ordinary wall shortcut is due"
    quota.refresh_declared(svc)
    assert reader.calls == 0
    clock.advance(FLOOR - 61)
    quota.refresh_declared(svc)
    assert reader.calls == 0
    clock.advance(2)
    quota.refresh_declared(svc)
    assert reader.calls == 1
    svc.ledger.close()


def test_a_default_reader_keeps_its_wall_shortcut(tmp_path, monkeypatch, reader):
    clock = Clock(monkeypatch)
    reader.strict_usage_interval = False
    svc = service(tmp_path)
    svc.ledger.quota.usage_read(POOL, clock.wall - timedelta(seconds=60), None)
    new_wall(svc, clock.wall)
    quota.refresh_declared(svc)
    assert reader.calls == 1, "existing adapters are unchanged by the strict capability"
    svc.ledger.close()


def test_a_restart_cannot_shorten_the_strict_floor(tmp_path, monkeypatch, reader):
    clock = Clock(monkeypatch)
    svc = service(tmp_path)
    quota.refresh_declared(svc)
    assert reader.calls == 1
    for _ in range(3):
        svc.ledger.close()
        clock.advance(FLOOR / 4)
        svc = service(tmp_path)  # memory pacing gone; only the book remains
        new_wall(svc, clock.wall)
        quota.refresh_declared(svc)
        assert reader.calls == 1
    clock.advance(FLOOR / 4 + 1)
    svc.ledger.close()
    svc = service(tmp_path)
    quota.refresh_declared(svc)
    assert reader.calls == 2
    svc.ledger.close()


def test_a_clock_set_back_cannot_shorten_the_strict_floor(tmp_path, monkeypatch, reader):
    clock = Clock(monkeypatch)
    svc = service(tmp_path)
    quota.refresh_declared(svc)
    clock.wall -= timedelta(hours=6)  # the wall clock moves back; the stored attempt is now in its future
    clock.mono += FLOOR * 2
    new_wall(svc, clock.wall)
    quota.refresh_declared(svc)
    assert reader.calls == 1
    # Pike rule 5: an attempt more than 3600 s ahead is corrupt evidence, never a silent hold.
    quota.refresh_declared(svc)
    assert reader.calls == 1
    [entry] = [e for e in usage_log(svc) if e.get("no_call")]
    assert "human" in entry["error"]
    assert svc.ledger.quota.pacing_error(POOL, strict=True, now=clock.wall) is not None
    assert alerted(svc, clock.wall)
    svc.ledger.close()


def test_an_attempt_less_than_the_longest_delay_ahead_is_held_by_the_floor_without_an_alert(tmp_path, monkeypatch, reader):
    clock = Clock(monkeypatch)
    svc = service(tmp_path)
    quota.refresh_declared(svc)
    clock.wall -= timedelta(seconds=BACKOFF_MAX - 60)  # still within the longest allowed delay
    clock.mono += FLOOR * 2
    quota.refresh_declared(svc)
    assert reader.calls == 1, "the floor counts from the stored attempt, never from a clock that went back"
    assert svc.ledger.quota.pacing_error(POOL, strict=True, now=clock.wall) is None
    assert not alerted(svc, clock.wall)
    clock.advance(BACKOFF_MAX - 60 + FLOOR + 1)
    quota.refresh_declared(svc)
    assert reader.calls == 2
    svc.ledger.close()


def test_a_default_reader_is_not_alerted_for_a_future_attempt(tmp_path, monkeypatch, reader):
    clock = Clock(monkeypatch)
    reader.strict_usage_interval = False
    svc = service(tmp_path)
    svc.ledger.quota.usage_read(POOL, clock.wall + timedelta(hours=6), None)
    assert svc.ledger.quota.pacing_error(POOL, strict=False, now=clock.wall) is None
    assert not alerted(svc, clock.wall), "existing adapters keep their behaviour"
    svc.ledger.close()


# -- Pike rule 4: the floor holds on every read of a strict pool, live, demand or destination ---


def test_a_live_read_keeps_the_strict_floor_across_walls_and_restarts(tmp_path, monkeypatch, reader):
    clock = Clock(monkeypatch)
    reader.background_usage = False
    svc = service(tmp_path)
    seat = live_seat(svc)
    seat.harness = reader
    quota.refresh(svc, seat, True, None)
    assert reader.calls == 1
    for step in range(3):
        new_wall(svc, clock.wall)
        clock.advance(FLOOR / 4)
        quota.refresh(svc, seat, True, None)
        svc.ledger.close()
        svc = service(tmp_path)  # restart: memory gone, stored attempt remains
        quota.refresh(svc, seat, True, None)
    assert reader.calls == 1
    clock.advance(FLOOR / 4 + 1)
    quota.refresh(svc, seat, True, None)
    assert reader.calls == 2
    svc.ledger.close()


def test_two_live_seats_on_one_strict_pool_make_one_read(tmp_path, monkeypatch, reader):
    Clock(monkeypatch)
    reader.background_usage = False
    svc = service(tmp_path)
    first, second = live_seat(svc), live_seat(svc)
    first.harness = second.harness = reader
    second.name = "synthetic-second-seat"
    quota.refresh(svc, first, True, None)
    quota.refresh(svc, second, True, None)
    assert reader.calls == 1
    svc.ledger.close()


def test_a_demand_read_inside_the_floor_makes_no_call_and_records_nothing(tmp_path, monkeypatch, reader):
    clock = Clock(monkeypatch)
    svc = service(tmp_path)
    quota.refresh_declared(svc)
    before = svc.ledger.quota.last_attempt(POOL)
    clock.advance(FLOOR - 1)
    assert quota.read_usage(svc, svc.ship.pools[POOL], reader) is None
    assert reader.calls == 1 and svc.ledger.quota.last_attempt(POOL) == before, "a refused demand never slides the floor"
    svc.ledger.close()


def test_a_destination_check_on_a_strict_pool_uses_evidence_only(tmp_path, monkeypatch, reader):
    Clock(monkeypatch)
    reader.background_usage = False
    svc = service(tmp_path)
    monkeypatch.setattr(quota, "cached_open", lambda *a, **kw: "synthetic cached answer")
    assert svc.known_open(live_seat(svc), POOL) == "synthetic cached answer"
    assert reader.calls == 0
    svc.ledger.close()


def test_an_unreadable_stored_attempt_holds_for_a_human_and_logs_once(tmp_path, monkeypatch, reader):
    Clock(monkeypatch)
    svc = service(tmp_path)
    svc.ledger.db.execute("INSERT INTO usage_reads VALUES (?,?,?)", (POOL, "not a time", None))
    svc.ledger.db.commit()
    for _ in range(3):
        quota.refresh_declared(svc)
    assert reader.calls == 0
    [entry] = usage_log(svc)
    assert entry["no_call"] is True and "human" in entry["error"]
    svc.ledger.close()


# -- refusal holds ----------------------------------------------------------------------------


@pytest.mark.parametrize("delay,hold", [(0, FLOOR), (10, FLOOR), (-5, FLOOR), (2400, 2400), (2400.5, 2400.5),
                                        (99_999, BACKOFF_MAX)])
def test_a_supplied_delay_is_bounded_and_anchored_once_to_the_attempt(tmp_path, monkeypatch, reader, delay, hold):
    clock = Clock(monkeypatch)
    svc = service(tmp_path)
    reader.answers = [refused(delay)]
    quota.refresh_declared(svc)
    retry_at, set_at, reason = side(svc)
    assert reason == "native refusal"
    assert datetime.fromisoformat(set_at) == clock.wall == svc.ledger.quota.last_attempt(POOL)
    assert datetime.fromisoformat(retry_at) - clock.wall == timedelta(seconds=hold)
    assert svc.usage_hold[POOL] == pytest.approx(clock.mono + hold), "memory and book hold the same deadline"
    for _ in range(3):  # reading the hold never slides it
        clock.advance(1)
        assert quota.held(svc, POOL)
    assert side(svc)[0] == retry_at
    svc.ledger.close()


@pytest.mark.parametrize("delay", [float("nan"), float("inf"), float("-inf"), "60", True, [60], {"s": 60}])
def test_a_malformed_delay_is_an_ordinary_refusal_held_3600_seconds(tmp_path, monkeypatch, reader, delay):
    clock = Clock(monkeypatch)
    svc = service(tmp_path)
    reader.answers = [refused(delay)]
    quota.refresh_declared(svc)
    retry_at, set_at, _ = side(svc)
    assert datetime.fromisoformat(set_at) == clock.wall
    assert datetime.fromisoformat(retry_at) - clock.wall == timedelta(seconds=BACKOFF_MAX)
    [entry] = usage_log(svc)
    assert {k: v for k, v in entry.items() if k != "at"} == {
        "event": "usage", "pool": POOL, "error": FIXED, "backoff": BACKOFF_MAX}
    stored = svc.ledger.db.execute("SELECT error FROM usage_reads WHERE pool=?", (POOL,)).fetchone()[0]
    assert FIXED in stored and SECRET not in stored and SECRET not in svc.log_path.read_text()
    assert "human" not in entry["error"], "an ordinary refusal needs no human"
    svc.ledger.close()


def test_a_held_pool_is_not_read_past_the_floor_nor_after_a_restart_and_logs_nothing(tmp_path, monkeypatch, reader):
    clock = Clock(monkeypatch)
    svc = service(tmp_path)
    reader.answers = [refused(float("nan"))]
    quota.refresh_declared(svc)
    assert reader.calls == 1
    clock.advance(FLOOR + 1)  # the floor alone would now allow a read
    quota.refresh_declared(svc)
    svc.ledger.close()
    svc = service(tmp_path)  # restart: memory hold gone, stored deadline remains
    clock.advance(BACKOFF_MAX - FLOOR - 2)
    quota.refresh_declared(svc)
    assert reader.calls == 1
    assert len(usage_log(svc)) == 1, "held ticks log nothing"
    clock.advance(2)  # past the deadline: the ordinary floor alone governs
    quota.refresh_declared(svc)
    assert reader.calls == 2
    assert side(svc) is None and POOL not in svc.usage_hold, "a good read clears the hold"
    quota.refresh_declared(svc)
    assert reader.calls == 2, "after expiry the strict floor applies again"
    svc.ledger.close()


def test_a_corrupt_stored_hold_refuses_alerts_a_human_and_logs_once(tmp_path, monkeypatch, reader):
    clock = Clock(monkeypatch)
    svc = service(tmp_path)
    svc.ledger.quota.usage_read(POOL, clock.wall - timedelta(hours=3), None)
    svc.ledger.db.execute("INSERT INTO usage_holds VALUES (?,?,?,?)", (POOL, "not a time", clock.wall.isoformat(), "native refusal"))
    svc.ledger.db.commit()
    for _ in range(3):
        quota.refresh_declared(svc)
    assert reader.calls == 0
    [entry] = usage_log(svc)
    assert entry["no_call"] is True and "human" in entry["error"]
    assert any(POOL in note and "human" in note for note in svc.ledger.quota.usage_notes())
    assert alerted(svc, clock.wall), "the status line and the detailed status show the corrupt hold"
    svc.ledger.close()


def test_a_hold_set_in_the_far_future_is_corrupt_and_alerted(tmp_path, monkeypatch, reader):
    clock = Clock(monkeypatch)
    svc = service(tmp_path)
    ahead = clock.wall + timedelta(seconds=BACKOFF_MAX + 60)
    svc.ledger.quota.usage_read(POOL, ahead, "synthetic refusal", retry_at=ahead + timedelta(seconds=60),
                                retry_reason="native refusal")
    quota.refresh_declared(svc)
    assert reader.calls == 0
    with pytest.raises(ValueError):
        svc.ledger.quota.usage_hold(POOL, clock.wall)
    assert alerted(svc, clock.wall)
    svc.ledger.close()


@pytest.mark.parametrize("row", [
    ("{now}", "{now}", "another reason"),
    ("{late}", "{now}", "native refusal"),
    ("{early}", "{now}", "native refusal"),
    ("2040-01-01T12:00:00", "{now}", "native refusal"),
    ("{now}", "2040-01-01T12:00:00", "native refusal"),
    (None, "{now}", "native refusal"),
    ("{far_deadline}", "{far}", "native refusal"),
])
def test_every_invalid_hold_shape_is_corrupt_not_absent(row):
    now = T0
    far = now + timedelta(seconds=BACKOFF_MAX + 1)
    values = dict(now=now.isoformat(), late=(now + timedelta(seconds=BACKOFF_MAX + 1)).isoformat(),
                  early=(now - timedelta(seconds=1)).isoformat(), far=far.isoformat(),
                  far_deadline=(far + timedelta(seconds=10)).isoformat())
    with sqlite3.connect(":memory:") as db:
        book = Quotas(db)
        book.usage_read(POOL, now, None)
        db.execute("INSERT INTO usage_holds VALUES (?,?,?,?)",
                   (POOL, *[v.format(**values) if isinstance(v, str) else v for v in row]))
        with pytest.raises(ValueError):
            book.usage_hold(POOL, now)


def test_a_valid_hold_reads_back_exactly_at_the_same_clock():
    with sqlite3.connect(":memory:") as db:
        book = Quotas(db)
        hold(book)
        assert book.usage_hold(POOL, T0) == T0 + timedelta(hours=1)
        assert book.pacing_error(POOL, strict=True, now=T0) is None


# -- the side table and its writers ------------------------------------------------------------


def hold(book, pool=POOL, at=T0):
    book.usage_read(pool, at, "synthetic refusal", retry_at=at + timedelta(hours=1), retry_reason="native refusal")


@pytest.mark.parametrize("touch", [
    "INSERT OR REPLACE INTO usage_reads VALUES ('synthetic-pool', '2040-01-01T13:00:00+00:00', NULL)",
    "UPDATE usage_reads SET error=NULL WHERE pool='synthetic-pool'",
    "UPDATE usage_reads SET pool='renamed-pool' WHERE pool='synthetic-pool'",
    "DELETE FROM usage_reads WHERE pool='synthetic-pool'",
])
def test_any_attempt_write_invalidates_the_older_hold_and_only_its_own(touch):
    with sqlite3.connect(":memory:") as db:
        book = Quotas(db)
        hold(book)
        hold(book, "other-pool")
        db.execute(touch)
        assert book.usage_hold(POOL, T0) is None
        assert book.usage_hold("other-pool", T0) == T0 + timedelta(hours=1)


def test_the_attempt_table_keeps_exactly_three_columns():
    with sqlite3.connect(":memory:") as db:
        Quotas(db)
        assert [r[1] for r in db.execute("PRAGMA table_info(usage_reads)")] == ["pool", "at", "error"]
        assert [r[1] for r in db.execute("PRAGMA table_info(usage_holds)")] == ["pool", "retry_at", "set_at", "reason"]


def test_an_attempt_and_its_hold_commit_together_or_not_at_all(tmp_path):
    path = tmp_path / "synthetic.sqlite"
    first, second = sqlite3.connect(path), sqlite3.connect(path)
    book = Quotas(first)
    first.execute("CREATE TRIGGER synthetic_refuse BEFORE INSERT ON usage_holds BEGIN SELECT RAISE(ABORT, 'synthetic'); END")
    first.commit()
    with pytest.raises(sqlite3.DatabaseError):
        hold(book)
    assert second.execute("SELECT * FROM usage_reads WHERE pool=?", (POOL,)).fetchone() is None
    book.usage_read("other-pool", T0, None)  # a later, unrelated commit on the same connection
    assert second.execute("SELECT * FROM usage_reads WHERE pool=?", (POOL,)).fetchone() is None, \
        "a refused attempt never lands later without its hold"
    first.close()
    second.close()


def test_the_ledger_writes_the_reading_its_attempt_and_its_hold_together_or_not_at_all(tmp_path):
    book = Ledger(tmp_path / "synthetic-ledger.sqlite")
    book.db.execute("CREATE TRIGGER synthetic_refuse BEFORE INSERT ON usage_holds BEGIN SELECT RAISE(ABORT, 'synthetic'); END")
    book.db.commit()
    other = sqlite3.connect(tmp_path / "synthetic-ledger.sqlite")
    pools_before = other.execute("SELECT * FROM pools ORDER BY pool, window").fetchall()
    usage = Usage((Window("5h", .5, T0 + timedelta(hours=5)),), T0, "synthetic refusal")
    with pytest.raises(sqlite3.DatabaseError):
        book.record_usage(POOL, usage, T0, retry_at=T0 + timedelta(hours=1), retry_reason="native refusal")
    book.quota.usage_read("other-pool", T0, None)  # a later, unrelated commit on the same connection
    assert other.execute("SELECT * FROM usage_reads WHERE pool=?", (POOL,)).fetchone() is None
    assert other.execute("SELECT * FROM pools ORDER BY pool, window").fetchall() == pools_before
    other.close()
    book.close()


def test_rollback_to_an_older_writer_and_reupgrade(tmp_path):
    path = tmp_path / "synthetic.sqlite"
    with sqlite3.connect(path) as db:
        hold(Quotas(db))
    with sqlite3.connect(path) as db:  # an older service: its own positional writer, no side-table knowledge
        db.execute("CREATE TABLE IF NOT EXISTS usage_reads (pool TEXT PRIMARY KEY, at TEXT NOT NULL, error TEXT)")
        db.execute("INSERT OR REPLACE INTO usage_reads VALUES (?,?,?)", (POOL, (T0 + timedelta(minutes=5)).isoformat(), "old"))
    with sqlite3.connect(path) as db:  # re-upgrade
        book = Quotas(db)
        assert book.usage_hold(POOL, T0 + timedelta(minutes=5)) is None, "the older writer's attempt superseded the hold"
        assert book.last_attempt(POOL) == T0 + timedelta(minutes=5)
        hold(book, at=T0 + timedelta(minutes=10))
        assert book.usage_hold(POOL, T0 + timedelta(minutes=10)) == T0 + timedelta(minutes=70)


def test_an_older_book_is_upgraded_in_place_without_losing_attempts(tmp_path):
    path = tmp_path / "synthetic.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE usage_reads (pool TEXT PRIMARY KEY, at TEXT NOT NULL, error TEXT)")
        db.execute("INSERT INTO usage_reads VALUES (?,?,?)", (POOL, T0.isoformat(), "old refusal"))
    with sqlite3.connect(path) as db:
        book = Quotas(db)
        assert book.last_attempt(POOL) == T0 and book.usage_hold(POOL, T0) is None
        hold(book, at=T0 + timedelta(minutes=1))
        assert book.usage_hold(POOL, T0 + timedelta(minutes=1)) == T0 + timedelta(minutes=61)
