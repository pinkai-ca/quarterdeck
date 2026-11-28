"""Ordinary queue scheduling: quick work yields, while lifecycle work keeps its step."""
from contextlib import contextmanager
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import request_queue, service, ship


@pytest.fixture
def quick_clock(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(service, 'time', SimpleNamespace(monotonic=lambda: clock.now))
    return clock


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


def test_only_submit_and_resend_run_between_duties(owner, quick_clock):
    kinds = ['relaunch', 'submit', 'select', 'control', 'resend', 'stop', 'press', 'type',
             'rebrief', 'cascade', 'fresh-ready']
    paths = [publish(owner, f'example-{i}', kind, i + 1) for i, kind in enumerate(kinds)]
    owner.drain_requests()
    assert [row['kind'] for row in owner.calls] == ['submit', 'resend']
    assert all(path.exists() == (kind not in request_queue.QUICK_KINDS) for path, kind in zip(paths, kinds))
    owner.requests()
    assert len(owner.calls) == len(kinds)
    assert [row['kind'] for row in owner.calls[2:]] == [kind for kind in kinds if kind not in request_queue.QUICK_KINDS]


def test_arrival_order_is_not_the_filename_order(owner, quick_clock):
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


def test_refused_answers_remain_and_neither_drain_repeats_them(owner, quick_clock):
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


def test_a_scan_resumes_past_unrelated_requests_with_one_operation_per_slice(owner, quick_clock, monkeypatch):
    monkeypatch.setattr(service, 'QUICK_REQUEST_OPERATIONS', 1)
    for i in range(4):
        publish(owner, f'lifecycle-{i}', 'select', i + 1)
    publish(owner, 'quick', 'submit', 5)
    for _ in range(5):
        owner.drain_requests()
        assert not owner.calls
    owner.drain_requests()
    assert [row['kind'] for row in owner.calls] == ['submit']


def test_one_atomic_request_can_finish_but_the_next_waits_for_another_slice(owner, quick_clock):
    first = publish(owner, 'first', 'submit', 1)
    second = publish(owner, 'second', 'resend', 2)
    answer = owner.answer

    def elapsed(data):
        quick_clock.now += 2 * service.QUICK_REQUEST_SECONDS
        return answer(data)

    owner.answer = elapsed
    owner.drain_requests()
    assert [row['kind'] for row in owner.calls] == ['submit']
    assert first.with_suffix('.done').read_text() == 'answered submit\n'
    assert second.exists()
    owner.drain_requests()
    assert [row['kind'] for row in owner.calls] == ['submit', 'resend']
    assert second.with_suffix('.done').read_text() == 'answered resend\n'


def test_a_busy_owner_keeps_the_earlier_quick_request_first(owner, quick_clock, monkeypatch):
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


def test_a_path_selected_by_an_earlier_scan_is_rechecked_before_dispatch(owner, quick_clock):
    path = publish(owner, 'lifecycle', 'select', 1)
    owner.quick_request_scan = iter([path])
    owner.drain_requests()
    assert not owner.calls and path.exists()
    owner.requests()
    assert [row['kind'] for row in owner.calls] == ['select']


def test_a_failed_second_stat_leaves_the_full_writer_its_candidate(owner, monkeypatch):
    path = publish(owner, 'stat-failure', 'submit', 1)
    original, calls = Path.stat, []

    def second_stat_fails(self, *args, **kwargs):
        if self == path:
            calls.append(self)
            if len(calls) == 2:
                raise OSError('synthetic second stat failed')
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'stat', second_stat_fails)
    candidates = [p for p in request_queue.arrival_paths(owner.runtime) if p is not None]
    assert candidates == [path] and not owner.calls
    assert not path.with_suffix('.claimed').exists() and not path.with_suffix('.done').exists()
    owner._requests(candidates)
    assert [row['kind'] for row in owner.calls] == ['submit']
    assert path.with_suffix('.done').read_text() == 'answered submit\n'
