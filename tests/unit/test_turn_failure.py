"""A turn that ended in a recorded error is shown, alarmed, and what it took is not settled as received."""
from __future__ import annotations

import json

import pytest

from quarterdeck import pending, post, turn_failure, watch
from quarterdeck.door import Door
from tests.unit.refusal_ship import OTHER, REFUSED, World


@pytest.fixture
def world(tmp_path):
    built = World(tmp_path)
    yield built
    built.svc.ledger.close()


def test_a_refused_turn_reads_refused_with_its_first_words_in_watch(world):  # law: record-first
    seat = world.launch('worker')
    world.turn('worker', world.order('worker'), error=REFUSED)
    reading = watch.activity(seat)
    assert reading.state == 'refused' and reading.state != 'idle'
    assert 'provider refusal (cyber_policy): Synthetic refusal stand-in' in reading.detail


def test_any_other_error_reads_failed_and_never_idle(world):  # law: record-first
    seat = world.launch('worker')
    world.turn('worker', world.order('worker'), error=OTHER)
    reading = watch.activity(seat)
    assert reading.state == 'failed' and 'provider error (internal_server_error)' in reading.detail


@pytest.mark.parametrize('error,open_kind,held', [
    (REFUSED, None, True),
    (OTHER, 'order', True),
    (OTHER, 'question', True),
    (OTHER, None, False),
    (None, 'order', False),
], ids=['refusal-always', 'error-with-order', 'error-with-question', 'error-nothing-open', 'no-error'])
def test_the_door_holds_and_the_status_line_shows_it_while_an_obligation_is_open(world, error, open_kind, held):  # law: nothing-waits-silently
    seat = world.launch('worker')
    answered = world.order('worker', kind='answer')
    world.turn('worker', answered, error=error)
    if open_kind:
        world.order('worker', kind=open_kind)
    reason = turn_failure.obstruction(seat)
    assert bool(reason) is held
    reading = Door(seat)._reading(None)
    assert (reading.clears == 'needs a human' and reading.reason == reason) is held
    facts = watch.Facts('ok', 0, 0, 0, turn_errors=[f'worker: {reason}'] if reason else [])
    assert ('worker: provider' in facts.line()) is held


def test_a_held_door_types_nothing_and_withholds_before_any_key(world):  # law: one-door
    seat = world.launch('worker')
    world.turn('worker', world.order('worker'), error=REFUSED)
    delivery = Door(seat).deliver(post.typed([world.order('worker', 'A later invented message.')]))
    assert delivery.result == 'withheld' and 'provider refusal' in delivery.detail


def test_the_alarm_goes_to_the_systems_seat_and_for_a_human_seat_to_the_desk_too(world):  # law: nothing-waits-silently
    world.launch('spock')
    seat = world.launch('worker')
    world.turn('worker', world.order('worker'), error=REFUSED)
    reason = turn_failure.obstruction(seat)
    assert world.svc.alarm_recipients(seat, reason) == ['spock']
    aide = world.launch('aide')
    assert world.svc.alarm_recipients(aide, 'provider refusal: synthetic') == ['spock', 'desk']


@pytest.mark.parametrize('output,last,expected', [
    (False, None, 'unacted'),
    (True, None, 'delivered'),
    (False, 'absent', 'uncertain'),
], ids=['proved-no-output', 'partial-output', 'unknown-output'])
def test_a_message_typed_into_an_errored_turn_is_not_settled_as_received(world, output, last, expected):  # law: one-door
    seat = world.launch('worker')
    order = world.order('worker')
    world.event('worker', {'type': 'task_started'})
    world.typed('worker', order)
    if output:
        world.event('worker', {'type': 'token_count', 'info': {'last_token_usage': {'output_tokens': 3}}})
    payload = {'type': 'task_complete', 'error': OTHER}
    if last != 'absent':
        payload['last_agent_message'] = last
    world.event('worker', payload)
    world.svc.reconcile()
    latest = world.svc.attempts.latest()[order.id]
    assert latest == expected
    ticked = {m.id for m in world.svc.attempts.undelivered(world.svc.store, 'worker')}
    launched = {m.id for m in world.svc.attempts.undelivered(world.svc.store, 'worker', for_launch=True)}
    assert order.id not in ticked, 'no tick types it again'
    assert (order.id in launched) is (expected == 'unacted'), 'only a proved unacted message is carried at a start'
    if expected != 'delivered':
        attempt = world.svc.attempts.for_message(order.id)[-1]
        assert world.svc.attempts.close(attempt, 'delivered', 'a late receipt').result == expected, 'it stays unsettled'
        assert watch.Facts('ok', 0, 0, 0, unsettled=1).line().count('1 unsettled after an error') == 1
        world.svc.reconcile()
        assert world.svc.attempts.latest()[order.id] == expected


def test_a_journaled_delivery_is_unsettled_with_its_journal(world):  # law: one-door
    seat = world.launch('worker')
    order = world.order('worker')
    launched = seat.launched()
    attempt = world.svc.attempts.open(order, 'worker', launched.pane, launched.pid, launched.at)
    proof = pending.Proof.begin(seat, attempt, post.typed([order]))
    proof.save(phase='submitted', shown=proof.data['payload'])
    world.event('worker', {'type': 'task_started'})
    world.human_input('worker', post.typed([order]))
    world.event('worker', {'type': 'task_complete', 'error': OTHER, 'last_agent_message': None})
    world.svc.reconcile()
    assert json.loads(proof.path.read_text())['phase'] == 'unacted'
    assert world.svc.attempts.latest()[order.id] == 'unacted'
    world.svc.reconcile()  # the journal now stands for the unsettled result; nothing flips it back
    assert world.svc.attempts.latest()[order.id] == 'unacted'


def test_a_later_normal_turn_ends_the_hold(world):
    seat = world.launch('worker')
    world.turn('worker', world.order('worker'), error=OTHER)
    world.order('worker')
    assert turn_failure.obstruction(seat)
    world.turn('worker', world.order('worker', kind='answer'), last='done')
    assert turn_failure.obstruction(seat) is None and watch.activity(seat).state == 'idle'


def test_a_human_seat_is_read_from_operational_metadata_only(world, monkeypatch):  # law: record-first
    seat = world.launch('aide')
    world.turn('aide', world.order('aide', 'Invented private subject matter.'), error=REFUSED)
    record = seat.record()
    state, turns = seat.harness.read(record)
    turn_failure.publish(seat, record, state, turns)
    text = (seat.folder / turn_failure.FILE).read_text()
    assert 'Invented private subject matter' not in text and 'inputs' not in text
    monkeypatch.setattr(type(seat.harness), 'state', lambda *a, **k: pytest.fail('a human record is never read'))
    cached = turn_failure.current(seat)
    assert cached.kind == 'provider refusal' and cached.code == 'cyber_policy'
    assert 'the desk starts it fresh' in turn_failure.obstruction(seat)
    with record.open('a') as stream:
        stream.write('{}\n')
    assert 'outcome unknown' in turn_failure.current(seat).message, 'a newer record is never read as the old refusal'
