"""The accounting alarm and the recovery of a standalone usage commit, on the real Service.account and SQLite.

Synthetic records and readers only; a second connection holding a read lock is what refuses a commit.
"""

from __future__ import annotations

import errno
import json
import sqlite3
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest

from quarterdeck import accounting_health
from quarterdeck.accounting_slices import Driver
from quarterdeck.attribution_cache import Attribution
from quarterdeck.harness.limits import Usage, Window
from quarterdeck.ledger import UsageCommitUnproved
from .test_ledger_pass_boundaries import AT, grow, world

SECRET = 'synthetic record body q7 that must never reach an alarm'


def events(w, name):
    return [data for event, data in w.logs if event == name]


def snapshot(w):
    return json.loads((w.svc.runtime / accounting_health.NAME).read_text())


def reading(w, monkeypatch):
    """The status reading of the snapshot, with this process standing in for the live service."""
    data = snapshot(w)
    monkeypatch.setattr(accounting_health.servicectl, 'read_heartbeat', lambda runtime: (data['pid'], 0.0))
    monkeypatch.setattr(accounting_health.servicectl, 'alive', lambda runtime: True)
    monkeypatch.setattr(accounting_health, 'time', SimpleNamespace(time=lambda: data['observed']))
    return accounting_health.reading(w.svc.runtime)


@contextmanager
def held_reader(svc):
    """Another connection holding a read lock: the writer's COMMIT is refused as busy while it stands."""
    other = sqlite3.connect(f'file:{svc.ledger.path}?mode=ro', uri=True)
    other.execute('BEGIN')
    other.execute('SELECT COUNT(*) FROM turns').fetchone()
    svc.ledger.db.execute('PRAGMA busy_timeout=20')
    try:
        yield other
    finally:
        other.rollback()
        other.close()


def failing_replay(monkeypatch):
    def replay(self, *args, **kwargs):
        raise RuntimeError(SECRET)
    monkeypatch.setattr(Attribution, 'replay', replay)


def test_a_failure_inside_the_pass_raises_the_alarm_after_the_bound_and_clears_on_recovery(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    svc, record = w.svc, w.paths[0]
    svc.account()
    grow(w, record)
    with monkeypatch.context() as patch:
        failing_replay(patch)
        for count in (1, 2):
            svc.account()
            assert svc.accounting_health.failures == count and events(w, 'accounting-failing') == []
            assert reading(w, patch) is None, 'below the bound a transient failure raises nothing'
        svc.account()
        (onset,) = events(w, 'accounting-failing')
        alarm = reading(w, patch)
        assert alarm == f"accounting failing since {onset['since']}: accounting exception"
        for _ in range(3):
            svc.account()
        assert len(events(w, 'accounting-failing')) == 1, 'one line when it starts, not one a pass'
        assert svc.ledger.ingested(record) == 1, 'nothing of a failed pass is accounted'
    svc.account()
    (cleared,) = events(w, 'accounting-recovered')
    assert cleared['failures'] == 6 and svc.accounting_health.failures == 0
    assert svc.ledger.ingested(record) == 2, 'the first committed pass accounts what the failed ones held'
    assert reading(w, monkeypatch) is None
    svc.account()
    assert len(events(w, 'accounting-recovered')) == 1


def test_a_single_transient_failure_raises_and_logs_nothing(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    svc = w.svc
    with monkeypatch.context() as patch:
        failing_replay(patch)
        svc.account()
    svc.account()
    assert events(w, 'accounting-failing') == [] and events(w, 'accounting-recovered') == []
    assert svc.accounting_health.failures == 0 and reading(w, monkeypatch) is None


@pytest.mark.parametrize(('kind', 'arguments', 'error_type', 'message'), [
    (RuntimeError, (SECRET * 100,), 'RuntimeError', 'accounting exception'),
    (type(SECRET, (RuntimeError,), {}), (SECRET * 100,), 'Exception', 'accounting exception'),
    (OSError, (errno.EIO, SECRET * 100), 'OSError', 'file I/O error (EIO)'),
], ids=['runtime', 'custom', 'errno'])
def test_the_alarm_never_names_a_records_content(tmp_path, monkeypatch, kind, arguments, error_type, message):
    w = world(tmp_path, monkeypatch)
    svc = w.svc
    with monkeypatch.context() as patch:
        def replay(*args, **kwargs):
            raise kind(*arguments)
        patch.setattr(Attribution, 'replay', replay)
        for _ in range(4):
            svc.account()
        alarm = reading(w, patch)
    assert alarm and SECRET not in alarm
    assert SECRET not in json.dumps(snapshot(w)) and SECRET not in json.dumps(w.logs, default=str)
    (onset,) = events(w, 'accounting-failing')
    assert onset['error_type'] == error_type and onset['message'] == message


def usage(svc):
    svc.ledger.record_usage('synthetic-pool-q', Usage((Window('7d', .4, AT + timedelta(days=3)),), AT), AT)


def test_a_usage_commit_refused_once_leaves_no_lasting_halt(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    svc, record = w.svc, w.paths[0]
    svc.account()
    grow(w, record)
    with held_reader(svc):
        try:
            usage(svc)
        except sqlite3.OperationalError:
            pass
        else:
            raise AssertionError('the held reader was to refuse the usage commit')
        assert svc.ledger.db.in_transaction, 'the intended write is kept, never rolled back'
    svc.account()
    assert svc.accounting_health.failures == 0 and not svc.ledger.db.in_transaction
    assert svc.ledger.quota.last_attempt('synthetic-pool-q') == AT, 'the pacing attempt was kept'
    assert [r.used for r in svc.ledger.usage.rows('synthetic-pool-q')] == [.4]
    assert svc.ledger.ingested(record) == 2, 'the same pass accounted the turns'


def test_a_usage_commit_that_keeps_failing_raises_the_alarm_and_clears_when_it_succeeds(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    svc, record = w.svc, w.paths[0]
    svc.account()
    grow(w, record)
    with held_reader(svc):
        try:
            usage(svc)
        except sqlite3.OperationalError:
            pass
        for _ in range(3):
            svc.account()
        (onset,) = events(w, 'accounting-failing')
        assert onset['cause'] == 'database busy'
        assert onset['error_type'] == 'OperationalError' and onset['message'] == 'database busy (SQLITE_BUSY)'
        assert reading(w, monkeypatch) == f"accounting failing since {onset['since']}: database busy"
        assert svc.ledger.ingested(record) == 1
    svc.account()
    assert len(events(w, 'accounting-recovered')) == 1 and svc.accounting_health.failures == 0
    assert svc.ledger.quota.last_attempt('synthetic-pool-q') == AT
    assert svc.ledger.ingested(record) == 2 and not svc.ledger.db.in_transaction


class InterruptedCommit:
    """Fault at COMMIT and receipt reads, backed by the ledger's original SQLite connection."""

    def __init__(self, raw, *, commit_then_raise=False):
        self.raw = raw
        self.commit_then_raise = commit_then_raise
        self.fail = True
        self.readable = True
        self.commits = self.reads = 0

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def execute(self, sql, parameters=()):
        if sql.startswith('SELECT attempt FROM usage_commit'):
            assert not self.raw.in_transaction, 'a receipt in the open writer is no proof'
            self.reads += 1
            if not self.readable:
                raise OSError(errno.EIO, SECRET)
        return self.raw.execute(sql, parameters)

    def commit(self):
        self.commits += 1
        if self.fail and not self.commit_then_raise:
            with self.raw:  # COMMIT is refused by a real read lock and sqlite3's context manager rolls back
                pass
        else:
            self.raw.commit()
            if self.fail:
                raise sqlite3.OperationalError(SECRET)


def write_usage(svc, writer):
    if writer == 'ledger':
        usage(svc)
    else:
        svc.ledger.quota.usage_read('synthetic-pool-q', AT, None)


def usage_counts(db):
    return tuple(db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                 for table in ('usage_reads', 'pools', 'usage_samples', 'usage_commit'))


@pytest.mark.parametrize('writer', ['ledger', 'quota'])
@pytest.mark.parametrize('older_identical', [False, True])
def test_rolled_back_usage_commit_reports_failure_and_retries_on_the_same_connection(tmp_path, monkeypatch,
                                                                               writer, older_identical):
    svc = world(tmp_path, monkeypatch).svc
    book = svc.ledger
    db = book.db
    expected = (1, 1, 1, 1) if writer == 'ledger' else (1, 0, 0, 1)
    try:
        if older_identical:
            write_usage(svc, writer)
        before = db.execute('SELECT attempt FROM usage_commit').fetchone()
        book.db = fault = InterruptedCommit(db)
        with held_reader(svc):
            with pytest.raises(sqlite3.OperationalError) as raised:
                write_usage(svc, writer)
            assert raised.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
        assert not db.in_transaction and fault.reads == 1
        assert db.execute('SELECT attempt FROM usage_commit').fetchone() == before
        assert usage_counts(db) == (expected if older_identical else (0, 0, 0, 0))
        book.prepare_usage()  # the known rollback released the owner on the failing pass
        fault.fail = False
        write_usage(svc, writer)
        assert book.db.raw is db and book.quota.last_attempt('synthetic-pool-q') == AT
        assert usage_counts(db) == expected
        assert db.execute('SELECT attempt FROM usage_commit').fetchone() != before
        assert fault.commits == 2
    finally:
        book.close()


@pytest.mark.parametrize('writer', ['ledger', 'quota'])
@pytest.mark.parametrize('delayed_proof', [False, True])
def test_raised_commit_with_a_committed_receipt_counts_once(tmp_path, monkeypatch, writer, delayed_proof):
    svc = world(tmp_path, monkeypatch).svc
    book = svc.ledger
    db = book.db
    book.db = fault = InterruptedCommit(db, commit_then_raise=True)
    fault.readable = not delayed_proof
    try:
        if delayed_proof:
            with pytest.raises(UsageCommitUnproved):
                write_usage(svc, writer)
            fault.readable = True
        else:
            write_usage(svc, writer)
        book.prepare_usage()
        assert book._usage_commit_pending is None and fault.commits == 1
        assert fault.reads == (2 if delayed_proof else 1)
        assert usage_counts(db) == ((1, 1, 1, 1) if writer == 'ledger' else (1, 0, 0, 1))
    finally:
        book.close()


@pytest.mark.parametrize('mode', ['direct', 'active-slice', 'idle-slice'])
def test_unreadable_usage_proof_is_rechecked_and_alarms_in_accounting(tmp_path, monkeypatch, mode):
    w = world(tmp_path, monkeypatch)
    svc = w.svc
    book = svc.ledger
    db = book.db
    book.db = fault = InterruptedCommit(db)
    fault.readable = False
    driver = None
    gate_checks = []
    try:
        with held_reader(svc):
            with pytest.raises(UsageCommitUnproved, match='outcome unreadable'):
                usage(svc)
        if mode != 'direct':
            svc._accounting_tick = True
            svc.account_driver = driver = Driver()
            driver.active = mode == 'active-slice'
            if mode == 'idle-slice':
                def idle_gate(*args, **kwargs):
                    gate_checks.append(True)
                    return False
                monkeypatch.setattr(driver.gate, 'allowed', idle_gate)
        for _ in range(3):
            svc.account()
        assert fault.reads == 4 and fault.commits == 1 and not db.in_transaction
        (onset,) = events(w, 'accounting-failing')
        assert onset['message'] == onset['cause'] == 'standalone usage commit outcome unreadable'
        assert onset['error_type'] == 'UsageCommitUnproved'
        assert reading(w, monkeypatch) == f"accounting failing since {onset['since']}: {onset['cause']}"
        assert SECRET not in json.dumps(w.logs, default=str)

        fault.readable = True
        svc.account()
        assert fault.reads == 5 and fault.commits == 1
        assert svc.accounting_health.current == 'standalone usage commit rolled back'
        assert svc.accounting_health.failures == 4 and book._usage_commit_pending is None
        assert not gate_checks, 'the idle gate could not skip an unresolved usage owner'
        assert usage_counts(db) == (0, 0, 0, 0)
        fault.fail = False
        usage(svc)
        assert usage_counts(db) == (1, 1, 1, 1)
        if mode == 'idle-slice':
            svc.account()
            assert gate_checks == [True] and fault.reads == 5, 'with no pending owner the quiet gate works as before'
    finally:
        if driver is not None:
            driver.close()
        book.close()
