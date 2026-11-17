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


def test_a_claim_whose_delivery_was_interrupted_raises_one_alarm_after_the_wait(world, monkeypatch):
    """Its submit refused before the program took it, the claim stays at 'off' before 'delivered',
    and no later pass of the recovery advances it."""
    from quarterdeck import pending
    with_systems(world)
    world.native.fault = 'submit-before'
    recovery.run(world.service)
    world.native.fault = None
    recovery.run(world.service)
    [proof] = pending.journals(world.seat)[0]
    assert proof.data['recovery']['mode'] == 'off' and proof.data['phase'] not in ('delivered', 'prepared')
    assert output_limit._recovery(world.seat, output_limit._stop(world.seat, world.service.store)[1])[0] == 'claiming'
    output_limit.step(world.service, world.seat)  # the pass that first sees the stop
    at(monkeypatch, output_limit.WAIT_SECONDS - 1)
    output_limit.step(world.service, world.seat)
    assert told(world) == [], 'within the wait: the claim may still finish'
    at(monkeypatch, output_limit.WAIT_SECONDS + 2)
    output_limit.step(world.service, world.seat)
    output_limit.step(world.service, world.seat)
    assert len(told(world)) == 1, 'past the wait, the claim unfinished and the stop standing: told once'


def verdicts(monkeypatch, *steps):
    """The recovery's decision over time, injected: (from seconds, verdict, its paste's moment in seconds or None)."""
    def decided(seat, state):
        now = (post.now() - AT).total_seconds()
        _, verdict, since = [step for step in steps if step[0] <= now][-1]
        return verdict, (AT + timedelta(seconds=since)) if since is not None else None
    monkeypatch.setattr(output_limit, '_recovery', decided)


def test_a_claim_delivered_within_the_wait_raises_nothing_at_its_end(world, monkeypatch):
    with_systems(world)
    verdicts(monkeypatch, (0, 'claiming', None), (120, 'delivered', 110))
    for seconds in (1, 120, output_limit.WAIT_SECONDS + 2, 110 + output_limit.WAIT_SECONDS - 1):
        at(monkeypatch, seconds)
        output_limit.step(world.service, world.seat)
    assert told(world) == [], "delivered within the claim's wait: its own wait from the paste applies"


def test_a_claim_held_within_the_wait_raises_one_alarm_then_not_again(world, monkeypatch):
    with_systems(world)
    verdicts(monkeypatch, (0, 'claiming', None), (120, 'held', None))
    at(monkeypatch, 1)
    output_limit.step(world.service, world.seat)
    assert told(world) == []
    for seconds in (120, output_limit.WAIT_SECONDS + 2, 2 * output_limit.WAIT_SECONDS):
        at(monkeypatch, seconds)
        output_limit.step(world.service, world.seat)
    assert len(told(world)) == 1, 'held: told once, when it is held, and not again at the end of the wait'
