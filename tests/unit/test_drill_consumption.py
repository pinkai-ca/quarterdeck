"""The crash validator counts acceptances, not multiple views of consumption."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from quarterdeck import drill, input_receipt, post
from quarterdeck.harness import claude, synthetic_record
from quarterdeck.harness.synthetic_record import ClaudeShape
from quarterdeck.harness.types import Input


@pytest.fixture
def crash(tmp_path, monkeypatch):
    shape = ClaudeShape(tmp_path / 'home', tmp_path / 'workspace', 'synthetic', 'model')
    shape.begin('previous input', 'model', 'low')
    adapter = SimpleNamespace(synthetic=True, inputs=claude.inputs,
                              screen=lambda _: SimpleNamespace(working=False))
    captain = SimpleNamespace(name='worker', home=tmp_path / 'home', harness=adapter,
                              session=SimpleNamespace(human=False),
                              pane=lambda: SimpleNamespace(id='%synthetic', pid=123), record=lambda: shape.path)
    exercise = drill.Drill(SimpleNamespace(runtime=tmp_path), Mock(), patience=0.01)
    monkeypatch.setattr(exercise, 'seats', lambda: [captain])
    monkeypatch.setattr(drill, 'wait_for', lambda predicate, *args, **kwargs: predicate())
    monkeypatch.setattr(drill, 'Rendezvous', lambda *args: SimpleGate())
    monkeypatch.setattr(drill.service, 'read_heartbeat', lambda _: (123,))
    monkeypatch.setattr(drill.service, 'alive', lambda _: False)
    kill = Mock()
    monkeypatch.setattr(drill.os, 'kill', kill)
    state = SimpleNamespace(exercise=exercise, shape=shape, sends=1, attempts=1, submissions=1,
                            queued=False, stale=False, same_time=False, direct=False, kill=kill)

    def caught(_, order, gate):
        state.order = order
        state.attempt = exercise.attempts.open(order, captain.name, '%synthetic', 123, 'synthetic launch')
        return True  # Crash-observation refusal is tested independently in test_drill.py.
    monkeypatch.setattr(exercise, 'crash_attempt', caught)

    def restart(_):
        payload = post.typed([state.order]).strip()
        origin = state.attempt.receipt_since + timedelta(seconds=-1 if state.stale else 1)
        clock = iter(origin + timedelta(microseconds=i) for i in range(20))
        monkeypatch.setattr(synthetic_record, 'now', lambda: origin if state.same_time else next(clock))
        for _ in range(state.sends):
            if state.direct:
                shape.begin(payload, 'model', 'low')
                continue
            shape.queued(payload)
            if not state.queued:
                shape.dequeued(payload)
                shape.begin(payload, 'model', 'low')
        exercise.attempts.close(state.attempt, 'delivered')
        for _ in range(state.attempts - 1):
            extra = exercise.attempts.open(state.order, captain.name, '%synthetic', 123, 'synthetic launch')
            exercise.attempts.close(extra, 'delivered')
        exercise.store.new('report', captain.name, post.SHIP, 'survived', re=state.order.id)
        state.evidence = [i for i in claude.inputs(shape.path) if i.text == payload]
        state.receipt = input_receipt.confirmed(adapter, shape.path, payload, state.attempt.receipt_since)
        return 'synthetic service restarted'
    monkeypatch.setattr(drill.service, 'start', restart)
    monkeypatch.setattr(drill, 'events', lambda _: [
        {'event': 'submitted', 'text': post.typed([state.order]).strip()} for _ in range(state.submissions)])
    return state


class SimpleGate:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def release(self):
        pass


def test_crash_accepts_fresh_consumed_views_of_one_original_enqueue(crash):
    crash.exercise.crash()
    assert [i.kind for i in crash.evidence] == ['consumed', 'consumed']
    assert len({i.at for i in crash.evidence}) == 1
    assert crash.receipt == 'delivered'
    check, = crash.exercise.checks
    assert check.passed, str(check)
    assert 'in the record 1 time(s); submissions 1;' in check.detail
    crash.kill.assert_called_once()


def test_crash_still_accepts_one_direct_input(crash):
    crash.direct = True
    crash.exercise.crash()
    assert [i.kind for i in crash.evidence] == ['accepted']
    assert crash.receipt == 'delivered'
    assert crash.exercise.checks[0].passed


@pytest.mark.parametrize('changes,receipt', [
    ({'sends': 2, 'submissions': 2}, 'delivered'),
    ({'sends': 2}, 'delivered'),  # distinct enqueue times reject even with an undercounted event log
    ({'sends': 2, 'submissions': 2, 'same_time': True}, 'delivered'),
    ({'sends': 2, 'direct': True, 'same_time': True}, 'delivered'),
    ({'submissions': 2}, 'delivered'),
    ({'attempts': 2}, 'delivered'),
    ({'queued': True}, 'queued'),
    ({'stale': True}, None),
    ({'stale': True, 'direct': True}, None),
])
def test_crash_still_refuses_duplicates_queueing_and_stale_acceptance(crash, changes, receipt):
    for key, value in changes.items():
        setattr(crash, key, value)
    crash.exercise.crash()
    assert crash.receipt == receipt
    check, = crash.exercise.checks
    assert not check.passed, str(check)


def test_count_requires_exact_fresh_timed_consumption():
    at = post.now()
    before, after = at - timedelta(seconds=1), at + timedelta(seconds=1)
    inputs = [Input('exact  input', at, 'queued'),
              Input('exact  input', None, 'consumed', after),
              Input('exact  input', before, 'consumed', after),
              Input('exact  input', at, 'consumed'),
              Input('exact  input', at, 'consumed', before),
              Input('exact  input', at, 'unknown', after),
              Input('exact input', at), Input('other', at), Input('exact  input', None)]
    assert drill.acceptances(inputs, 'exact  input', at) == 0
    inputs += [Input('exact  input', at, 'consumed', at),
               Input('exact  input', at, 'consumed', after)]
    assert drill.acceptances(inputs, 'exact  input', at) == 1
    inputs += [Input('exact  input', after, 'consumed', after)]
    assert drill.acceptances(inputs, 'exact  input', at) == 2
