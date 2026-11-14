"""Declared-pool scheduling, with the native billing boundary replaced and no provider call."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from quarterdeck import harness, quota
from quarterdeck.harness.limits import Usage, Window
from quarterdeck.ledger import Ledger
from quarterdeck.ship import Pool


@pytest.fixture
def background(tmp_path, monkeypatch):
    clock = SimpleNamespace(seconds=0.0)
    epoch = datetime(2040, 1, 1, tzinfo=timezone.utc)
    class ClockDate(datetime):
        @classmethod
        def now(cls, tz=None):
            return (epoch + timedelta(seconds=clock.seconds)).astimezone(tz)
    monkeypatch.setattr(quota, 'datetime', ClockDate)
    monkeypatch.setattr(quota, 'time', SimpleNamespace(monotonic=lambda: clock.seconds))
    pool = Pool('example-subscription', 'xai', config_dir=tmp_path / 'synthetic-sign-in')
    ship = SimpleNamespace(runtime=tmp_path / 'runtime', pools={pool.name: pool}, seats={})
    ledger = Ledger(ship.runtime / 'ledger.sqlite')
    calls, logs = [], []
    answer = SimpleNamespace(value=Usage((Window('7d', 0.25, epoch + timedelta(days=7)),), epoch))
    reader = harness.native_for(pool.provider)
    def native_boundary(self, actual_pool, home):
        calls.append((actual_pool, home))
        return answer.value
    monkeypatch.setattr(type(reader), 'usage_in_home', native_boundary)
    def service():
        return SimpleNamespace(ship=ship, ledger=ledger, resolved_seats=lambda: [],
                               usage_read={}, usage_hold={}, usage_logged={},
                               log=lambda event, **details: logs.append((event, details)))
    try:
        yield SimpleNamespace(service=service, pool=pool, reader=reader, clock=clock, epoch=epoch,
                              calls=calls, logs=logs, answer=answer, ledger=ledger)
    finally:
        ledger.close()


def test_seatless_grok_pool_is_read_and_paced_across_restart(background):
    example = background
    service = example.service()
    assert service.ship.seats == {}
    assert example.reader.background_usage is True
    assert example.reader.usage_interval == 1800
    quota.refresh_declared(service)
    assert example.calls == [(example.pool, service.ship.runtime / 'pool-usage' / example.pool.name)]
    assert example.ledger.pool_windows(example.pool.name)[0][1:3] == ('7d', 0.25)
    assert example.ledger.quota.last_attempt(example.pool.name) == example.epoch
    # A new service has no in-memory pacing cache; the ledger still prevents an early call.
    service = example.service()
    example.clock.seconds = 1799
    quota.refresh_declared(service)
    assert len(example.calls) == 1
    example.clock.seconds = 1800
    quota.refresh_declared(service)
    assert len(example.calls) == 2
    assert example.ledger.quota.last_attempt(example.pool.name) == example.epoch + timedelta(seconds=1800)


def test_background_grok_respects_retry_after_even_when_interval_is_due(background):
    example = background
    service = example.service()
    example.answer.value = Usage((), example.epoch, 'example refusal', retry_after=2400)
    quota.refresh_declared(service)
    assert len(example.calls) == 1
    assert service.usage_hold[example.pool.name] == 2400
    assert example.logs[-1] == ('usage', {'pool': example.pool.name, 'error': 'example refusal', 'backoff': 2400})
    example.clock.seconds = 1800
    assert example.ledger.quota.due(example.pool.name, example.epoch + timedelta(seconds=1800), 1800)
    quota.refresh_declared(service)
    assert len(example.calls) == 1
    example.clock.seconds = 2400
    example.answer.value = Usage((Window('7d', 0.25, example.epoch + timedelta(days=7)),), example.epoch)
    quota.refresh_declared(service)
    assert len(example.calls) == 2
    assert example.ledger.pool_windows(example.pool.name)[0][1:3] == ('7d', 0.25)


def test_no_call_reading_is_recorded_and_waits_for_the_next_interval(background):
    example = background
    service = example.service()
    example.answer.value = Usage((), example.epoch, 'example sign-in unavailable', called=False)
    quota.refresh_declared(service)
    quota.refresh_declared(service)
    assert len(example.calls) == 1
    assert example.ledger.quota.last_attempt(example.pool.name) == example.epoch
    assert example.logs == [('usage', {'pool': example.pool.name, 'error': 'example sign-in unavailable', 'no_call': True})]
    example.clock.seconds = 1800
    quota.refresh_declared(service)
    assert len(example.calls) == 2
    assert len(example.logs) == 1
