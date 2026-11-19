"""A finished transport may remain unconfirmed while its open order is carried."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from quarterdeck import drill


def exercise(tmp_path, monkeypatch, phase):
    clock = [0.0]
    run = drill.Drill(SimpleNamespace(runtime=tmp_path), Mock(), settlement_guard=.08,
                      settlement_clock=lambda: clock[0])
    def advance(seconds):
        clock[0] += seconds
    monkeypatch.setattr(drill.service_wait, 'Arrivals', lambda runtime: SimpleNamespace(
        refresh=lambda *args, **kwargs: None, wait=advance, close=lambda: None))
    order = run.store.new('order', 'ship', 'worker', 'still open across fresh')
    seat = SimpleNamespace(name='worker', session=SimpleNamespace(human=False),
                           fresh=Mock(return_value='fresh proof'), launched=lambda: SimpleNamespace(at='new launch'))
    attempt = run.attempts.open(order, seat.name, '%synthetic', 123, 'synthetic launch')
    run.attempts.close(attempt, 'blind', 'transport finished without record confirmation')
    proofs = [] if phase is None else [SimpleNamespace(data=dict(phase=phase, attempts=[attempt.path.name]))]
    monkeypatch.setattr(drill, 'journals', lambda _: (proofs, []))
    return run, order, seat


@pytest.mark.parametrize('phase', [None, 'cancelled', 'carried'])
def test_finished_blind_input_can_be_carried_without_acceptance_in_the_old_program(tmp_path, monkeypatch, phase):
    run, order, seat = exercise(tmp_path, monkeypatch, phase)
    proof, reason = run.fresh_after_delivery(seat, order)
    assert proof == 'fresh proof'
    assert reason == 'carried after finished blind attempt'
    assert run.store.is_open(order)
    seat.fresh.assert_called_once_with()


@pytest.mark.parametrize('phase', ['prepared', 'pasting', 'submitting', 'submitted'])
def test_blind_result_does_not_make_an_active_paste_or_submit_settled(tmp_path, monkeypatch, phase):
    run, order, seat = exercise(tmp_path, monkeypatch, phase)
    proof, _ = run.fresh_after_delivery(seat, order)
    assert proof is None
    seat.fresh.assert_not_called()
    assert run.store.is_open(order)
