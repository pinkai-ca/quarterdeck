"""A declared-against-running model difference is told to the systems seats, except while a change to it is pending:
a request's own continuation, or a model request for that seat and value still queued, for a bounded wait.

The model control's own synthetic Claude seat and service (test_control_confirmation_claude_gates); the reading of
the running model is injected as a difference, and so is the clock past the wait. No program runs.
"""
from dataclasses import replace
from datetime import timedelta

import pytest

from quarterdeck import model_mismatch, post, request_queue
from quarterdeck.model_mismatch import Reading
from .test_control_confirmation_claude_gates import TARGET, owned_request, ship_seat  # noqa: F401 -- a fixture

RUNNING = 'claude-opus-5-5'


@pytest.fixture
def world(ship_seat, monkeypatch):
    made = ship_seat()
    made.seat.spec = replace(made.seat.spec, model=TARGET)  # the ship file declares the new model
    monkeypatch.setattr(model_mismatch, 'observe', lambda seat, records=None: Reading(
        'mismatch', declared=TARGET, running=RUNNING, source='record'))
    made.svc.systems_recipients = lambda seat: ['systems-example']
    return made


def step(world):
    model_mismatch.step(world.svc, world.seat, request_queue.pending_model)


def told(world):
    return [m for m in post.Store(world.svc.runtime).all() if m.to == 'systems-example' and m.kind == 'report']


def unrouted(world):
    return model_mismatch.unrouted(world.svc.ship, [world.seat], pending_model=request_queue.pending_model)


def queued(world):
    return request_queue.enqueue(world.svc.runtime, 'control', dict(seat='worker', name='model', value=TARGET))


def test_with_no_request_pending_a_difference_is_told(world):
    step(world)
    [report] = told(world)
    assert report.body == f'worker: model mismatch; ship declares {TARGET}, program reports {RUNNING} in its record'
    assert unrouted(world) == [f'worker: model mismatch {RUNNING} / declared {TARGET}; no systems seat up']


def test_a_queued_model_request_for_that_seat_and_value_is_pending_and_tells_nothing(world):
    step(world)  # the difference was seen, and told, before the change was asked for
    queued(world)
    assert request_queue.pending_model(world.seat, TARGET) is True
    assert request_queue.pending_model(world.seat, RUNNING) is False, 'only for the value it sets'
    assert unrouted(world) == [], 'the status line names no difference while its change is pending'
    step(world)
    assert len(told(world)) == 1, 'nothing more told while the change waits its turn'


def test_a_change_queued_before_the_difference_is_seen_tells_nothing_until_its_wait_ends(world, monkeypatch):
    path = queued(world)
    arrived = request_queue.created(path, request_queue.load(path))
    from datetime import datetime
    start = datetime.fromisoformat(arrived)
    monkeypatch.setattr(post, 'now', lambda: start + timedelta(seconds=request_queue.QUEUED_MODEL_SECONDS - 1))
    step(world)
    assert told(world) == [] and unrouted(world) == []
    monkeypatch.setattr(post, 'now', lambda: start + timedelta(seconds=request_queue.QUEUED_MODEL_SECONDS + 1))
    assert request_queue.pending_model(world.seat, TARGET) is False, 'stuck past its wait: no longer pending'
    step(world)
    assert len(told(world)) == 1, 'a change stuck in the queue is told, never silent for good'
    assert unrouted(world) == [f'worker: model mismatch {RUNNING} / declared {TARGET}; no systems seat up']


@pytest.mark.parametrize('how', ['claimed without a continuation', 'another seat', 'settled', 'not a model'])
def test_a_request_that_is_not_plainly_pending_suppresses_nothing(world, how):
    path = queued(world)
    payload = request_queue.load(path)
    if how == 'claimed without a continuation':
        path.with_suffix('.claimed').write_text(path.read_text())  # being applied, or its outcome unknown
    elif how == 'another seat':
        path.write_text(__import__('json').dumps(dict(payload, seat='someone-else')) + '\n')
    elif how == 'settled':
        request_queue.save_metadata(path, {'terminal': 'synthetic answer', 'created_at': payload.get('created_at')
                                           or request_queue.created(path, payload)})
    else:
        path.write_text(__import__('json').dumps(dict(payload, name='effort')) + '\n')
    assert request_queue.pending_model(world.seat, TARGET) is False
    step(world)
    assert len(told(world)) == 1


def test_a_request_with_its_continuation_tells_nothing_as_before(world):
    owned_request(world)
    assert request_queue.pending_model(world.seat, TARGET) is True
    step(world)
    assert told(world) == [] and unrouted(world) == []
