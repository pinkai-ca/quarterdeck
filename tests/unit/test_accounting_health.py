"""Ordinary state/display scaffolding; independent service/DB fault gates live separately."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import accounting_health, quota, service, watch
from quarterdeck.harness.limits import Usage
from quarterdeck.ledger import Ledger


AT = datetime(2025, 2, 3, 10, 0, tzinfo=timezone.utc)


def test_new_process_has_no_accounting_observation():
    health = accounting_health.Health()
    assert not health.checked
    assert health.failures == 0
    assert health.since is None


def test_transient_is_below_alarm_bound_and_next_commit_resets():
    health = accounting_health.Health()
    assert not health.failed('database busy', AT)
    assert not health.committed()
    assert health.checked
    assert (health.failures, health.since, health.current, health.causes) == (0, None, None, {})


def test_mixed_causes_are_one_consecutive_streak():
    health = accounting_health.Health()
    assert not health.failed('database busy', AT)
    assert not health.failed('source unavailable', AT + timedelta(seconds=1))
    assert health.failed('database busy', AT + timedelta(seconds=2))
    assert health.since == AT.isoformat(timespec='seconds')
    assert health.current == 'database busy'
    assert health.causes == {'database busy': 2, 'source unavailable': 1}


def test_sustained_streak_signals_one_onset_and_one_clear():
    health = accounting_health.Health()
    onsets = [health.failed('file I/O error', AT + timedelta(seconds=index)) for index in range(6)]
    assert onsets == [False, False, True, False, False, False]
    assert health.committed()
    assert not health.committed()


def test_recovery_gives_next_streak_its_own_start():
    health = accounting_health.Health()
    health.failed('database locked', AT)
    health.committed()
    later = AT + timedelta(minutes=1)
    health.failed('database locked', later)
    assert health.since == later.isoformat(timespec='seconds')
    assert health.causes == {'database locked': 1}


@pytest.mark.parametrize('failures', [0, 1, 2, 3, 5])
def test_current_owned_snapshot_reading(tmp_path, monkeypatch, failures):
    data = dict(format=1, pid=4107, process_start='209', observed=AT.timestamp(), checked=True,
                failures=failures, since=AT.isoformat(timespec='seconds') if failures else None,
                cause='database busy' if failures else None,
                causes={'database busy': failures} if failures else {})
    (tmp_path / accounting_health.NAME).write_text(json.dumps(data))
    monkeypatch.setattr(accounting_health, 'time', SimpleNamespace(time=lambda: AT.timestamp()))
    monkeypatch.setattr(accounting_health.servicectl, 'read_heartbeat', lambda runtime: (4107, 0.0))
    monkeypatch.setattr(accounting_health.servicectl, 'alive', lambda runtime: True)
    monkeypatch.setattr(accounting_health.presentation, 'process_start', lambda pid: '209')
    expected = (f"accounting failing since {data['since']}: database busy" if failures >= 3 else None)
    assert accounting_health.reading(tmp_path) == expected


def test_alarm_remains_in_status_line_when_other_alerts_overflow():
    alarm = f'accounting failing since {AT.isoformat(timespec="seconds")}: database busy'
    facts = watch.Facts(service='ok', waiting=0, oldest=0, blind=0, accounting=alarm,
                        needs_human=['synthetic-reader-' + str(index) for index in range(30)])
    assert alarm in facts.line()
    assert '30 need human' in facts.line()
    assert alarm in watch.render_table(dict(service='ok', seats=[], open_orders=[], facts=facts, usage=[]))


def test_committed_pass_clears_before_ordinary_usage_refresh(tmp_path, monkeypatch):
    worker = service.Service.__new__(service.Service)
    worker.runtime = tmp_path
    worker.accounting_health = accounting_health.Health()
    for _ in range(3):
        worker.accounting_health.failed('database busy', AT)
    events, seen = [], []
    worker.log = lambda event, **data: events.append(event)
    worker.publish_accounting_health = lambda **kwargs: seen.append(('display', worker.accounting_health.failures))
    worker.account_records = lambda: ([], [], [])
    monkeypatch.setattr(quota, 'refresh_declared', lambda service: seen.append(('usage', service.accounting_health.failures)))
    worker.account()
    worker.account()
    assert events == ['accounting-recovered']
    assert seen == [('display', 0), ('usage', 0), ('display', 0), ('usage', 0)]


def test_ordinary_usage_and_record_pass_keep_both_transactions(tmp_path):
    with Ledger(tmp_path / 'book.sqlite') as book:
        book.record_usage('synthetic-pool-alpha', Usage((), AT, 'synthetic unavailable', called=False), AT)
        with book.record_pass() as staged:
            staged.mark_record(Path('synthetic-record-alpha'), 'synthetic-seat-alpha',
                               'synthetic-pool-alpha', 'synthetic unavailable', commit=False)
        assert book.quota.last_attempt('synthetic-pool-alpha') == AT
        assert book.db.execute('SELECT seat FROM records').fetchall() == [('synthetic-seat-alpha',)]
        assert not book.db.in_transaction


def test_direct_ledger_quota_attempt_retains_normal_api(tmp_path):
    with Ledger(tmp_path / 'book.sqlite') as book:
        book.quota.usage_read('synthetic-pool-beta', AT, None)
        with book.record_pass():
            pass
        assert book.quota.last_attempt('synthetic-pool-beta') == AT
        assert not book.db.in_transaction


def test_usage_preparation_precedes_ordinary_reader_call():
    seen = []
    worker = SimpleNamespace(ledger=SimpleNamespace(prepare_usage=lambda: seen.append('prepared')))
    reading = Usage((), AT, called=False)
    reader = SimpleNamespace(usage=lambda pool: seen.append('read') or reading)
    assert quota.read_usage(worker, SimpleNamespace(name='synthetic-pool-gamma'), reader) is reading
    assert seen == ['prepared', 'read']
