"""A stop at each point of one delivery, then a new service: typed once, every attempt journalled.

The fake program runs on the fixture's own private tmux server, which the
fixture names and closes.
"""

import json
import os
import signal

import pytest

from quarterdeck import servicectl, shutdown
from quarterdeck.harness.synthetic_control import events
from quarterdeck.post import Attempts
from quarterdeck.ship import ShipError
from . import test_service_shutdown as phases
from .test_service_shutdown import MARKER, wait_for, world  # shared private fake program and owned teardown
from .test_service_stop_preflight import PREFLIGHT, stop_at_write_unit, stop_during_preflight


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
    stop_during_preflight(runtime, monkeypatch)
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
    result = stop_at_write_unit(runtime, monkeypatch)
    attempts = every_attempt_journalled(world, message)
    assert 'service stopped' in result and 'killed' not in result
    assert len(attempts) == 1
    journal = world.journal()
    if phase == 'opened':
        assert journal['phase'] == 'cancelled' and typed(world) == 0
    else:
        assert journal['phase'] == 'submitted' and typed(world) <= 1
    restart_delivers_once(world, message)


def stalled_after_open(world, monkeypatch):
    monkeypatch.setattr(phases, 'DRIVER', phases.DRIVER.replace(
        'loaded = ship.load(folder)', 'from quarterdeck import post\n' + OPENED + '\nloaded = ship.load(folder)'))
    world.start('opened')
    message = world.post()
    runtime = world.ship.runtime
    wait_for(lambda: (runtime / 'phase-hit').exists())
    attempt, = Attempts(runtime).for_message(message.id)
    assert world.journal() is None
    assert shutdown.inflight(runtime, shutdown.owner(runtime))['kind'] == 'attempt'
    return message, attempt


def test_stop_after_open_stalled_past_ordinary_grace_waits_then_restart_types_once(world, monkeypatch):
    monkeypatch.setattr(shutdown, 'ORDINARY_GRACE', 1.0)  # the ordinary bound low, so the delivery's shows past it
    message, _ = stalled_after_open(world, monkeypatch)
    runtime = world.ship.runtime
    result = stop_at_write_unit(runtime, monkeypatch, past_ordinary=True)
    assert 'service stopped' in result and 'killed' not in result
    assert 'limit 30s' in result
    assert not shutdown.pointer_path(runtime).exists()
    attempt, = every_attempt_journalled(world, message)
    assert world.journal()['phase'] == 'cancelled' and typed(world) == 0
    restart_delivers_once(world, message)


def latest(runtime, message):
    # The test polls while the service rewrites the attempt; a read refused mid-rewrite is not yet an answer.
    try:
        return Attempts(runtime).latest().get(message.id)
    except ShipError:
        return None


def kill_the_service(runtime):
    pid = servicectl.read_heartbeat(runtime)[0]
    os.kill(pid, signal.SIGKILL)
    wait_for(lambda: not servicectl.locked(runtime))
    return 'process death'


@pytest.mark.parametrize('cut', ['killed-past-grace', 'process-death'])
def test_forced_exit_before_the_journal_keeps_the_attempt_and_never_types_twice(world, monkeypatch, cut):
    message, attempt = stalled_after_open(world, monkeypatch)
    runtime = world.ship.runtime
    kept = attempt.path.read_bytes()
    dead = shutdown.owner(runtime)
    assert dead is not None
    if cut == 'killed-past-grace':
        monkeypatch.setattr(shutdown, 'ORDINARY_GRACE', .1)
        monkeypatch.setattr(shutdown, 'DELIVERY_GRACE', .3)
        result = servicectl.stop(runtime)  # the stalled write unit never reaches its release
        assert 'killed' in result and 'attempt retained' in result
        assert not shutdown.pointer_path(runtime).exists()
    else:
        kill_the_service(runtime)
        assert shutdown.pointer_path(runtime).exists()  # the dead owner's early pointer, for the next start
    assert attempt.path.read_bytes() == kept
    assert world.journal() is None and typed(world) == 0
    world.start()
    wait_for(lambda: servicectl.started(runtime))
    # started() holds once the new service has beaten, while the dead owner's record is still in the lock and its
    # pointer not yet finished; the new owner is written only after the next start has dealt with that pointer.
    wait_for(lambda: shutdown.owner(runtime) not in (None, dead))
    # The next start clears the old owner's early pointer; any pointer now is the new owner's own.
    current = shutdown.inflight(runtime, shutdown.owner(runtime))
    assert not shutdown.pointer_path(runtime).exists() or current and current['journal'] != f'post/attempts/{attempt.path.name}'
    followup = world.post('synthetic-followup-zzzz')
    wait_for(lambda: latest(runtime, followup) == 'delivered', 60)
    # The early pointer granted grace only: the attempt is reconciled by its receipt, never resent.
    wait_for(lambda: latest(runtime, message) is not None, 60)
    after, = Attempts(runtime).for_message(message.id)
    assert after.path == attempt.path, 'the next service opened a second attempt'
    before, now = json.loads(kept), json.loads(after.path.read_text())
    assert {k: v for k, v in now.items() if k not in ('result', 'detail')} == \
        {k: v for k, v in before.items() if k not in ('result', 'detail')}
    assert after.result == 'blind' and 'resend' in after.detail
    journals = (runtime / 'post/pending' / world.seat.name).glob('*.json')
    assert not any(attempt.path.name in json.loads(j.read_text())['attempts'] for j in journals)
    assert typed(world) == 0
