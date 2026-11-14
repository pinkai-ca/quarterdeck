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
