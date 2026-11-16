"""A declared Claude pool is read even when no seat sits on it.

The Claude usage reader reads a pool with the pool's own sign-in, so the service reads a declared pool on its tick,
paced at the reader's interval, never on a reading's demand. A refusal holds it by its Retry-After; a failed call with
none waits twice as long each time. A throwaway pool and a stand-in reader, on an injected clock: no token is read.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from quarterdeck import harness as harnesses, quota
from quarterdeck.harness.claude import ClaudeHarness
from quarterdeck.harness.limits import Usage
from quarterdeck.service import Service
from quarterdeck.ship import Pool, Session, Ship

AT = datetime(2031, 12, 31, 12, 0, tzinfo=timezone.utc)


class Clock:
    """Monotonic seconds and the wall clock, moved by the test alone."""

    def __init__(self):
        self.seconds = 1000.0

    def monotonic(self):
        return self.seconds

    def now(self, tz=None):
        return AT + timedelta(seconds=self.seconds)


@pytest.fixture
def world(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(quota, 'time', SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(quota, 'datetime', SimpleNamespace(now=clock.now, fromisoformat=datetime.fromisoformat))
    ship = Ship(tmp_path, 'synthetic', 'operator', tmp_path, {'idle-example': Pool('idle-example', 'anthropic')},
                {'crew': Session('crew', 'crew')}, {})
    svc = Service(ship, SimpleNamespace())
    logs = []
    monkeypatch.setattr(svc, 'log', lambda event, **data: logs.append((event, data)))
    answers, asked = [], []

    def usage(pool):
        asked.append(pool.name)
        return answers.pop(0) if answers else Usage((), clock.now(), None)
    reader = SimpleNamespace(name='stand-in', background_usage=False, idle_usage=True, reads_usage=True,
                             usage_interval=300, private_usage_home=False, usage=usage)
    monkeypatch.setattr(harnesses, 'native_for', lambda provider: reader)
    try:
        yield SimpleNamespace(svc=svc, clock=clock, answers=answers, asked=asked, logs=logs, reader=reader)
    finally:
        svc.ledger.close()


def test_claudes_reader_reads_a_pool_with_no_seat():
    assert ClaudeHarness.idle_usage and not ClaudeHarness.background_usage and ClaudeHarness.usage_interval == 300


def test_an_idle_pool_is_read_on_the_tick_and_the_pacing_holds(world):
    assert world.svc.ship.seats == {}, 'no seat sits on the pool'
    quota.refresh_declared(world.svc)
    assert world.asked == ['idle-example']
    assert world.svc.ledger.quota.last_attempt('idle-example') == world.clock.now()
    world.clock.seconds += 299
    quota.refresh_declared(world.svc)
    assert world.asked == ['idle-example'], 'within the interval: not read again'
    world.clock.seconds += 1
    quota.refresh_declared(world.svc)
    assert world.asked == ['idle-example', 'idle-example'], 'at the interval: read again'


def test_a_live_seats_read_this_pass_makes_the_idle_read_wait(world):
    world.svc.usage_read['idle-example'] = world.clock.monotonic()  # a seat on the pool was read a moment ago
    quota.refresh_declared(world.svc)
    assert world.asked == [], 'one read per interval, whichever path made it'


def test_after_a_restart_the_last_attempt_in_the_book_paces_the_first_read(world):
    quota.refresh_declared(world.svc)
    world.svc.usage_read.clear()  # a restarted service: only the book remembers
    world.clock.seconds += 100
    quota.refresh_declared(world.svc)
    assert world.asked == ['idle-example'], 'seeded from the book: not read again'
    world.clock.seconds += 200
    quota.refresh_declared(world.svc)
    assert world.asked == ['idle-example', 'idle-example']


def test_a_refusal_holds_the_pool_by_its_retry_after_and_is_shown(world):
    world.answers.append(Usage((), world.clock.now(), 'HTTP 429, retry-after 900', retry_after=900))
    quota.refresh_declared(world.svc)
    assert ('usage', dict(pool='idle-example', error='HTTP 429, retry-after 900', backoff=900)) in world.logs
    assert world.svc.ledger.db.execute("SELECT error FROM usage_reads WHERE pool='idle-example'").fetchone() == (
        'HTTP 429, retry-after 900',), 'the refusal is kept, and shown as the latest read'
    world.clock.seconds += 899
    quota.refresh_declared(world.svc)
    assert len(world.asked) == 1, 'held until its Retry-After'
    world.clock.seconds += 1
    quota.refresh_declared(world.svc)
    assert len(world.asked) == 2


def test_a_failed_call_backs_off_twice_as_long_each_time_and_a_good_read_ends_it(world):
    world.answers += [Usage((), world.clock.now(), 'timeout'), Usage((), world.clock.now(), 'network error: OSError')]
    quota.refresh_declared(world.svc)
    assert world.logs[-1] == ('usage', dict(pool='idle-example', error='timeout', backoff=600))
    world.clock.seconds += 599
    quota.refresh_declared(world.svc)
    assert len(world.asked) == 1, 'backing off: 600 s after one failure'
    world.clock.seconds += 1
    quota.refresh_declared(world.svc)
    assert world.logs[-1] == ('usage', dict(pool='idle-example', error='network error: OSError', backoff=1200))
    world.clock.seconds += 1200
    quota.refresh_declared(world.svc)
    assert len(world.asked) == 3 and world.svc.usage_idle_failures == {}, 'a good read ends the backoff'
    world.clock.seconds += 300
    quota.refresh_declared(world.svc)
    assert len(world.asked) == 4, 'back to the ordinary interval'


def test_a_wait_never_passes_the_hour(world):
    world.answers += [Usage((), world.clock.now(), 'timeout') for _ in range(6)]
    waits = []
    for _ in range(6):
        world.clock.seconds += 3600
        quota.refresh_declared(world.svc)
        waits.append(world.logs[-1][1]['backoff'])
    assert waits == [600, 1200, 2400, 3600, 3600, 3600]


def test_a_state_that_made_no_call_waits_the_ordinary_interval(world):
    world.answers.append(Usage((), world.clock.now(), 'credential file missing', called=False))
    quota.refresh_declared(world.svc)
    assert 'idle-example' not in world.svc.usage_hold, 'no call to back off from'
    world.clock.seconds += 300
    quota.refresh_declared(world.svc)
    assert len(world.asked) == 2


def test_a_reader_that_needs_a_seat_is_not_read_idle(world):
    world.reader.idle_usage = False
    quota.refresh_declared(world.svc)
    assert world.asked == []
