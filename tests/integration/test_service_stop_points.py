"""A stop at each point of one delivery, then a new service: typed once, every attempt journalled.

The fake program runs on the fixture's own private tmux server, which the
fixture names and closes.
"""

import json
import time

import pytest

from quarterdeck import servicectl, shutdown
from quarterdeck.harness.synthetic_control import events
from quarterdeck.post import Attempts
from . import test_service_shutdown as phases
from .test_service_shutdown import MARKER, stopping, wait_for, world  # shared private fake program and owned teardown
from .test_service_stop_preflight import PREFLIGHT


OPENED = r'''
original_open = post.Attempts.open
opened_used = False
def opened(self, message, *args, **kwargs):
    global opened_used
    attempt = original_open(self, message, *args, **kwargs)
    if phase == 'opened' and not opened_used and 'synthetic-stop-payload-zzzz' in message.body \
            and sys._getframe(1).f_code.co_name == '_deliver_current':
        opened_used = True
        (runtime / 'phase-hit').write_text(phase)
        while not shutdown.requested():
            time.sleep(.005)
        (runtime / 'term-seen').touch()
        while not (runtime / 'phase-release').exists():
            time.sleep(.005)
    return attempt
post.Attempts.open = opened
'''


def every_attempt_journalled(world, message):
    runtime, seat = world.ship.runtime, world.seat.name
    attempts = Attempts(runtime).for_message(message.id)
    for attempt in attempts:
        journal = runtime / 'post/pending' / seat / attempt.path.name
        assert journal.is_file(), f'attempt {attempt.path.name} has no journal'
        assert attempt.path.name in json.loads(journal.read_text())['attempts']
    return attempts


def typed(world):
    rows = [row['text'] for row in events(world.seat.home) if row['event'] == 'submitted' and MARKER in row['text']]
    assert all(text.count(MARKER) == 1 for text in rows), 'one submission carried the message twice'
    return len(rows)


def restart_delivers_once(world, message):
    world.start()
    world.progress(message)  # the message and a later one delivered; one submission of the message
    assert typed(world) == 1
    attempts = every_attempt_journalled(world, message)
    assert sum(a.result == 'delivered' for a in attempts) == 1


@pytest.mark.parametrize('phase', ['initial-reading', 'attempt-history'])
def test_stop_before_the_attempt_leaves_none_and_restart_types_once(world, monkeypatch, phase):
    monkeypatch.setattr(phases, 'DRIVER', phases.DRIVER.replace(
        'loaded = ship.load(folder)', PREFLIGHT + '\nloaded = ship.load(folder)'))
    world.start(phase)
    message = world.post()
    runtime = world.ship.runtime
    wait_for(lambda: (runtime / 'phase-hit').exists())
    started = time.monotonic()
    result = servicectl.stop(runtime)
    assert time.monotonic() - started < shutdown.ORDINARY_GRACE
    assert 'service stopped' in result and 'killed' not in result
    assert Attempts(runtime).for_message(message.id) == []
    assert world.journal() is None and not shutdown.pointer_path(runtime).exists()
    assert typed(world) == 0
    restart_delivers_once(world, message)


@pytest.mark.parametrize('phase', ['opened', 'pasted'])
def test_stop_inside_the_write_unit_waits_then_restart_types_once(world, monkeypatch, phase):
    monkeypatch.setattr(phases, 'DRIVER', phases.DRIVER.replace(
        'loaded = ship.load(folder)', 'from quarterdeck import post\n' + OPENED + '\nloaded = ship.load(folder)'))
    world.start(phase)
    message = world.post()
    runtime = world.ship.runtime
    wait_for(lambda: (runtime / 'phase-hit').exists())
    assert len(Attempts(runtime).for_message(message.id)) == 1
    thread, results, errors = stopping(lambda: servicectl.stop(runtime))
    # A service that exits on the signal instead of deferring shows as gone, not as a timeout.
    wait_for(lambda: (runtime / 'term-seen').exists() or not thread.is_alive())
    waited = thread.is_alive()
    (runtime / 'phase-release').touch()
    thread.join(timeout=10)
    assert not thread.is_alive() and not errors
    assert not servicectl.locked(runtime)
    attempts = every_attempt_journalled(world, message)
    assert waited, 'the stop did not wait for the write unit'
    assert 'killed' not in results[0]
    assert len(attempts) == 1
    journal = world.journal()
    if phase == 'opened':
        assert journal['phase'] == 'cancelled' and typed(world) == 0
    else:
        assert journal['phase'] == 'submitted' and typed(world) <= 1
    restart_delivers_once(world, message)
