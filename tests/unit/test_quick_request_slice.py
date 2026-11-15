"""Ordinary queue scheduling: quick work yields, while lifecycle work keeps its step."""
from contextlib import contextmanager
import os

import pytest

from quarterdeck import request_queue, service, ship


@pytest.fixture
def owner(drydock):
    found = object.__new__(service.Service)
    found.runtime = ship.load(drydock).runtime
    found.quick_request_scan = found.quick_request_retry = None
    found.calls, found.events = [], []

    def answer(data):
        found.calls.append(data)
        return f"answered {data['kind']}"

    found.answer = answer
    found.log = lambda event, **fields: found.events.append((event, fields))
    return found


def publish(owner, name, kind, arrival):
    path = request_queue.enqueue(owner.runtime, kind, dict(seat=name), name=name)
    os.utime(path, ns=(arrival, arrival))
    return path


def test_only_submit_and_resend_run_between_duties(owner):
    kinds = ['relaunch', 'submit', 'select', 'control', 'resend', 'stop', 'press', 'type',
             'rebrief', 'cascade', 'fresh-ready']
    paths = [publish(owner, f'example-{i}', kind, i + 1) for i, kind in enumerate(kinds)]
    owner.drain_requests()
    assert [row['kind'] for row in owner.calls] == ['submit', 'resend']
    assert all(path.exists() == (kind not in request_queue.QUICK_KINDS) for path, kind in zip(paths, kinds))
    owner.requests()
    assert len(owner.calls) == len(kinds)
    assert [row['kind'] for row in owner.calls[2:]] == [kind for kind in kinds if kind not in request_queue.QUICK_KINDS]


def test_arrival_order_is_not_the_filename_order(owner):
    publish(owner, 'z-old', 'submit', 1)
    publish(owner, 'm-middle', 'resend', 2)
    publish(owner, 'a-new', 'submit', 3)
    owner.drain_requests()
    assert [row['seat'] for row in owner.calls] == ['z-old', 'm-middle', 'a-new']


def test_the_full_step_uses_the_same_arrival_order(owner):
    publish(owner, 'z-old', 'submit', 1)
    publish(owner, 'a-new', 'submit', 2)
    owner.requests()
    assert [row['seat'] for row in owner.calls] == ['z-old', 'a-new']


def test_refused_answers_remain_and_neither_drain_repeats_them(owner):
    path = publish(owner, 'refused', 'submit', 1)

    def refuse(data):
        owner.calls.append(data)
        raise ship.ShipError('synthetic refusal')

    owner.answer = refuse
    owner.drain_requests()
    assert path.with_suffix('.done').read_text() == 'refused: synthetic refusal\n'
    for _ in range(3):
        owner.drain_requests()
    owner.requests()
    assert len(owner.calls) == 1 and not path.exists()
    assert path.with_suffix('.done').read_text() == 'refused: synthetic refusal\n'


def test_a_scan_resumes_past_unrelated_requests_with_one_operation_per_slice(owner, monkeypatch):
    monkeypatch.setattr(service, 'QUICK_REQUEST_OPERATIONS', 1)
    monkeypatch.setattr(service, 'QUICK_REQUEST_SECONDS', 60)
    for i in range(4):
        publish(owner, f'lifecycle-{i}', 'select', i + 1)
    publish(owner, 'quick', 'submit', 5)
    for _ in range(5):
        owner.drain_requests()
        assert not owner.calls
    owner.drain_requests()
    assert [row['kind'] for row in owner.calls] == ['submit']


def test_one_atomic_request_can_finish_but_the_next_waits_for_another_slice(owner, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(service.time, 'monotonic', lambda: clock[0])
    publish(owner, 'first', 'submit', 1)
    publish(owner, 'second', 'resend', 2)
    answer = owner.answer

    def elapsed(data):
        clock[0] += 1
        return answer(data)

    owner.answer = elapsed
    owner.drain_requests()
    assert [row['kind'] for row in owner.calls] == ['submit']
    owner.drain_requests()
    assert [row['kind'] for row in owner.calls] == ['submit', 'resend']


def test_a_busy_owner_keeps_the_earlier_quick_request_first(owner, monkeypatch):
    busy = [True]

    @contextmanager
    def coordination(*args, **kwargs):
        yield not busy[0]

    monkeypatch.setattr(request_queue, 'coordination', coordination)
    publish(owner, 'z-first', 'submit', 1)
    publish(owner, 'a-second', 'submit', 2)
    owner.drain_requests()
    assert not owner.calls and owner.quick_request_retry.name == 'z-first.json'
    busy[0] = False
    owner.drain_requests()
    assert [row['seat'] for row in owner.calls] == ['z-first', 'a-second']


def test_a_path_selected_by_an_earlier_scan_is_rechecked_before_dispatch(owner):
    path = publish(owner, 'lifecycle', 'select', 1)
    owner.quick_request_scan = iter([path])
    owner.drain_requests()
    assert not owner.calls and path.exists()
    owner.requests()
    assert [row['kind'] for row in owner.calls] == ['select']
