"""Ordinary SIGTERM during reads before an attempt is opened."""

import time

import pytest

from quarterdeck import servicectl, shutdown
from quarterdeck.post import Attempts
from . import test_service_shutdown as phases
from .test_service_shutdown import world  # shared private fake program and owned teardown


PREFLIGHT = r'''
from quarterdeck import post
original_reading = door.Door.reading
original_attempts = post.Attempts.all
preflight_used = False
def stall():
    global preflight_used
    if preflight_used:
        return
    preflight_used = True
    (runtime / 'phase-hit').touch()
    while not (runtime / 'phase-release').exists():
        time.sleep(.005)
def reading_before_attempt(self, *args, **kwargs):
    if phase == 'initial-reading' and sys._getframe(1).f_code.co_name == '_deliver_current':
        stall()
    return original_reading(self, *args, **kwargs)
def history_before_attempt(self, *args, **kwargs):
    if phase == 'attempt-history':
        frame = sys._getframe(1)
        while frame:
            if frame.f_code.co_name == '_deliver_current':
                stall()
                break
            frame = frame.f_back
    return original_attempts(self, *args, **kwargs)
door.Door.reading = reading_before_attempt
post.Attempts.all = history_before_attempt
'''


@pytest.mark.parametrize('phase', ['initial-reading', 'attempt-history'])
def test_sigterm_during_preflight_stops_within_ordinary_grace(world, monkeypatch, phase):
    monkeypatch.setattr(phases, 'DRIVER', phases.DRIVER.replace(
        'loaded = ship.load(folder)', PREFLIGHT + '\nloaded = ship.load(folder)'))
    world.start(phase)
    message = world.post()
    runtime = world.ship.runtime
    phases.wait_for(lambda: (runtime / 'phase-hit').exists())
    started = time.monotonic()
    result = servicectl.stop(runtime)
    assert time.monotonic() - started < shutdown.ORDINARY_GRACE
    assert 'service stopped' in result and 'killed' not in result
    assert not servicectl.locked(runtime)
    assert Attempts(runtime).for_message(message.id) == []
    assert world.journal() is None


POST_ATTEMPT = r'''
from quarterdeck import post
original_open = post.Attempts.open
after_open_used = False
def open_before_journal(self, *args, **kwargs):
    global after_open_used
    attempt = original_open(self, *args, **kwargs)
    if phase == 'after-attempt-open' and not after_open_used:
        after_open_used = True
        (runtime / 'phase-hit').touch()
        while not shutdown.requested():
            time.sleep(.005)
        (runtime / 'term-seen').touch()
        while not (runtime / 'phase-release').exists():
            time.sleep(.005)
    return attempt
post.Attempts.open = open_before_journal
'''


def test_stop_after_attempt_open_waits_for_the_write_unit(world, monkeypatch):
    monkeypatch.setattr(shutdown, 'ORDINARY_GRACE', 1.0)  # the ordinary bound low, so the delivery's shows past it
    monkeypatch.setattr(phases, 'DRIVER', phases.DRIVER.replace(
        'loaded = ship.load(folder)', POST_ATTEMPT + '\nloaded = ship.load(folder)'))
    world.start('after-attempt-open')
    message = world.post()
    runtime = world.ship.runtime
    phases.wait_for(lambda: (runtime / 'phase-hit').exists())
    active = shutdown.inflight(runtime, shutdown.owner(runtime))
    assert active['kind'] == 'attempt'
    assert world.journal() is None
    thread, results, errors = phases.stopping(lambda: servicectl.stop(runtime))
    try:
        phases.wait_for(lambda: (runtime / 'term-seen').exists())
        time.sleep(shutdown.ORDINARY_GRACE + .15)
        assert thread.is_alive(), 'the pre-journal write unit must receive delivery grace'
        assert servicectl.locked(runtime)
    finally:
        (runtime / 'phase-release').touch()
        thread.join(timeout=10)
    assert not thread.is_alive() and not errors
    assert 'service stopped' in results[0] and 'killed' not in results[0]
    assert 'delivery boundary' in results[0] and 'limit 30s' in results[0]
    journal = world.journal()
    assert journal['phase'] == 'cancelled'
    attempt, = Attempts(runtime).for_message(message.id)
    assert attempt.result is None, 'stop must not invent a transport result'
    assert journal['attempts'] == [attempt.path.name]
    assert not shutdown.pointer_path(runtime).exists()
