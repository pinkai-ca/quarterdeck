"""A report and its delivery receipt are separate asynchronous facts."""

from types import SimpleNamespace

import pytest

from quarterdeck import drill, post


@pytest.mark.parametrize('duplicate', [False, True])
def test_matrix_waits_for_receipts_without_weakening_exactly_once(tmp_path, monkeypatch, duplicate):
    exercise = drill.Drill.__new__(drill.Drill)
    exercise.store = post.Store(tmp_path)
    exercise.attempts = post.Attempts(tmp_path)
    exercise.ship = SimpleNamespace(seats={'one':None,'two':None}, session_of=lambda _:SimpleNamespace(human=False))
    exercise.patience = 1
    exercise.checks = []
    exercise.seats = lambda: []
    def wait(predicate, seconds):
        orders = [m for m in exercise.store.all() if m.kind == 'order']
        for order in orders:
            exercise.store.new('report',order.to,order.sender,'done',re=order.id)
        assert not predicate(), 'closing reports alone must not end the wait'
        for order in orders:
            a = exercise.attempts.open(order,order.to,'%1',1,None)
            exercise.attempts.close(a,'delivered','record evidence')
        if duplicate:
            a = exercise.attempts.open(orders[0],orders[0].to,'%1',1,None)
            exercise.attempts.close(a,'delivered','duplicate')
        assert predicate()
        return True
    monkeypatch.setattr(drill,'wait_for',wait)
    exercise.matrix('synthetic')
    assert all(c.passed for c in exercise.checks) is not duplicate  # law: one-door
