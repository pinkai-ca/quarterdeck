"""The service's pure judgments: whether a record shows that this very message was taken."""

from __future__ import annotations

import pytest

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from quarterdeck import delivery_record, post
from quarterdeck.harness.synthetic_record import ClaudeShape
from quarterdeck.harness.fake import FakeHarness
from quarterdeck.post import Message, Store
from quarterdeck.service import Service


def test_receipt_matches_the_message_id_and_distinguishes_queue_acceptance(tmp_path: Path) -> None:
    store = Store(tmp_path)
    earlier = store.new("order", "desk", "scott", "done")
    record = ClaudeShape(tmp_path / "home", Path("/work"), "s1", "fake-1")
    record.begin(post.typed([earlier]), "fake-1", "low")
    record.end("ok")
    since = datetime.now(timezone.utc) - timedelta(seconds=1)
    later = store.new("order", "desk", "scott", "done")  # the same body, another id
    assert delivery_record.receipt(FakeHarness(), record.path, earlier, since) == "delivered"
    assert delivery_record.receipt(FakeHarness(), record.path, later, since) is None  # law: one-door
    # an input older than the attempt proves nothing about it
    assert delivery_record.receipt(FakeHarness(), record.path, earlier, datetime.now(timezone.utc) + timedelta(seconds=5)) is None
    # queued, not yet an input: still taken
    record.begin("something else", "fake-1", "low")
    record.queued(post.typed([later]))
    assert delivery_record.receipt(FakeHarness(), record.path, later, since) == "queued"


def test_receipt_without_a_record_is_unknown(tmp_path: Path) -> None:
    message = Message("ord-1-d-0001", "order", "desk", "scott", post.now(), "x")
    assert delivery_record.receipt(FakeHarness(), None, message, post.now()) is None


def test_account_tracks_wal_fingerprint_and_retries_after_transient_read_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    import sqlite3

    from tests.antigravity_shapes import AntigravityShape, _pb_bytes, _pb_str, _pb_ts, _pb_uint
    from quarterdeck.harness import antigravity
    from quarterdeck.harness.base import RecordError, record_fingerprint
    from quarterdeck.ship import Pool, SeatSpec, Session, Ship
    from quarterdeck.tmux import Pane

    pool = Pool("google", "antigravity")
    spec = SeatSpec("scott", "bridge", "worker", "antigravity", ("google",), cwd=tmp_path)
    ship = Ship(tmp_path, "enterprise", "admiral", tmp_path, {"google": pool}, {"bridge": Session("bridge", "qd")}, {"scott": spec})
    fake_tmux = SimpleNamespace(pane=lambda pane_id: Pane("%1", "qd", "scott", 4242, 80, 24, False, False, None), capture=lambda *a, **k: "")
    svc = Service(ship, fake_tmux)
    seat = svc.seat("scott")
    seat.folder.mkdir(parents=True, exist_ok=True)
    seat.trusted.mkdir(parents=True, exist_ok=True)
    rec = AntigravityShape(seat.home, tmp_path, "conv-wal", "gemini-3.1-pro-high")
    (seat.trusted / "record").write_text(f"{rec.path}\n")
    (seat.trusted / "launch.json").write_text(json.dumps({
        "pane": "%1", "pid": 4242, "at": datetime.now(timezone.utc).isoformat(),
        "pool": "google", "resume": None, "argv": ["agy"], "version": "1.5.2",
    }))

    rec.begin("turn 1", "gemini-3.1-pro-high", "high")
    rec.end("reply 1")

    # The service's retry bound runs on a monotonic clock the test moves; nothing else about time changes.
    import os
    import time as real_time

    from quarterdeck import attribution_cache, service as service_module

    clock = SimpleNamespace(now=1000.0)
    moved = SimpleNamespace(**{name: getattr(real_time, name) for name in dir(real_time) if not name.startswith("_")})
    moved.monotonic = lambda: clock.now
    monkeypatch.setattr(service_module, "time", moved)
    monkeypatch.setattr(attribution_cache, "time", moved)

    # Simulate a transient RecordError on the first account() pass: read_marks must NOT be set, so a later pass retries
    orig_read = antigravity.read
    failing = {"next": True}

    def flaky_read(path: Path):
        if failing["next"]:
            failing["next"] = False
            raise RecordError("transient concurrent write")
        return orig_read(path)

    monkeypatch.setattr(antigravity, "read", flaky_read)
    monkeypatch.setattr(antigravity.AntigravityHarness, "read", staticmethod(flaky_read))
    svc.account()
    assert svc.ledger.ingested(rec.path) == 0 and rec.path not in svc.read_marks
    assert svc.ledger.incomplete_records() == [(str(rec.path), "scott", "google", "transient concurrent write")]
    assert svc.ledger.rollup("seat")[0].unmeasured == 1, "nothing read yet, and the figure says so with a +"
    # Record, registry and launch evidence unchanged: no re-read inside the bound, though a read would now succeed
    for step in (0.0, service_module.ACCOUNT_RETRY - 1):
        clock.now += step
        svc.account()
        assert svc.ledger.ingested(rec.path) == 0 and rec.path not in svc.read_marks
        assert svc.ledger.incomplete_records() == [(str(rec.path), "scott", "google", "transient concurrent write")]
    clock.now += 1  # the bound itself: the unchanged record is read again and recovers
    svc.account()
    assert svc.ledger.ingested(rec.path) == 1 and rec.path in svc.read_marks
    assert svc.ledger.incomplete_records() == [] and svc.ledger.rollup("seat")[0].turns == 1

    # A source change is retried at once, with no clock movement: the first change meets another transient
    # error, the second change is read in the same instant and clears it
    failing["next"] = True
    os.utime(rec.path, ns=(rec.path.stat().st_atime_ns, rec.path.stat().st_mtime_ns + 1_000_000))
    svc.account()
    assert svc.ledger.incomplete_records() == [(str(rec.path), "scott", "google", "transient concurrent write")]
    assert rec.path not in svc.read_marks and svc.ledger.ingested(rec.path) == 1
    os.utime(rec.path, ns=(rec.path.stat().st_atime_ns, rec.path.stat().st_mtime_ns + 1_000_000))
    svc.account()
    assert svc.ledger.incomplete_records() == [] and rec.path in svc.read_marks
    assert svc.ledger.ingested(rec.path) == 1 and svc.ledger.rollup("seat")[0].turns == 1

    # Now hold a writer connection open and write Turn 2 only to the WAL (main .db mtime frozen)
    writer = sqlite3.connect(rec.path)
    try:
        db_stat = rec.path.stat()
        stamp = datetime.now(timezone.utc)
        m2 = _pb_ts(1, stamp) + _pb_str(12, "exec-2")
        writer.execute("INSERT INTO steps VALUES (10, 14, 3, 0, ?, ?, ?)", (m2, b"", _pb_bytes(19, _pb_str(2, "turn 2"))))
        u2 = _pb_uint(1, 0) + _pb_uint(2, 80) + _pb_uint(3, 20) + _pb_uint(5, 300)
        m3 = _pb_ts(1, stamp) + _pb_bytes(9, u2) + _pb_str(12, "exec-2")
        writer.execute("INSERT INTO steps VALUES (11, 15, 3, 0, ?, ?, ?)", (m3, b"", _pb_bytes(20, _pb_str(8, "reply 2"))))
        em2 = _pb_uint(1, 4) + _pb_uint(2, 1) + _pb_uint(3, 11) + _pb_str(9, "exec-2")
        writer.execute("INSERT INTO executor_metadata VALUES (10, ?)", (em2,))
        writer.commit()
        import os
        os.utime(rec.path, ns=(db_stat.st_atime_ns, db_stat.st_mtime_ns))
        assert record_fingerprint(rec.path) != svc.read_marks[rec.path]
        svc.account()
        assert svc.ledger.ingested(rec.path) == 2
        fp_with_wal = record_fingerprint(rec.path)
    finally:
        writer.close()

    # Closing the writer checkpoints and removes .db-wal: fingerprint reflects WAL removal
    assert record_fingerprint(rec.path) != fp_with_wal

    # If an earlier step becomes status 0 (unknown turn state), account() keeps the known turns in the ledger
    # and marks the record incomplete, so the rollup keeps its figures and puts a + beside them
    with sqlite3.connect(rec.path) as con:
        con.execute("UPDATE steps SET status = 0 WHERE idx = 0")
    svc.account()
    assert svc.ledger.ingested(rec.path) == 2
    assert len(svc.ledger.incomplete_records()) == 1
    assert svc.ledger.rollup("seat")[0].turns == 2 and svc.ledger.rollup("seat")[0].unmeasured >= 1
    svc.ledger.close()


def test_account_keeps_sparse_ids_and_missing_owned_records_visible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from quarterdeck.harness.antigravity import AntigravityHarness
    from quarterdeck.harness.base import State, Tokens, Turn
    from quarterdeck.ledger import table
    from quarterdeck.seat import Seat
    from quarterdeck.ship import Pool, SeatSpec, Session, Ship

    record = tmp_path / "synthetic.db"
    record.write_bytes(b"placeholder")
    stamp = datetime.now(timezone.utc)
    later = Turn(4, stamp, stamp, "later", "reply", "m", Tokens(0, 11, 0, 1))
    earlier = Turn(0, stamp, stamp, "earlier", "reply", "m", Tokens(0, 10, 0, 1))
    data = {"turns": [later], "state": State(turns=None)}
    home = tmp_path / "home"
    home.mkdir()
    (home / "conversations.owned").write_text(str(record) + "\n")
    harness = AntigravityHarness()
    monkeypatch.setattr(harness, "read", lambda r: (data["state"], data["turns"]) if r.exists() else
                        (_ for _ in ()).throw(OSError("record missing")))
    pool = Pool("pool-a", "antigravity")
    spec = SeatSpec("worker", "crew-synthetic", "worker", "antigravity", (pool.name,))
    ship = Ship(tmp_path, "ship-synthetic", "operator-synthetic", tmp_path, {pool.name: pool},
                {"crew-synthetic": Session("crew-synthetic", "Synthetic Crew")}, {spec.name: spec})
    seat = Seat(ship, spec, None)
    seat.home, seat.harness = home, harness
    monkeypatch.setattr(seat, "pane", lambda: None)
    monkeypatch.setattr(seat, "launched", lambda: None)
    monkeypatch.setattr(seat, "record", lambda: None)
    from quarterdeck import service
    monkeypatch.setattr(service, "seats", lambda *a: [seat])
    monkeypatch.setattr(service.quota, "refresh_declared", lambda service: None)
    from quarterdeck.record_owner import register
    register(seat.trusted, record, "antigravity")
    svc = Service(ship, None)
    svc.account()
    assert svc.ledger.ingested(record) == 1
    data.update(turns=[earlier, later], state=State(turns=2))
    record.write_bytes(b"placeholder changed")
    svc.account()
    assert svc.ledger.db.execute("SELECT turn,read_uncached FROM turns ORDER BY turn").fetchall() == [(0, 10), (4, 11)]
    assert svc.ledger.incomplete_records() == []
    svc.account()
    assert svc.ledger.ingested(record) == 2
    record.unlink()
    assert harness.records(home) == [record], "missing owned record must remain enumerable"
    svc.account()
    assert "record missing" in "\n".join(table(svc.ledger, "seat"))
    assert svc.ledger.rollup("seat")[0].unmeasured >= 1
    svc.ledger.close()


def test_a_blind_attempt_is_judged_delivered_once_a_record_shows_the_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from quarterdeck.harness.synthetic_record import ClaudeShape
    from quarterdeck.ship import Pool, SeatSpec, Session, Ship
    from quarterdeck.tmux import Pane

    pool = Pool("claude-secondary", "claude", config_dir=tmp_path / "pool")
    spec = SeatSpec("scott", "bridge", "worker", "claude", ("claude-secondary",), cwd=tmp_path)
    ship = Ship(tmp_path, "t", "operator", tmp_path, {"claude-secondary": pool}, {"bridge": Session("bridge", "qd")}, {"scott": spec})
    fake_tmux = SimpleNamespace(pane=lambda pane_id: Pane("%1", "qd", "scott", 4242, 80, 24, False, False, None), capture=lambda *a, **k: "")
    svc = Service(ship, fake_tmux)
    seat = svc.seat("scott")
    seat.folder.mkdir(parents=True, exist_ok=True)
    seat.trusted.mkdir(parents=True, exist_ok=True)
    launched_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    (seat.trusted / "launch.json").write_text(json.dumps({"pane": "%1", "pid": 4242, "at": launched_at.isoformat(),
                                                         "pool": "claude-secondary", "resume": None, "argv": ["claude"], "version": "2.1.317"}))
    record = ClaudeShape(seat.home, tmp_path, "s1", "claude-opus-5-5")
    (seat.trusted / "record").write_text(f"{record.path}\n")
    message = svc.store.new("order", "desk", "scott", "Count the stars.")
    attempt = svc.attempts.open(message, "scott", "%1", 4242, launched_at.isoformat())
    svc.attempts.close(attempt, "blind", "typed and submitted, not seen in the record within 5s")
    assert svc.attempts.blind_ids() == {message.id}
    svc.reconcile()
    assert svc.attempts.blind_ids() == {message.id}, "the record does not show it yet: still blind, and never resent"  # law: one-door
    record.begin(post.typed([message]), "claude-opus-5-5", "high")
    record.end("counted")
    svc.reconcile()
    assert svc.attempts.blind_ids() == {message.id}, "the records are read for this once a minute, not every tick"
    svc.blind_checked = -1e9
    svc.reconcile()
    assert svc.attempts.blind_ids() == set()
    assert svc.attempts.for_message(message.id)[-1].detail == "found in the record later"
    svc.ledger.close()
