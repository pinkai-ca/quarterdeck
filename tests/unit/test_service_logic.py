"""The service's pure judgments: whether a record shows that this very message was taken."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from quarterdeck import post
from quarterdeck.fakerecord import ClaudeShape
from quarterdeck.harness.fake import FakeHarness
from quarterdeck.post import Message, Store
from quarterdeck.service import Service


def seat_with(record: Path):
    return SimpleNamespace(record=lambda: record, harness=FakeHarness())


def test_taken_matches_the_message_id_not_a_look_alike_body(tmp_path: Path):
    store = Store(tmp_path)
    earlier = store.new("order", "desk", "scott", "done")
    record = ClaudeShape(tmp_path / "home", Path("/work"), "s1", "fake-1")
    record.begin(post.typed([earlier]), "fake-1", "low")
    record.end("ok")
    since = datetime.now(timezone.utc) - timedelta(seconds=1)
    later = store.new("order", "desk", "scott", "done")  # the same body, another id
    assert Service.taken(seat_with(record.path), earlier, since) is True
    assert Service.taken(seat_with(record.path), later, since) is False  # law: one-door
    # an input older than the attempt proves nothing about it
    assert Service.taken(seat_with(record.path), earlier, datetime.now(timezone.utc) + timedelta(seconds=5)) is False
    # queued, not yet an input: still taken
    record.begin("something else", "fake-1", "low")
    record.queued(post.typed([later]))
    assert Service.taken(seat_with(record.path), later, since) is True


def test_taken_without_a_record_is_false(tmp_path: Path):
    message = Message("ord-1-d-0001", "order", "desk", "scott", post.now(), "x")
    assert Service.taken(SimpleNamespace(record=lambda: None, harness=FakeHarness()), message, post.now()) is False


def test_account_tracks_wal_fingerprint_and_retries_after_transient_read_error(tmp_path: Path, monkeypatch):
    import json
    import sqlite3

    from quarterdeck.fakerecord import AntigravityShape, _pb_bytes, _pb_str, _pb_ts, _pb_uint
    from quarterdeck.harness import antigravity
    from quarterdeck.harness.base import RecordError, record_fingerprint
    from quarterdeck.ship import Pool, SeatSpec, Session, Ship
    from quarterdeck.tmux import Pane

    pool = Pool("google", "antigravity")
    spec = SeatSpec("scott", "bridge", "worker", "antigravity", ("google",), cwd=tmp_path)
    ship = Ship(tmp_path, "enterprise", "admiral", tmp_path, {"google": pool}, {"bridge": Session("bridge", "qd")}, {"scott": spec})
    fake_tmux = SimpleNamespace(pane=lambda pane_id: Pane("%1", "qd", "scott", 4242, 80, 24, False, False, None))
    svc = Service(ship, fake_tmux)
    seat = svc.seat("scott")
    seat.folder.mkdir(parents=True, exist_ok=True)
    rec = AntigravityShape(seat.home, tmp_path, "conv-wal", "gemini-3.1-pro-high")
    (seat.folder / "record").write_text(f"{rec.path}\n")
    (seat.folder / "launch.json").write_text(json.dumps({
        "pane": "%1", "pid": 4242, "at": datetime.now(timezone.utc).isoformat(),
        "pool": "google", "resume": None, "argv": ["agy"], "version": "1.5.2",
    }))

    rec.begin("turn 1", "gemini-3.1-pro-high", "high")
    rec.end("reply 1")

    # Simulate a transient RecordError on the first account() pass: read_marks must NOT be set, so the next pass retries
    orig_read = antigravity.read
    calls = {"n": 0}

    def flaky_read(path: Path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RecordError("transient concurrent write")
        return orig_read(path)

    monkeypatch.setattr(antigravity, "read", flaky_read)
    monkeypatch.setattr(antigravity.AntigravityHarness, "read", staticmethod(flaky_read))
    svc.account()
    assert svc.ledger.ingested(rec.path) == 0 and rec.path not in svc.read_marks
    assert svc.ledger.incomplete_records() == [(str(rec.path), "scott", "google", "transient concurrent write")]
    assert svc.ledger.rollup("seat")[0].turns is None
    svc.account()
    assert svc.ledger.ingested(rec.path) == 1 and rec.path in svc.read_marks
    assert svc.ledger.incomplete_records() == [] and svc.ledger.rollup("seat")[0].turns == 1

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

    # If an earlier step becomes status 0 (unknown turn state), account() keeps the known turn in the ledger
    # and marks the record incomplete so rollup reports turns as None (unknown) rather than a partial count
    with sqlite3.connect(rec.path) as con:
        con.execute("UPDATE steps SET status = 0 WHERE idx = 0")
    svc.account()
    assert svc.ledger.ingested(rec.path) == 2
    assert len(svc.ledger.incomplete_records()) == 1
    assert svc.ledger.rollup("seat")[0].turns is None
    svc.ledger.close()


def test_account_keeps_sparse_ids_and_missing_owned_records_visible(tmp_path, monkeypatch):
    from quarterdeck.harness.antigravity import AntigravityHarness
    from quarterdeck.harness.base import State, Tokens, Turn
    from quarterdeck.ledger import table

    record = tmp_path / 'synthetic.db'
    record.write_bytes(b'placeholder')
    stamp = datetime.now(timezone.utc)
    later = Turn(4,stamp,stamp,'later','reply','m',Tokens(0,11,0,1))
    earlier = Turn(0,stamp,stamp,'earlier','reply','m',Tokens(0,10,0,1))
    data = {'turns':[later], 'state':State(turns=None)}
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'conversations.owned').write_text(str(record)+'\n')
    harness = AntigravityHarness()
    monkeypatch.setattr(harness,'read',lambda r: (data['state'],data['turns']) if r.exists() else
                        (_ for _ in ()).throw(OSError('record missing')))
    seat = SimpleNamespace(name='worker',home=home,harness=harness,pane=lambda:None,launched=lambda:None,
                           pool=SimpleNamespace(name='pool-a'))
    from quarterdeck import service
    monkeypatch.setattr(service,'seats',lambda *a:[seat])
    ship = SimpleNamespace(runtime=tmp_path/'runtime',folder=tmp_path)
    svc = Service(ship,None)
    svc.account()
    assert svc.ledger.ingested(record) == 1
    data.update(turns=[earlier,later],state=State(turns=2))
    record.write_bytes(b'placeholder changed')
    svc.account()
    assert svc.ledger.db.execute('SELECT turn,read_uncached FROM turns ORDER BY turn').fetchall() == [(0,10),(4,11)]
    assert svc.ledger.incomplete_records() == []
    svc.account()
    assert svc.ledger.ingested(record) == 2
    record.unlink()
    assert harness.records(home) == [record], 'missing owned record must remain enumerable'
    svc.account()
    assert 'record missing' in '\n'.join(table(svc.ledger,'seat'))
    assert svc.ledger.rollup('seat')[0].turns is None
    svc.ledger.close()
