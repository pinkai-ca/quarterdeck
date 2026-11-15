"""Independent gates for the service's one record transaction per accounting pass.

Real SQLite and the real ``Service.account``; only the program's record reader, the
process table and the monotonic clock are supplied. Everything here is synthetic.
"""

from __future__ import annotations

import json
import sqlite3
import time as real_time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from quarterdeck import attribution_cache, record_owner, service, turn_failure
from quarterdeck.attribution_cache import Attribution
from quarterdeck.harness.base import State, Tokens, ToolCall, Turn
from quarterdeck.harness.fake import FakeHarness
from quarterdeck.harness.limits import Wall
from quarterdeck.ledger import Ledger
from quarterdeck.service import ACCOUNT_RETRY, Service
from quarterdeck.ship import Ship

AT = datetime(2026, 1, 1, tzinfo=timezone.utc)
SEAT = 'synthetic-seat-q'
WRITES = ('BEGIN', 'COMMIT', 'INSERT', 'REPLACE', 'UPDATE', 'DELETE', 'CREATE', 'ALTER', 'DROP', 'PRAGMA')


def writes(trace):
    return [sql for sql in trace if sql.lstrip().upper().startswith(WRITES)]


def turn(index):
    return Turn(index, AT + timedelta(seconds=10 * index), AT + timedelta(seconds=10 * index + 5),
                'synthetic input q', 'synthetic reply q', 'synthetic-model-q', Tokens(1, 2, 3, 4),
                (ToolCall('synthetic-tool-q', 0.5, False),), cost=0.25)


class Clock:
    """The time module, with a monotonic clock the test moves."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def __getattr__(self, name):
        return getattr(real_time, name)


def world(tmp_path, monkeypatch, *, records=('record-q',), human=False):
    """One seat with record files whose readings the test sets; each entry is (state, turns) or an exception."""
    ship = Ship(tmp_path, 'synthetic', 'operator', tmp_path, {}, {}, {})
    svc = Service(ship, SimpleNamespace())
    folder = ship.runtime / 'seats' / SEAT
    home = folder / 'home'
    home.mkdir(parents=True)
    trusted = ship.runtime / 'trusted' / SEAT
    trusted.mkdir(parents=True)
    reader = FakeHarness()
    paths = []
    for name in records:
        path = home / name
        path.write_text(f'synthetic {name} first revision')
        record_owner.register(trusted, path, 'fake')
        paths.append(path)
    (trusted / 'record-launches.jsonl').write_text(json.dumps(dict(
        record=str(paths[0]), at=(AT - timedelta(minutes=1)).isoformat(), pool='synthetic-pool-q', harness='fake')) + '\n')
    sources = {path: (State(turns=1, turn_open=False), [turn(0)]) for path in paths}
    inputs = {path: [] for path in paths}
    reads, input_reads, logs, published = [], [], [], []

    def read(path):
        assert not svc.ledger.db.in_transaction, 'record reads precede the record transaction'
        reads.append(path)
        found = sources[path]
        if isinstance(found, Exception):
            raise found
        return found

    def attribution_inputs(self, path):
        input_reads.append(path)
        found = inputs[path]
        if isinstance(found, Exception):
            raise found
        return found

    owned = {path: record_owner.Owned(path, 'fake', reader) for path in paths}
    seat = SimpleNamespace(name=SEAT, home=home, folder=folder, trusted=trusted, harness=reader,
                           session=SimpleNamespace(human=human), pane=lambda: None,
                           launched=lambda: None, record=lambda: paths[0])
    clock = Clock()
    monkeypatch.setattr(service, 'time', clock)
    monkeypatch.setattr(attribution_cache, 'time', clock)
    monkeypatch.setattr(reader, 'read', read)
    monkeypatch.setattr(FakeHarness, 'attribution_inputs', attribution_inputs)
    monkeypatch.setattr(service.machine, 'processes', lambda: {})
    monkeypatch.setattr(svc, 'resolved_seats', lambda: [seat])
    monkeypatch.setattr(svc, 'log', lambda event, **data: logs.append((event, data)))
    monkeypatch.setattr(record_owner, 'records', lambda who: list(owned.values()))
    monkeypatch.setattr(turn_failure, 'publish', lambda *args: published.append(args[1]))
    return SimpleNamespace(svc=svc, seat=seat, paths=paths, sources=sources, inputs=inputs, owned=owned,
                           reads=reads, input_reads=input_reads, logs=logs, published=published, clock=clock,
                           journal=trusted / 'record-launches.jsonl')


def counts(db):
    return tuple(db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                 for table in ('turns', 'record_launches', 'wall_events', 'records', 'turn_attribution'))


def caches(svc):
    """Everything the pass may only publish after its commit."""
    return (dict(svc.ledger.provenance.sources), dict(svc.ledger.attributions.sources),
            dict(svc.ledger.attributions.retries), svc.ledger.attributions.last,
            dict(svc.read_marks), dict(svc.account_errors))


def grow(w, path, *, wall=False):
    """A second completed turn (and optionally a wall) in a record whose bytes changed."""
    state = State(turns=2, turn_open=False,
                  walls=(Wall('synthetic-wall-q', AT, AT + timedelta(hours=1), signature='synthetic-q'),) if wall else ())
    w.sources[path] = (state, [turn(0), turn(1)])
    path.write_text(f'synthetic {path.name} second revision, longer')


def test_a_pass_that_fails_after_staged_writes_rolls_back_rows_and_publishes_no_progress(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, human=True)
    svc, record = w.svc, w.paths[0]
    svc.account()
    baseline, before = counts(svc.ledger.db), caches(svc)
    assert baseline == (1, 1, 0, 0, 1) and w.published == [record]
    assert svc.ledger.attributions.last is not None
    marks, errors, meters = svc.read_marks, svc.account_errors, svc.meters
    logged = list(w.logs)
    # New record bytes, a new wall and a new launch: every staged cache would move if published.
    grow(w, record, wall=True)
    w.journal.write_text(w.journal.read_text() + json.dumps(dict(
        record=str(record), at=AT.isoformat(), pool='synthetic-pool-r', harness='fake')) + '\n')
    original = Attribution.replay

    def replay_then_fail(self, *args, **kwargs):
        original(self, *args, **kwargs)
        assert self.db.in_transaction and counts(self.db)[:3] == (2, 2, 1), 'the rows were staged'
        raise RuntimeError('synthetic failure after staged progress')

    with monkeypatch.context() as patch:
        patch.setattr(Attribution, 'replay', replay_then_fail)
        svc.account()  # the pass fails; the service counts it rather than raising
    assert (svc.accounting_health.failures, svc.accounting_health.current) == (1, 'accounting exception')
    assert not svc.ledger.db.in_transaction
    assert counts(svc.ledger.db) == baseline, 'no row of the failed pass survives'
    assert caches(svc) == before, 'no fingerprint, retry state or attribution stamp ran ahead of the rows'
    assert svc.read_marks is marks and svc.account_errors is errors and svc.meters is meters
    assert w.logs == logged, 'no ledger notice for turns that were never committed'
    assert w.published == [record], "a human seat's reading is published only after its commit"
    # The next pass reads the record again and commits everything once.
    trace = []
    svc.ledger.db.set_trace_callback(trace.append)
    svc.account()
    svc.ledger.db.set_trace_callback(None)
    assert trace.count('COMMIT') == 1
    assert counts(svc.ledger.db) == (2, 2, 1, 0, 2)
    assert w.reads == [record, record, record]
    assert w.published == [record, record]
    assert [event for event, _ in w.logs[len(logged):]] == ['ledger']
    assert svc.ledger.db.execute('SELECT COUNT(DISTINCT pool) FROM record_launches').fetchone() == (2,)
    svc.ledger.close()


def test_a_commit_refused_by_a_concurrent_reader_rolls_back_and_the_next_pass_completes(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    svc, record = w.svc, w.paths[0]
    svc.account()
    baseline, before = counts(svc.ledger.db), caches(svc)
    grow(w, record)
    status = sqlite3.connect(f'file:{svc.ledger.path}?mode=ro', uri=True)
    try:
        status.execute('BEGIN')
        assert status.execute('SELECT COUNT(*) FROM turns').fetchone() == (1,)  # holds its read lock
        svc.ledger.db.execute('PRAGMA busy_timeout=50')
        svc.account()  # the commit is refused; the service counts it rather than raising
        assert (svc.accounting_health.failures, svc.accounting_health.current) == (1, 'database busy')
        assert status.execute('SELECT COUNT(*) FROM turns').fetchone() == (1,), 'the reader saw one consistent book'
    finally:
        status.rollback()
        status.close()
    assert not svc.ledger.db.in_transaction, 'a refused commit leaves no transaction open for the next pass'
    assert counts(svc.ledger.db) == baseline and caches(svc) == before
    svc.account()
    assert counts(svc.ledger.db)[0] == 2 and w.reads == [record, record, record]
    svc.ledger.close()


def test_a_reader_during_a_pass_sees_only_committed_passes_and_sets_nothing(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    svc, record = w.svc, w.paths[0]
    seen, opened = [], []
    original, connect = Attribution.replay, sqlite3.connect

    def traced(*args, **kwargs):
        connection = connect(*args, **kwargs)
        trace = []
        connection.set_trace_callback(trace.append)
        opened.append(trace)
        return connection

    def replay_with_reader(self, *args, **kwargs):
        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, 'connect', traced)
            reading = Ledger.reading(svc.ledger.path)
        with reading:
            seen.append(counts(reading.db))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Attribution, 'replay', replay_with_reader)
    svc.account()
    assert seen == [(0, 0, 0, 0, 0)]
    grow(w, record, wall=True)
    svc.account()
    assert seen[1] == (1, 1, 0, 0, 1), 'the second pass is invisible until its commit'
    with Ledger.reading(svc.ledger.path) as reading:
        assert counts(reading.db) == (2, 1, 1, 0, 2)
    assert opened and all(writes(trace) == [] for trace in opened), 'a reading sets no pragma and changes no schema'
    assert svc.ledger.db.execute('PRAGMA synchronous').fetchone() == (0,)
    svc.ledger.close()


def test_an_unchanged_known_reader_failure_is_retried_once_a_minute_and_never_rewritten(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, records=('record-q', 'record-r'))
    svc, (failing, healthy) = w.svc, w.paths
    w.sources[failing] = OSError('synthetic transient reading q')
    trace = []
    svc.ledger.db.set_trace_callback(trace.append)
    svc.account()
    assert trace.count('COMMIT') == 1, 'a failed record and a healthy one share the one record commit'
    assert svc.ledger.ingested(healthy) == 1 and svc.ledger.ingested(failing) == 0
    assert [r[0] for r in svc.ledger.incomplete_records()] == [str(failing)]
    assert [event for event, _ in w.logs].count('error') == 1
    assert sorted(w.reads) == sorted([failing, healthy])
    for step in (30, ACCOUNT_RETRY - 31):  # still inside the minute: no read, no write, no notice
        w.clock.now += step
        trace.clear()
        svc.account()
        assert writes(trace) == [] and w.reads.count(failing) == 1
    w.clock.now += 1.5  # past the minute, the same failure: read once, nothing rewritten or logged again
    trace.clear()
    svc.account()
    assert w.reads.count(failing) == 2 and writes(trace) == []
    assert [event for event, _ in w.logs].count('error') == 1
    assert [r[0] for r in svc.ledger.incomplete_records()] == [str(failing)], 'the unread record stays marked'
    # The reader recovers, but nothing about the record changed and the minute has not passed again.
    w.sources[failing] = (State(turns=1, turn_open=False), [turn(0)])
    w.clock.now += 1
    svc.account()
    assert w.reads.count(failing) == 2
    # Changed record bytes are new evidence: read at once, and the mark is cleared in the same commit.
    failing.write_text('synthetic record-q changed while the minute runs')
    trace.clear()
    svc.account()
    assert w.reads.count(failing) == 3 and trace.count('COMMIT') == 1
    assert svc.ledger.ingested(failing) == 1 and svc.ledger.incomplete_records() == []
    assert w.reads.count(healthy) == 1, 'the healthy record was never read again'
    svc.ledger.close()


def test_an_unknown_reader_stays_visibly_unresolved_until_its_evidence_changes(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    svc, record = w.svc, w.paths[0]
    reason = 'synthetic reader unknown q'
    w.owned[record] = record_owner.Owned(record, None, None, reason)
    attempts = []
    original = record_owner.Owned.read
    monkeypatch.setattr(record_owner.Owned, 'read', lambda self: attempts.append(self.path) or original(self))
    svc.account()
    assert attempts == [] and svc.ledger.ingested(record) == 0, 'a record no reader can read is settled, not parsed'
    assert [(r, e) for r, _, _, e in svc.ledger.incomplete_records()] == [(str(record), reason)]
    trace = []
    svc.ledger.db.set_trace_callback(trace.append)
    for _ in range(3):
        w.clock.now += 3600
        svc.account()
    assert attempts == [] and writes(trace) == [], 'no retry clock applies without new evidence'
    assert [(r, e) for r, _, _, e in svc.ledger.incomplete_records()] == [(str(record), reason)], \
        'an unknown turn count is never shown as zero'
    assert [event for event, _ in w.logs].count('error') == 1
    record.write_text('synthetic record bytes moved')
    trace.clear()
    svc.account()
    assert attempts == [] and [event for event, _ in w.logs].count('error') == 2, 'changed bytes are settled again, once'
    assert trace.count('COMMIT') == 1
    # Once its reader becomes known the record is read and accounted in one commit.
    w.owned[record] = record_owner.Owned(record, 'fake', w.seat.harness)
    trace.clear()
    svc.account()
    assert trace.count('COMMIT') == 1
    assert svc.ledger.ingested(record) == 1 and svc.ledger.incomplete_records() == []
    svc.ledger.close()


def test_the_service_retries_an_unchanged_failed_attribution_source_once_a_minute(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    svc, record = w.svc, w.paths[0]
    w.inputs[record] = OSError('synthetic attribution source q')
    svc.account()
    assert w.input_reads == [record]
    (detail,) = svc.ledger.attributions.details()
    assert detail['status'] != 'pending' and detail['orders'] == [], 'the failed source was replayed and recorded'
    trace = []
    svc.ledger.db.set_trace_callback(trace.append)
    w.clock.now += ACCOUNT_RETRY - 1
    svc.account()
    assert w.input_reads == [record] and writes(trace) == []
    w.inputs[record] = []
    w.clock.now += 2
    svc.account()
    assert w.input_reads == [record, record]
    w.clock.now += ACCOUNT_RETRY * 5
    trace.clear()
    svc.account()
    assert w.input_reads == [record, record] and writes(trace) == [], 'a recovered source is not read again'
    svc.ledger.close()


def test_an_older_writer_after_rollback_is_reconciled_through_the_existing_triggers(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    svc, record = w.svc, w.paths[0]
    svc.account()
    assert svc.ledger.db.execute('SELECT valid FROM turn_attribution').fetchall() == [(1,)]
    # An older writer: its own connection settings, the same tables, no knowledge of the pass boundary.
    older = sqlite3.connect(svc.ledger.path)
    older.execute('UPDATE turns SET seconds=seconds+1 WHERE record=?', (str(record),))
    older.commit()
    older.close()
    assert svc.ledger.db.execute('SELECT valid FROM turn_attribution').fetchall() == [(0,)]
    trace = []
    svc.ledger.db.set_trace_callback(trace.append)
    svc.account()
    assert trace.count('COMMIT') == 1 and w.reads == [record]
    assert svc.ledger.db.execute('SELECT valid FROM turn_attribution').fetchall() == [(1,)]
    trace.clear()
    svc.account()
    assert writes(trace) == []
    svc.ledger.close()
    # A newer writer reopening the book after it keeps the journal mode and needs no migration.
    with Ledger(svc.ledger.path) as book:
        assert book.db.execute('PRAGMA journal_mode').fetchone() == ('delete',)
        assert book.db.execute('PRAGMA synchronous').fetchone() == (0,)
        assert counts(book.db) == (1, 1, 0, 0, 1)


def test_record_pass_refuses_to_nest_in_an_open_transaction(tmp_path):
    with Ledger(tmp_path / 'ledger.sqlite') as book:
        book.mark_record(tmp_path / 'record-q', SEAT, 'synthetic-pool-q', 'synthetic note q', commit=False)
        assert book.db.in_transaction
        with pytest.raises(RuntimeError):
            with book.record_pass():
                pass
        book.db.rollback()
        with book.record_pass() as staged:
            staged.mark_record(tmp_path / 'record-q', SEAT, 'synthetic-pool-q', 'synthetic note q', commit=False)
            assert book.db.in_transaction
        assert not book.db.in_transaction
        assert book.incomplete_records()[0][3] == 'synthetic note q'
