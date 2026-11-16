"""A hand holds one order at a time at the door, too (hand_door): a further order or question waits, unread and named
on the status line, until the order in hand is reported; then the hand's next fresh start carries it. What belongs to
the order in hand still arrives; every other role is unchanged. Synthetic messages only."""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import json

import pytest

from quarterdeck import pending, post, watch
from .test_pending_delivery import input_row, setup  # noqa: F401


class _Door:
    """hand_door, imported where it is used, so that on code without it a test fails on the behaviour first."""
    def __getattr__(self, name):
        from quarterdeck import hand_door as module
        return getattr(module, name)


hand_door = _Door()


def typed(svc, message) -> list:
    return [attempt for attempt in svc.attempts.all() if attempt.message == message.id]


def as_role(svc, seat, role):
    """The fixture's one seat, given ``role``: the ship file and the seat agree."""
    seat.spec = replace(seat.spec, role=role)
    svc.ship.seats['worker'] = seat.spec
    return seat


@pytest.fixture
def hand(setup):  # noqa: F811
    svc, seat, live, record = setup
    return svc, as_role(svc, seat, 'hand'), live, record


def taken(svc, seat, record, live):
    """The hand's program takes the typed message: its box empties and its record shows the input. The door opens."""
    live['box'] = ''
    proof = pending.active(seat)[-1]
    input_row(record, proof.data['payload'].strip(), post.now())
    svc.reconcile()
    assert not pending.active(seat)


def report(svc, seat, order):
    return svc.store.new('report', seat.name, order.sender, 'Done, synthetically.', re=order.id)


def pending_lines(svc, seat):
    store, attempts = svc.store, svc.attempts
    step = watch._run_step('pending', seat, {}, store, attempts, attempts.latest(seat.name))
    return [text for target, text, _ in step.ops if target == 'pending']


def relaunch(seat, seconds):
    launch = json.loads((seat.trusted / 'launch.json').read_text())
    launch['at'] = (post.now() + timedelta(seconds=seconds)).isoformat()
    (seat.trusted / 'launch.json').write_text(json.dumps(launch))


def test_a_second_order_waits_while_the_first_is_open_and_the_status_line_names_it(hand):
    svc, seat, live, record = hand
    first = svc.store.new('order', 'desk', seat.name, 'An invented first order.')
    svc.deliver(seat, [first])
    taken(svc, seat, record, live)
    assert typed(svc, first) and live['pasted'] == 1
    second = svc.store.new('order', 'desk', seat.name, 'An invented second order.')
    question = svc.store.new('question', 'peer', seat.name, 'An invented question from another seat.')
    for message in (second, question):
        line = svc.deliver(seat, [message])
        assert f'waits: its order in hand {first.short} is open' in line
        assert not typed(svc, message), 'never typed, no attempt opened'
    svc.deliveries()  # the service's own pass holds them the same
    assert live['pasted'] == 1
    assert [m.id for m in svc.attempts.undelivered(svc.store, seat.name)] == [second.id, question.id], 'still unread'
    lines = pending_lines(svc, seat)
    assert lines == [f'{seat.name}: {second.short} waits: its order in hand {first.short} is open',
                     f'{seat.name}: {question.short} waits: its order in hand {first.short} is open']
    assert hand_door.blocks_fresh(seat, svc.store, svc.attempts), 'the order in hand is still being worked'


def test_a_held_order_comes_at_the_fresh_start_after_the_first_is_reported(hand):
    svc, seat, live, record = hand
    first = svc.store.new('order', 'desk', seat.name, 'An invented first order.')
    svc.deliver(seat, [first])
    taken(svc, seat, record, live)
    second = svc.store.new('order', 'desk', seat.name, 'An invented second order.')
    assert 'waits:' in svc.deliver(seat, [second])
    report(svc, seat, first)
    # Now in hand, the next order is not typed into the conversation that worked the first: its fresh start brings it.
    assert hand_door.order_in_hand(svc.store, seat.name).id == second.id
    line = svc.deliver(seat, [second])
    assert f'waits: it comes at the fresh start after {first.short}' in line and not typed(svc, second)
    assert pending_lines(svc, seat) == [f'{seat.name}: {second.short} waits: it comes at the fresh start after {first.short}']
    assert not hand_door.blocks_fresh(seat, svc.store, svc.attempts), 'its open order no longer holds the fresh start'
    carriage = seat.carriage()
    assert carriage.in_hand and carriage.text == post.typed([second]), 'the fresh start carries the next order whole'
    relaunch(seat, 1)  # the fresh launch, which was given only that order
    assert hand_door.held(seat, svc.store, svc.attempts, second) is None
    line = svc.deliver(seat, [second])
    assert typed(svc, second), line


def test_what_belongs_to_the_order_in_hand_still_arrives(hand):
    svc, seat, live, record = hand
    first = svc.store.new('order', 'desk', seat.name, 'An invented first order.')
    svc.deliver(seat, [first])
    taken(svc, seat, record, live)
    later = svc.store.new('order', 'desk', seat.name, 'An invented unrelated order.')
    assert 'waits:' in svc.deliver(seat, [later])
    correction = svc.store.new('order', 'desk', seat.name, 'An invented correction to the first order.', re=first.id)
    question = svc.store.new('question', seat.name, 'desk', 'Which invented option?', re=first.id)
    answer = svc.store.new('answer', 'desk', seat.name, 'The second invented option.', re=question.id)
    for message in (correction, answer):
        assert hand_door.held(seat, svc.store, svc.attempts, message) is None
        line = svc.deliver(seat, [message])
        assert typed(svc, message), line
        taken(svc, seat, record, live)
    assert pending_lines(svc, seat) == [f'{seat.name}: {later.short} waits: its order in hand {first.short} is open']


def test_a_message_that_asks_nothing_and_belongs_to_no_order_goes_as_before(hand):
    svc, seat, live, record = hand
    first = svc.store.new('order', 'desk', seat.name, 'An invented first order.')
    svc.deliver(seat, [first])
    taken(svc, seat, record, live)
    notice = svc.store.new('report', 'peer', seat.name, 'An invented report that asks nothing.')
    assert hand_door.held(seat, svc.store, svc.attempts, notice) is None


def test_a_hand_with_nothing_given_takes_its_order_in_hand_at_the_door(hand):
    svc, seat, live, _ = hand
    first = svc.store.new('order', 'desk', seat.name, 'An invented order to an idle hand.')
    assert hand_door.held(seat, svc.store, svc.attempts, first) is None
    svc.deliver(seat, [first])
    assert typed(svc, first)


@pytest.mark.parametrize('role', ['worker', 'engineer', 'desk'])
def test_every_other_role_is_unchanged(setup, role):  # noqa: F811
    svc, seat, live, _ = setup
    seat = as_role(svc, seat, role)
    first = svc.store.new('order', 'issuer', seat.name, 'An invented first order.')
    second = svc.store.new('order', 'issuer', seat.name, 'An invented second order.')
    assert hand_door.held(seat, svc.store, svc.attempts, second) is None
    assert 'waits: its order in hand' not in svc.deliver(seat, [first, second])
    assert hand_door.blocks_fresh(seat, svc.store, svc.attempts), 'open orders hold its fresh start, as before'
    assert pending_lines(svc, seat) == []
