"""Ordinary stop bookkeeping for the fixed wake's separate reservation file."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from quarterdeck import human_wake, pending, shutdown
from quarterdeck.door import Door
from quarterdeck.service import Service


def wake_reservation(tmp_path):
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    stamp = datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat()
    name = 'synthetic-human-zzzz'
    proof = pending.Proof(runtime / 'seats' / name / 'human-wake.json', {})
    proof.save(format=1, seat=name, identity=dict(pane='synthetic-pane-zzzz', pid=101,
               launch=stamp, harness='fake'), at=stamp, since=stamp, payload=human_wake.LINE,
               phase='prepared', record=None, shown=None, attempts=[], generation=None, ended=False)
    return runtime, proof


def test_wake_reservation_joins_stop_without_becoming_a_pending_post(tmp_path):
    runtime, proof = wake_reservation(tmp_path)
    original = proof.path.read_bytes()
    with shutdown.Stop(runtime).installed():
        with shutdown.delivery(proof):
            active = shutdown.inflight(runtime, shutdown.owner(runtime))
            assert active['kind'] == 'human wake'
            assert active['journal'] == 'seats/synthetic-human-zzzz/human-wake.json'
            assert active['before_paste']
            assert proof.path.read_bytes() == original
            assert 'delivery_token' not in proof.data and 'service_owner' not in proof.data
        assert not shutdown.pointer_path(runtime).exists()
    assert proof.path.read_bytes() == original


def test_stop_with_pending_wake_leaves_reservation_unchanged_and_unsent(tmp_path):
    runtime, proof = wake_reservation(tmp_path)
    original = proof.path.read_bytes()
    door = Door.__new__(Door)

    def unexpected_input(*args, **kwargs):
        pytest.fail('a requested stop reached the typing path')

    door._deliver = unexpected_input
    with shutdown.Stop(runtime).installed():
        controller = shutdown._controller
        with pytest.raises(SystemExit):
            with shutdown.delivery(proof):
                controller.signal()
                door.deliver(human_wake.LINE, proof=proof, human_wake=True)
    assert proof.path.read_bytes() == original
    assert proof.data['phase'] == 'prepared' and proof.data['shown'] is None


def test_post_exit_prepared_wake_keeps_its_original_reservation(tmp_path, monkeypatch):
    runtime, proof = wake_reservation(tmp_path)
    original = proof.path.read_bytes()
    with shutdown.Stop(runtime).installed():
        owner = shutdown.owner(runtime)
        with pytest.raises(SystemExit):
            with shutdown.delivery(proof):
                shutdown._controller.signal()
    monkeypatch.setattr(shutdown, 'process_gone', lambda expected: True)
    detail = shutdown.annotate(runtime, owner, forced=False)
    assert 'reservation unchanged and unsent' in detail
    assert proof.path.read_bytes() == original
    assert not shutdown.pointer_path(runtime).exists()


def test_post_exit_submitted_wake_preserves_submission_for_its_receipt(tmp_path, monkeypatch):
    runtime, proof = wake_reservation(tmp_path)
    with shutdown.Stop(runtime).installed():
        owner = shutdown.owner(runtime)
        with pytest.raises(SystemExit):
            with shutdown.delivery(proof):
                proof.save(phase='submitted', shown=human_wake.LINE)
                shutdown._controller.signal()
    monkeypatch.setattr(shutdown, 'process_gone', lambda expected: True)
    assert 'delivery reached submitted' in shutdown.annotate(runtime, owner, forced=False)
    saved = human_wake.load(SimpleNamespace(folder=proof.path.parent, name=proof.data['seat']))
    assert saved.data['phase'] == 'submitted'
    assert saved.data['service_stop']['phase'] == 'submitted'
    assert saved.data['attempts'] == []


def test_one_wake_failure_is_named_and_later_crew_deliveries_continue(monkeypatch):
    from quarterdeck import auth_failure, recovery, turn_failure

    human = SimpleNamespace(name='synthetic-human-zzzz', session=SimpleNamespace(human=True), is_up=True)
    crew = SimpleNamespace(name='synthetic-crew-zzzz', session=SimpleNamespace(human=False), is_up=True)
    message = SimpleNamespace(id='synthetic-order-zzzz')
    calls, logs, held = [], [], []
    service = Service.__new__(Service)
    service.store = object()
    service.wake_errors = set()
    service.attempts = SimpleNamespace(latest=lambda: {}, undelivered=lambda *a, **kw: [message])
    service.resolved_seats = lambda: [human, crew]
    service._due = lambda *args: True
    service.deliver = lambda seat, messages: calls.append((seat.name, messages))
    service.needs_human = lambda seat: None
    service.log = lambda event, **fields: logs.append((event, fields))
    monkeypatch.setattr(recovery, 'held', lambda seat: False)
    monkeypatch.setattr(auth_failure, 'status', lambda seat: None)
    monkeypatch.setattr(turn_failure, 'obstruction', lambda *args: None)
    monkeypatch.setattr(human_wake, 'observe_failure', lambda seat, reason: held.append((seat.name, reason)))

    def unavailable(seat):
        raise OSError('synthetic wake bookkeeping unavailable zzzz')

    monkeypatch.setattr(human_wake, 'run', unavailable)
    service.deliveries()
    assert calls == [(crew.name, [message])]
    assert len(held) == 1 and held[0][0] == human.name
    assert held[0][1].startswith('human wake service failure (pass: ')
    assert logs == [('held', dict(step='human_wake', seat=human.name, reason=held[0][1]))]
