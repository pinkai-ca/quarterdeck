"""A sign-in turn failure while the pool's sign-in reads valid: the next delivery first, then the guarded resume,
then attention, and every hold names its check. The failed turn's own input stays in the conversation.

Records are the measured Claude Code API-error envelope (tests/authentication_shapes.py) read by the real reader;
the post is a real store. The pool's sign-in, the seat's launch and the service's relaunch are stood in, so no
program starts and no credential is read. Text, paths and identities are invented.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from quarterdeck import auth_failure, post, turn_failure
from quarterdeck.harness import claude
from tests.authentication_shapes import error_row

SEAT = 'synthetic-worker'
PEER = 'synthetic-engineer'


def stamp(at):
    return at.isoformat()


def failed(text, at):
    """One turn as the record writes a sign-in failure: the input, the API-error row, the turn's end."""
    error = error_row()
    error['timestamp'] = stamp(at + timedelta(seconds=1))
    return [dict(type='user', timestamp=stamp(at), message=dict(role='user', content=text)), error,
            dict(type='system', subtype='turn_duration', timestamp=stamp(at + timedelta(seconds=2)))]


def answered(text, at):
    return [dict(type='user', timestamp=stamp(at), message=dict(role='user', content=text)),
            dict(type='assistant', timestamp=stamp(at + timedelta(seconds=1)),
                 message=dict(role='assistant', id='synthetic-answer', model='synthetic-model', stop_reason='end_turn',
                              content=[dict(type='text', text='Synthetic answer.')])),
            dict(type='system', subtype='turn_duration', timestamp=stamp(at + timedelta(seconds=2)))]


@pytest.fixture
def world(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    runtime = tmp_path / 'runtime'
    (runtime / 'post').mkdir(parents=True)
    grant = tmp_path / 'synthetic-grant'
    grant.write_text('synthetic local metadata stand-in')
    pool = SimpleNamespace(name='synthetic-pool')
    launched = SimpleNamespace(pool=pool.name, at=(now - timedelta(hours=1)).isoformat())
    record = tmp_path / 'synthetic-record.jsonl'
    rows = []
    seat = SimpleNamespace(name=SEAT, ship=SimpleNamespace(runtime=runtime, pools={pool.name: pool}),
                           session=SimpleNamespace(human=False), launched=Mock(return_value=launched), is_up=True,
                           record=Mock(return_value=record), harness=SimpleNamespace(state=claude.state, name='claude'))
    monkeypatch.setattr(auth_failure.credential, 'pool_file', Mock(return_value=grant))
    validity = SimpleNamespace(state='valid')
    monkeypatch.setattr(auth_failure.credential, 'sign_in_status', lambda *_: validity.state)
    store, attempts = post.Store(runtime), post.Attempts(runtime)
    service = SimpleNamespace(resolved_seats=lambda: [seat], relaunch=Mock(return_value='synthetic resumed proof'),
                              log=Mock(), needs_human=Mock(), store=store, attempts=attempts, request_wait=None)

    def write(*turns):
        for turn in turns:
            rows.extend(turn)
        record.write_text(''.join(json.dumps(row) + '\n' for row in rows))

    def order(body):
        message = store.prepare('order', PEER, SEAT, body)
        store.write(message)
        return message

    return SimpleNamespace(now=now, seat=seat, service=service, store=store, attempts=attempts, write=write,
                           order=order, validity=validity, at=lambda seconds: now - timedelta(minutes=5) + timedelta(seconds=seconds))


def test_a_first_failure_with_a_valid_sign_in_lets_the_next_delivery_run(world):
    world.order('Synthetic open order the seat is working on.')
    world.write(answered('Synthetic earlier task.', world.at(0)), failed('Synthetic task.', world.at(10)))
    assert auth_failure.failed_in_a_row(claude.state(world.seat.record())) == 1
    assert turn_failure.obstruction(world.seat, world.store) is None, 'the door delivers the next message'
    auth_failure.run(world.service)
    world.service.relaunch.assert_not_called()
    world.service.needs_human.assert_not_called()
    reading = auth_failure.status(world.seat)
    assert reading.endswith(auth_failure.DELIVERING) and auth_failure.waiting(reading), 'no alarm: it is not stuck'


def test_a_first_failure_with_no_message_coming_falls_to_the_guarded_resume_past_the_window(world):
    world.order('Synthetic open order.')
    world.write(answered('Synthetic earlier task.', world.at(-3000)), failed('Synthetic task.', world.at(-1000)))
    assert world.now - claude.state(world.seat.record()).failure.at > timedelta(seconds=auth_failure.WINDOW_SECONDS)
    assert not auth_failure.first_failure(world.seat), 'nothing waits silently past the window'
    assert turn_failure.obstruction(world.seat, world.store) == auth_failure.AUTHENTICATION
    auth_failure.run(world.service)
    world.service.relaunch.assert_called_once_with(world.seat, True)


def test_the_next_delivery_failing_at_sign_in_too_brings_the_guarded_resume(world):
    world.order('Synthetic open order.')
    world.write(failed('Synthetic task.', world.at(0)), failed('Synthetic next message.', world.at(10)))
    assert auth_failure.failed_in_a_row(claude.state(world.seat.record())) == 2
    assert turn_failure.obstruction(world.seat, world.store) == auth_failure.AUTHENTICATION, 'now the door holds'
    auth_failure.run(world.service)
    world.service.relaunch.assert_called_once_with(world.seat, True)
    assert 'relaunch with resume (1/3)' in world.service.log.call_args.kwargs['proof']


def test_a_resume_that_does_not_cure_it_needs_attention(world):
    world.order('Synthetic open order.')
    world.write(failed('Synthetic task.', world.at(0)), failed('Synthetic next message.', world.at(10)))
    path = auth_failure._path(world.seat)
    path.parent.mkdir(parents=True, exist_ok=True)
    tries = [dict(at=(world.at(20 + n)).isoformat(), pool='synthetic-pool', credential=[1, 2, 3, 4, n]) for n in range(3)]
    path.write_text(json.dumps(dict(format=1, **tries[-1], attempts=tries)) + '\n')
    world.write(failed('Synthetic carried messages after the last resume.', world.at(40)))
    auth_failure.run(world.service)
    world.service.relaunch.assert_not_called()
    world.service.needs_human.assert_called_once_with(world.seat)
    assert 'relaunch did not cure it; needs attention' in auth_failure.status(world.seat)


@pytest.mark.parametrize('wait, named', [
    (('tools-unknown', 'tool state unknown'), 'the idle proof (tools-unknown): tool state unknown'),
    (('recovery', 'pending delivery journal unreadable; recovery/lifecycle held'),
     'the recovery hold: pending delivery journal unreadable'),
    (None, 'an unnamed lifecycle check')])
def test_a_held_resume_names_its_check(world, wait, named):
    world.write(failed('Synthetic task.', world.at(0)), failed('Synthetic next message.', world.at(10)))

    def held(seat, resume):
        world.service.request_wait = wait
        return None
    world.service.relaunch = Mock(side_effect=held)
    auth_failure.run(world.service)
    proof = world.service.log.call_args.kwargs['proof']
    assert f'{SEAT}: sign-in resume held by {named}' in proof
    world.service.needs_human.assert_called_once_with(world.seat)


def test_a_recovery_error_names_the_step_that_held_it(world):
    world.write(failed('Synthetic task.', world.at(0)), failed('Synthetic next message.', world.at(10)))
    world.seat.record.side_effect = OSError('synthetic unreadable record')
    auth_failure.run(world.service)
    proof = world.service.log.call_args.kwargs['proof']
    assert proof == f"{SEAT}: sign-in recovery held by the seat's record (OSError); needs attention"
    assert 'synthetic unreadable record' not in proof, 'never the error text'


def test_lapsed_access_with_a_renewal_grant_keeps_the_next_delivery_path(world):
    world.validity.state = 'renewable'
    world.order('Synthetic open order.')
    world.write(failed('Synthetic task.', world.at(0)))
    assert turn_failure.obstruction(world.seat, world.store) is None
    status = auth_failure.status(world.seat)
    assert auth_failure.waiting(status) and status.endswith(auth_failure.RENEWABLE_DELIVERY)
    auth_failure.run(world.service)
    world.service.relaunch.assert_not_called()
    world.service.needs_human.assert_not_called()


@pytest.mark.parametrize('pool', ['expired', 'unknown'])
def test_without_a_valid_sign_in_a_first_failure_still_holds_the_door(world, monkeypatch, pool):
    world.order('Synthetic open order.')
    world.write(failed('Synthetic task.', world.at(0)))
    world.validity.state = pool
    assert turn_failure.obstruction(world.seat, world.store) == auth_failure.AUTHENTICATION
    assert not auth_failure.first_failure(world.seat)
    assert not auth_failure.status(world.seat).endswith(auth_failure.DELIVERING)


def test_a_later_answered_turn_ends_the_episode_and_a_new_failure_is_a_first_again(world):
    world.order('Synthetic open order.')
    world.write(failed('Synthetic task.', world.at(0)), failed('Synthetic next.', world.at(10)),
                answered('Synthetic resumed work.', world.at(20)), failed('Synthetic later task.', world.at(30)))
    assert auth_failure.failed_in_a_row(claude.state(world.seat.record())) == 1
    assert turn_failure.obstruction(world.seat, world.store) is None


def test_a_human_seat_is_never_delivered_to_by_this_rule(world):
    world.seat.session.human = True
    world.order('Synthetic open order.')
    world.write(failed('Synthetic task.', world.at(0)))
    assert not auth_failure.first_failure(world.seat)
