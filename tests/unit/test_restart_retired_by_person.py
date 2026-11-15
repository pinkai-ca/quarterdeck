"""Own restart decisions on a copied ship and synthetic native records, with no live program."""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from quarterdeck import human_wake, person_quiet, post, request_queue, service as servicemod
from quarterdeck.harness.types import Screen
from tests.compaction_shapes import AT
from tests.unit.test_person_quiet import desk


@pytest.fixture
def own_restart(desk, monkeypatch):
    world = desk()
    world.now = AT + timedelta(seconds=10)
    world.screen = Screen(text='', box='', working=False)
    world.calls = []
    monkeypatch.setattr(person_quiet, 'waking_lines', lambda: (human_wake.LINE,))
    runner = servicemod.Service(world.seat.ship, world.seat.tmux)
    world.runner = runner
    monkeypatch.setattr(post, 'now', lambda: world.now)
    monkeypatch.setattr(runner, 'seat', lambda name: world.seat)
    monkeypatch.setattr(world.seat, 'restart', lambda: world.calls.append('restart') or 'synthetic restarted')
    monkeypatch.setattr(world.seat, 'fresh', lambda: world.calls.append('fresh') or 'synthetic fresh')

    class Door:
        def __init__(self, seat):
            assert seat is world.seat

        def screen(self):
            return world.screen

    monkeypatch.setattr(servicemod, 'Door', Door)
    try:
        yield world
    finally:
        runner.ledger.close()


def enqueue(world, resume=True):
    return request_queue.enqueue(world.seat.ship.runtime, 'relaunch', dict(
        seat=world.seat.name, caller=world.seat.name, resume=resume,
        origin=request_queue.launch_binding(world.seat)), name='synthetic-own-restart')


def finish(world):
    world.native.serial = max(world.native.serial, int((world.now - AT).total_seconds()))
    world.native.finish()
    world.now = AT + timedelta(seconds=world.native.serial + 20)


def later_input(world, text):
    world.native.serial = max(world.native.serial, int((world.now - AT).total_seconds()))
    world.native.begin(text)
    world.now = AT + timedelta(seconds=world.native.serial + 1)


@pytest.mark.parametrize('framework_input', ['wake', 'notice', 'delivery'])
@pytest.mark.parametrize('resume', [True, False])
def test_framework_input_does_not_retire_an_own_request(own_restart, framework_input, resume):
    world = own_restart
    path = enqueue(world, resume)
    world.runner.requests()
    assert path.exists() and not world.calls
    finish(world)
    if framework_input == 'wake':
        # Use the factory's actual fixed line; the ordinary reader excludes it whole.
        later_input(world, human_wake.LINE)
        world.runner.requests()
        assert path.exists() and not world.calls
        finish(world)
    elif framework_input == 'notice':
        world.native.serial = int((world.now - AT).total_seconds())
        world.native.add('user', message=dict(role='user', content='synthetic compacted context'),
                         isCompactSummary=True)
        world.now += timedelta(seconds=30)
    else:
        message = world.runner.store.new('answer', 'synthetic-peer', world.seat.name, 'synthetic delivery')
        attempt = world.runner.attempts.open(message, world.seat.name, None, None, None)
        world.runner.attempts.close(attempt, 'watch', 'synthetic watch receipt')
        later_input(world, post.typed([message]))
        world.runner.requests()
        assert path.exists() and not world.calls
        finish(world)
    world.runner.requests()
    assert not path.exists()
    assert path.with_suffix('.done').read_text() == ('synthetic restarted\n' if resume else 'synthetic fresh\n')
    assert world.calls == ['restart' if resume else 'fresh']


def test_a_framework_input_before_the_first_decision_does_not_retire_the_request(own_restart):
    world = own_restart
    path = enqueue(world)
    finish(world)
    later_input(world, human_wake.LINE)
    finish(world)
    world.runner.requests()
    assert not path.exists() and world.calls == ['restart']


def test_the_persons_later_input_retires_the_request(own_restart):
    world = own_restart
    path = enqueue(world)
    world.runner.requests()
    finish(world)
    later_input(world, 'Synthetic person continues the conversation')
    finish(world)
    world.runner.requests()
    assert not path.exists() and not world.calls
    assert "person's later input" in path.with_suffix('.done').read_text()


def test_busy_box_waits_without_retiring_after_framework_work(own_restart):
    world = own_restart
    path = enqueue(world)
    finish(world)
    later_input(world, human_wake.LINE)
    finish(world)
    world.screen = Screen(text='', box='synthetic draft', working=False)
    world.runner.requests()
    assert path.exists() and not world.calls
    assert request_queue.pending(world.seat.ship.runtime)[0]['dimension'] == 'box'
    world.screen = Screen(text='', box='', working=False)
    world.runner.requests()
    assert not path.exists() and world.calls == ['restart']


def test_pending_restart_is_decided_once_before_slow_duties_on_the_copy(own_restart, monkeypatch):
    world = own_restart
    path = enqueue(world)
    finish(world)
    runner = world.runner
    clock, events = SimpleNamespace(seconds=0.0), []
    duties = (
        'server_recovery', 'reconcile', 'file_drops', 'groups', 'cascades', 'fleet_post',
        'inboxes', 'views', 'recoveries', 'refusals', 'answers', 'looks', 'jailed_watches',
        'deliveries', 'follow', 'compactions', 'person_quiet', 'fresh_after_order',
        'rebrief', 'push_reminders', 'credentials', 'authentications', 'model_differences',
        'failover', 'account', 'refresh_usage', 'pool_forecasts', 'retire_post',
    )

    def duty(name):
        def run():
            events.append((name, clock.seconds))
            if name in ('deliveries', 'follow'):
                clock.seconds += 265.0  # the reported queue delay, deterministically represented
        return run

    for name in duties:
        monkeypatch.setattr(runner, name, duty(name))
    for name in ('drain_drops', 'drain_accounting', 'heartbeat', 'status_due'):
        monkeypatch.setattr(runner, name, lambda: None)
    original = runner.requests

    def requests():
        events.append(('requests', clock.seconds))
        original()
        assert not path.exists() and world.calls == ['restart']

    monkeypatch.setattr(runner, 'requests', requests)
    runner._tick_steps()
    assert events[0] == ('requests', 0.0)
    assert sum(name == 'requests' for name, _ in events) == 1
    assert [name for name, _ in events[1:]] == list(duties)
    assert clock.seconds == 530.0
