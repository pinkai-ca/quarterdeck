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


ABSENT = object()


def spent_look(tmp_path, monkeypatch, *, phase='pasting', look='held', receipts=('blind',), extra=()):
    """A journal naming one attempt per receipt: 'missing' names none on file, None leaves one open."""
    run, order, seat = exercise(tmp_path, monkeypatch, None)
    closed = run.attempts.for_message(order.id)
    names = [a.path.name for a in closed]
    for receipt in receipts:
        if receipt == 'missing':
            names.append(f'{order.id}-99.json')
            continue
        attempt = run.attempts.open(order, seat.name, '%synthetic', 123, 'synthetic launch')
        names.append(attempt.path.name)
        if receipt is not None:
            run.attempts.close(attempt, receipt, 'synthetic receipt')
    data = dict(phase=phase, attempts=names)
    if look is not ABSENT:
        data['look'] = dict(at='synthetic', outcome=look) if isinstance(look, str) else look
    proofs = [SimpleNamespace(data=data), *(SimpleNamespace(data=dict(item, attempts=names)) for item in extra)]
    monkeypatch.setattr(drill, 'journals', lambda _: (proofs, []))
    return run, order, seat


@pytest.mark.parametrize('look', ['held', 'second delivery'])
@pytest.mark.parametrize('receipts', [('blind',), ('blind', 'blind')])
def test_a_blind_paste_whose_look_is_spent_is_left_to_the_fresh(tmp_path, monkeypatch, look, receipts):
    run, order, seat = spent_look(tmp_path, monkeypatch, look=look, receipts=receipts)
    assert run.fresh_after_delivery(seat, order) == ('fresh proof', 'carried after finished blind attempt')
    seat.fresh.assert_called_once_with()
    assert run.store.is_open(order)


@pytest.mark.parametrize('look', ['held', 'second delivery'])
@pytest.mark.parametrize('phase', ['prepared', 'submitting', 'submitted'])
def test_a_spent_look_settles_only_a_paste(tmp_path, monkeypatch, phase, look):
    run, order, seat = spent_look(tmp_path, monkeypatch, phase=phase, look=look)
    proof, reason = run.fresh_after_delivery(seat, order)
    assert proof is None and f'still being typed or confirmed ({phase}:' in reason
    seat.fresh.assert_not_called()


@pytest.mark.parametrize('receipts', [('missing', 'blind'), ('pending', 'blind'), ('withheld', 'blind'), (None, 'blind')])
def test_every_named_attempt_must_be_closed_blind(tmp_path, monkeypatch, receipts):
    run, order, seat = spent_look(tmp_path, monkeypatch, receipts=receipts)
    proof, reason = run.fresh_after_delivery(seat, order)
    assert proof is None and ('still being typed' in reason or 'still unfinished' in reason)
    seat.fresh.assert_not_called()


@pytest.mark.parametrize('look', [ABSENT, 'retyping', 'retype not typed', 'retyped', 'submitting', 'submitted',
                                  'not submitted', 'delivered', ['held'], {'outcome': ['held']}, {}])
def test_an_unfinished_or_unreadable_look_keeps_waiting(tmp_path, monkeypatch, look):
    run, order, seat = spent_look(tmp_path, monkeypatch, look=look)
    proof, reason = run.fresh_after_delivery(seat, order)
    assert proof is None and 'still being typed or confirmed (pasting:' in reason
    seat.fresh.assert_not_called()


def test_one_spent_journal_does_not_settle_another_that_is_active(tmp_path, monkeypatch):
    run, order, seat = spent_look(tmp_path, monkeypatch, extra=[dict(phase='submitted')])
    proof, reason = run.fresh_after_delivery(seat, order)
    assert proof is None and 'still being typed or confirmed (submitted:' in reason
    seat.fresh.assert_not_called()


def test_unreadable_journal_evidence_keeps_waiting(tmp_path, monkeypatch):
    run, order, seat = spent_look(tmp_path, monkeypatch)
    proofs, _ = drill.journals(seat)
    monkeypatch.setattr(drill, 'journals', lambda _: (proofs, ['synthetic unreadable journal']))
    proof, reason = run.fresh_after_delivery(seat, order)
    assert proof is None and 'pending delivery evidence is unreadable' in reason
    seat.fresh.assert_not_called()
