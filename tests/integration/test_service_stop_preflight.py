"""Ordinary SIGTERM during reads before an attempt is opened."""

from types import SimpleNamespace

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


def stop_with_clock(runtime, monkeypatch, *, monotonic, sleep):
    real_clock, real_holder = servicectl.time, servicectl.holder

    def holder(folder):
        # Let the lock-owner probe make its own two kernel reads before injecting the stop clock.
        with monkeypatch.context() as owner_clock:
            owner_clock.setattr(servicectl, 'time', real_clock)
            return real_holder(folder)

    with monkeypatch.context() as stop_clock:
        stop_clock.setattr(servicectl, 'holder', holder)
        stop_clock.setattr(servicectl, 'time', SimpleNamespace(monotonic=monotonic, sleep=sleep))
        return servicectl.stop(runtime)


def stop_during_preflight(runtime, monkeypatch):
    owner = shutdown.owner(runtime)
    assert owner is not None
    result = stop_with_clock(runtime, monkeypatch, monotonic=lambda: 0.0,
                             sleep=lambda _: phases.wait_for(lambda: shutdown.process_gone(owner)))
    assert 'service stopped' in result and 'killed' not in result
    assert 'delivery boundary' not in result
    assert not (runtime / 'phase-release').exists()
    assert shutdown.process_gone(owner) and not servicectl.locked(runtime)


def stop_at_write_unit(runtime, monkeypatch, *, past_ordinary=False):
    """Advance only the stop observer's clock while the driver holds the write unit."""
    owner = shutdown.owner(runtime)
    assert owner is not None
    release, term_seen = runtime / 'phase-release', runtime / 'term-seen'
    now, waited = 0.0, False

    def sleep(_):
        nonlocal now, waited
        if past_ordinary and now == 0:
            phases.wait_for(term_seen.exists)
            now = shutdown.ORDINARY_GRACE + .5
            return  # the next loop must still grant the delivery its own grace
        if not release.exists():
            phases.wait_for(term_seen.exists)
            assert servicectl.locked(runtime) and not shutdown.process_gone(owner)
            waited = True
            release.touch()
        phases.wait_for(lambda: shutdown.process_gone(owner))

    try:
        result = stop_with_clock(runtime, monkeypatch, monotonic=lambda: now, sleep=sleep)
    finally:
        release.touch()
    assert waited, 'stop did not wait for the write unit at the expected deadline'
    assert shutdown.process_gone(owner) and not servicectl.locked(runtime)
    return result


@pytest.mark.parametrize('phase', ['initial-reading', 'attempt-history'])
def test_sigterm_during_preflight_stops_within_ordinary_grace(world, monkeypatch, phase):
    monkeypatch.setattr(phases, 'DRIVER', phases.DRIVER.replace(
        'loaded = ship.load(folder)', PREFLIGHT + '\nloaded = ship.load(folder)'))
    world.start(phase)
    message = world.post()
    runtime = world.ship.runtime
    phases.wait_for(lambda: (runtime / 'phase-hit').exists())
    stop_during_preflight(runtime, monkeypatch)
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
    result = stop_at_write_unit(runtime, monkeypatch, past_ordinary=True)
    assert 'service stopped' in result and 'killed' not in result
    assert 'delivery boundary' in result and 'limit 30s' in result
    journal = world.journal()
    assert journal['phase'] == 'cancelled'
    attempt, = Attempts(runtime).for_message(message.id)
    assert attempt.result is None, 'stop must not invent a transport result'
    assert journal['attempts'] == [attempt.path.name]
    assert not shutdown.pointer_path(runtime).exists()
