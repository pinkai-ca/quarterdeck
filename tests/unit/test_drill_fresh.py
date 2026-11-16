"""Fresh-start carriage does not depend on an old-conversation delivery."""
from types import SimpleNamespace
from unittest.mock import Mock
import threading

import pytest

from quarterdeck import drill, lifecycle


@pytest.mark.parametrize('previous', [None, 'withheld', 'cancelled'])
def test_fresh_carries_an_open_order_without_prior_acceptance(tmp_path, monkeypatch, previous):
    exercise = drill.Drill(SimpleNamespace(runtime=tmp_path), Mock(), patience=.02,
                           settlement_clock=lambda: 0.0)
    order = exercise.store.new('order', 'ship', 'worker', 'still open across fresh')
    seat = SimpleNamespace(name='worker', session=SimpleNamespace(human=False), fresh=Mock(return_value='fresh proof'),
                           launched=lambda: SimpleNamespace(at='new launch'))
    if previous is not None:
        attempt = exercise.attempts.open(order, seat.name, '%synthetic', 123, 'synthetic launch')
        exercise.attempts.close(attempt, previous, 'no input left in the old conversation')
    monkeypatch.setattr(drill, 'journals', lambda _: ([], []))
    proof, reason = exercise.fresh_after_delivery(seat, order)
    assert proof == 'fresh proof'
    assert reason == ('carried unattempted' if previous is None else f'carried after settled {previous}')
    assert exercise.store.is_open(order)
    seat.fresh.assert_called_once_with()


@pytest.mark.parametrize('winner', ['delivery', 'fresh'])
def test_delivery_and_fresh_use_one_claim_even_when_they_race(tmp_path, monkeypatch, winner):
    exercise = drill.Drill(SimpleNamespace(runtime=tmp_path), Mock(), patience=1)
    order = exercise.store.new('order', 'ship', 'worker', 'carry this open order')
    seat = SimpleNamespace(name='worker', session=SimpleNamespace(human=False),
                           launched=lambda: SimpleNamespace(at='new launch'))
    worker = drill.service.Service.__new__(drill.service.Service)
    worker.runtime = tmp_path
    sequence = []
    held, release = threading.Event(), threading.Event()
    def delivery_body(seat, messages, resend):
        sequence.append('delivery start')
        attempt = exercise.attempts.open(order, seat.name, '%synthetic', 123, 'old launch')
        held.set()
        if winner == 'delivery':
            assert release.wait(2)
        exercise.attempts.close(attempt, 'delivered', 'settled')
        sequence.append('delivery end')
        return 'delivered'
    worker._deliver_claimed = delivery_body
    def fresh_body():
        with lifecycle.claim(tmp_path, seat.name):
            sequence.append('fresh start')
            if winner == 'fresh':
                assert 'another lifecycle transition' in worker.deliver(seat, [order])
                assert sequence == ['fresh start']
            sequence.append('fresh end')
            return 'fresh proof'
    seat.fresh = fresh_body
    thread = None
    def evidence(_):
        nonlocal thread
        if winner == 'delivery' and thread is None:
            # Delivery wins after the drill's snapshot, before fresh takes its claim.
            thread = threading.Thread(target=lambda: worker.deliver(seat, [order]), daemon=True)
            thread.start()
            assert held.wait(2)
        return [], []
    monkeypatch.setattr(drill, 'journals', evidence)
    def wait(predicate, seconds):
        if winner == 'delivery':
            assert predicate() is None
            assert sequence == ['delivery start']
            release.set()
            thread.join(2)
            assert not thread.is_alive()
        return predicate()
    monkeypatch.setattr(drill, 'wait_for', wait)
    try:
        proof, mode = exercise.fresh_after_delivery(seat, order)
    finally:
        release.set()
        if thread is not None:
            thread.join(2)
    assert proof == 'fresh proof'
    if winner == 'delivery':
        assert sequence == ['delivery start', 'delivery end', 'fresh start', 'fresh end']
        assert mode == 'delivered first'
    else:
        assert mode == 'carried unattempted'
        assert worker.deliver(seat, [order]) == 'delivered'
        assert sequence == ['fresh start', 'fresh end', 'delivery start', 'delivery end']


def test_fresh_waits_for_an_existing_delivery_before_carrying_the_order(tmp_path, monkeypatch):
    exercise = drill.Drill(SimpleNamespace(runtime=tmp_path), Mock(), patience=1)
    order = exercise.store.new('order', 'ship', 'worker', 'still open across fresh')
    seat = SimpleNamespace(name='worker', session=SimpleNamespace(human=False), fresh=Mock(return_value='fresh proof'),
                           launched=lambda: SimpleNamespace(at='new launch'))
    attempt = exercise.attempts.open(order, seat.name, '%synthetic', 123, 'synthetic launch')
    monkeypatch.setattr(drill, 'journals', lambda _: ([], []))
    def wait(predicate, seconds):
        assert predicate() is None
        seat.fresh.assert_not_called()
        exercise.attempts.close(attempt, 'delivered', 'receipt settled')
        return predicate()
    monkeypatch.setattr(drill, 'wait_for', wait)
    assert exercise.fresh_after_delivery(seat, order) == ('fresh proof', 'delivered first')
    seat.fresh.assert_called_once_with()


def test_a_truncated_drill_report_names_failed_checks_first(tmp_path, monkeypatch):
    folder = tmp_path / 'owned-drill'
    folder.mkdir()
    ship = SimpleNamespace(runtime=folder / 'runtime')
    server = SimpleNamespace(socket='synthetic binding', close=lambda: True)
    monkeypatch.setattr(drill, 'temporary_drydock', lambda: (ship, server, folder))
    monkeypatch.setattr(drill.seatmod, 'bring_down', lambda *args: None)
    monkeypatch.setattr(drill.Drill, 'run', lambda _: [drill.Check('first pass', True),
                        drill.Check('fresh failed', False), drill.Check('carriage failed', False)])
    passed, lines = drill.run(None)
    assert not passed
    assert lines == ['FAIL  fresh failed', 'FAIL  carriage failed', 'PASS  first pass', '2 failed of 3 checks']



def test_generated_drydock_leaves_automatic_fresh_to_the_explicit_drill():
    import shutil
    from quarterdeck import ship
    original = (drill.DRYDOCK / 'ship.toml').read_bytes()
    assert ship.load(drill.DRYDOCK).seats['worker-1'].fresh == 'after-order'
    generated, tmux, folder = drill.temporary_drydock()
    try:
        assert all(seat.fresh is None for seat in generated.seats.values())
        assert (drill.DRYDOCK / 'ship.toml').read_bytes() == original
    finally:
        tmux.close()
        shutil.rmtree(folder)
