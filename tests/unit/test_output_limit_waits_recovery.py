"""The output-limit alarm waits for the thinking recovery's decision on the same stop.

The recovery's own test world: a synthetic OpenCode hand on an endpoint pool, a capped turn on the real reader, the
real Door and journal, a stand-in program. The clock is post.now, injected; nothing sleeps.
"""
from dataclasses import replace
from datetime import timedelta

from quarterdeck import alarms, output_limit, post, recovery
from quarterdeck.ship import SeatSpec
from tests.unit.test_opencode_record import AT
from tests.unit.test_thinking_recovery import measured_hand_turn, world  # noqa: F401 -- world is a fixture


def told(world):
    return [m for m in post.Store(world.seat.ship.runtime).all() if m.to == 'systems-example' and m.kind == 'report']


def with_systems(world):
    """A systems seat to tell, and the service's road to it."""
    ship = world.service.ship
    world.service.ship = replace(ship, seats={**ship.seats, 'systems-example': SeatSpec(
        'systems-example', 'crew', 'systems', 'opencode', ('synthetic',), model='qwen3.8-27b')})
    world.service.systems_recipients = lambda seat: ['systems-example']
    return world


def at(monkeypatch, seconds):
    monkeypatch.setattr(post, 'now', lambda: AT + timedelta(seconds=seconds))


def test_a_capped_hand_turn_the_recovery_takes_raises_no_alarm(world, monkeypatch):
    with_systems(world)
    assert output_limit.reading(world.seat) is not None, 'the stop stands, an order open'
    output_limit.step(world.service, world.seat)  # before the recovery's pass: it would take it
    assert told(world) == []
    recovery.run(world.service)
    assert len(world.native.inputs) == 1, 'the recovery took it'
    for seconds in (2, 601, 3600):
        at(monkeypatch, seconds)
        output_limit.step(world.service, world.seat)
    assert told(world) == [], 'taken: no alarm, then or later'
    assert output_limit.reading(world.seat) is None, 'its continuation ran: the stop no longer stands'


def test_a_capped_hand_turn_the_recovery_declines_raises_exactly_one_alarm(world):
    with_systems(world)
    measured_hand_turn(world.record, capped_reasoning=8189, capped_written=3, tool=True)  # work: no candidate
    assert world.seat.harness.state(world.record).capped is None
    recovery.run(world.service)
    assert world.native.inputs == [], 'the recovery declined it'
    output_limit.step(world.service, world.seat)
    output_limit.step(world.service, world.seat)
    [alarm] = told(world)
    assert alarm.body.startswith('worker: its last turn stopped at its output limit with an order open')
    assert output_limit.line(world.seat) is not None, 'the status line names the seat as before'


def test_a_delivered_continuation_that_caps_again_raises_one_alarm_after_the_wait(world, monkeypatch):
    with_systems(world)
    world.native.failed_recovery = True  # the continuation itself stops at its limit
    recovery.run(world.service)
    recovery.run(world.service)
    assert len(world.native.inputs) == 1, 'one recovery for the turn, no further one'
    key = output_limit.reading(world.seat)
    assert key is not None and output_limit.line(world.seat) is not None, 'the stop stands again, and is named'
    output_limit.step(world.service, world.seat)
    at(monkeypatch, output_limit.WAIT_SECONDS - 1)
    output_limit.step(world.service, world.seat)
    assert told(world) == [], 'within the wait after its continuation: nothing told'
    assert alarms.load(world.seat.ship.runtime, 'worker', output_limit.CHANNEL)['key'] is None, 'nor kept'
    at(monkeypatch, output_limit.WAIT_SECONDS + 2)
    output_limit.step(world.service, world.seat)
    output_limit.step(world.service, world.seat)
    assert len(told(world)) == 1, 'past the wait, the stop still standing: told once'
