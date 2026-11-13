"""A record whose reader cannot be established is settled once, not read and logged at every pass. All synthetic."""

from __future__ import annotations

import sqlite3

from quarterdeck import record_owner
from quarterdeck.ledger import Ledger
from quarterdeck.service import ACCOUNT_RETRY
from .test_ledger_pass_boundaries import world, writes

REASON = 'synthetic reader unknown or conflicting q; historical harness evidence required'


def unknown(w, record, reason=REASON, **extra):
    w.owned[record] = record_owner.Owned(record, None, None, reason, **extra)


def errors(w):
    return [data for event, data in w.logs if event == 'error']


def restart(svc):
    """What a service restart forgets: every in-memory mark and retry clock."""
    svc.read_marks, svc.account_errors = {}, {}


def test_one_settlement_and_no_repeat_across_passes_and_restarts(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    svc, record = w.svc, w.paths[0]
    unknown(w, record)
    svc.account()
    (logged,) = errors(w)
    assert logged['record'] == record.name and logged['error'] == REASON and logged['settled'] is True
    error, evidence = svc.ledger.settled(record)
    assert error == REASON and evidence.startswith('v1:')
    assert [(r, e) for r, _, _, e in svc.ledger.incomplete_records()] == [(str(record), REASON)]
    trace = []
    svc.ledger.db.set_trace_callback(trace.append)
    for _ in range(3):
        w.clock.now += ACCOUNT_RETRY * 60
        restart(svc)
        svc.account()
    assert len(errors(w)) == 1 and writes(trace) == [] and w.reads == []
    assert svc.ledger.today('synthetic-seat-q', '2026-01-01').unmeasured == 1, 'its turns are never shown as zero'


def test_a_change_elsewhere_in_the_seats_registry_is_no_new_attempt(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    svc, record = w.svc, w.paths[0]
    unknown(w, record)
    svc.account()
    record_owner.register(w.seat.home.parent, w.seat.home / 'synthetic-other-q', 'fake')
    svc.account()
    assert len(errors(w)) == 1


def test_a_changed_record_or_reason_is_settled_again_once(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    svc, record = w.svc, w.paths[0]
    unknown(w, record)
    svc.account()
    first = svc.ledger.settled(record)
    record.write_text('synthetic record bytes grew q')
    svc.account()
    svc.account()
    assert len(errors(w)) == 2 and svc.ledger.settled(record) not in (None, first)
    unknown(w, record, 'synthetic reader conflicting q')
    svc.account()
    assert len(errors(w)) == 3 and svc.ledger.settled(record)[0] == 'synthetic reader conflicting q'


def test_a_settled_record_whose_reader_is_established_is_accounted_once(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    svc, record = w.svc, w.paths[0]
    unknown(w, record)
    svc.account()
    assert svc.ledger.ingested(record) == 0
    w.owned[record] = record_owner.Owned(record, 'fake', w.seat.harness)
    svc.account()
    assert svc.ledger.ingested(record) == 1 and svc.ledger.incomplete_records() == []
    assert svc.ledger.settled(record) is None
    restart(svc)
    svc.account()
    assert svc.ledger.ingested(record) == 1, 'reading it again counts its turns once'


def test_an_empty_record_of_unknown_reader_holds_no_turn_and_is_not_reported(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    svc, record = w.svc, w.paths[0]
    record.write_text('')
    unknown(w, record)
    for _ in range(3):
        restart(svc)
        svc.account()
    assert errors(w) == [] and svc.ledger.incomplete_records() == [] and w.reads == []
    assert svc.ledger.today('synthetic-seat-q', '2026-01-01').unmeasured == 0
    record.write_text('synthetic first bytes q')
    svc.account()
    assert len(errors(w)) == 1 and svc.ledger.settled(record)[0] == REASON, 'once it holds bytes it is settled'


def test_a_settled_note_is_cleared_when_its_record_is_found_empty(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    svc, record = w.svc, w.paths[0]
    unknown(w, record)
    svc.account()
    record.write_text('')
    svc.account()
    assert svc.ledger.incomplete_records() == [] and svc.ledger.settled(record) is None


def test_unreadable_ownership_evidence_is_transient_retried_and_never_settled(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    svc, record = w.svc, w.paths[0]
    unknown(w, record, 'synthetic registry unreadable q', transient=True)
    attempts = []
    original = record_owner.Owned.read
    monkeypatch.setattr(record_owner.Owned, 'read', lambda self: attempts.append(self.path) or original(self))
    svc.account()
    assert attempts == [record] and svc.ledger.settled(record) is None
    svc.account()
    assert attempts == [record]
    w.clock.now += ACCOUNT_RETRY + 1
    svc.account()
    assert attempts == [record, record] and len(errors(w)) == 1, 'retried each minute, logged once while it stands'


def test_an_older_book_gains_the_evidence_column_and_keeps_its_notes(tmp_path):
    path = tmp_path / 'ledger.sqlite'
    with Ledger(path) as book:
        book.db.execute('ALTER TABLE records DROP COLUMN evidence')
        book.db.execute("INSERT INTO records VALUES ('synthetic-record-q','seat-q','pool-q','synthetic old note q')")
        book.db.commit()
    with Ledger(path) as book:
        assert [e for _, _, _, e in book.incomplete_records()] == ['synthetic old note q']
        assert book.settled(tmp_path / 'synthetic-record-q') is None, 'an old note is not a settlement'
        assert 'evidence' in {row[1] for row in book.db.execute('PRAGMA table_info(records)')}
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT evidence FROM records').fetchone() == (None,)
