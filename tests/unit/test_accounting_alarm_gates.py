"""The accounting alarm and the recovery of a standalone usage commit, on the real Service.account and SQLite.

Synthetic records and readers only; a second connection holding a read lock is what refuses a commit.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

from quarterdeck import accounting_health
from quarterdeck.attribution_cache import Attribution
from quarterdeck.harness.limits import Usage, Window
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


def test_the_alarm_never_names_a_records_content(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    svc = w.svc
    with monkeypatch.context() as patch:
        failing_replay(patch)
        for _ in range(4):
            svc.account()
        alarm = reading(w, patch)
    assert alarm and SECRET not in alarm
    assert SECRET not in json.dumps(snapshot(w)) and SECRET not in json.dumps(w.logs, default=str)


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
        assert reading(w, monkeypatch) == f"accounting failing since {onset['since']}: database busy"
        assert svc.ledger.ingested(record) == 1
    svc.account()
    assert len(events(w, 'accounting-recovered')) == 1 and svc.accounting_health.failures == 0
    assert svc.ledger.quota.last_attempt('synthetic-pool-q') == AT
    assert svc.ledger.ingested(record) == 2 and not svc.ledger.db.in_transaction
