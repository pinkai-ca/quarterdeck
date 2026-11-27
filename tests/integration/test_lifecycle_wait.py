"""Explicit lifecycle commands wait for a concurrent seat transition to finish."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from types import SimpleNamespace

import pytest

from quarterdeck import cli, idle, lifecycle, request_queue, seat as seatmod, ship as shipmod
from quarterdeck.service import Service
from .test_launch_lifecycle import process_identity, windows
from .waiting import start_with_conversation, wait_for


@pytest.mark.parametrize('operation', ['restart', 'fresh'])
def test_service_lifecycle_requests_refuse_busy_claim_without_waiting(drydock, tmux, monkeypatch, operation):
    ship = shipmod.load(drydock)
    seat = seatmod.Seat(ship, ship.seat('worker-1'), tmux)
    seat.up(False)
    prior = seat.launched()
    runner = Service.__new__(Service)
    runner.runtime = ship.runtime
    runner.seat = lambda name: seat
    runner.resolved_seats = lambda: [seat]
    runner.log = lambda *args, **kwargs: None
    # Synthetic admission isolates ordinary lifecycle contention from idle-policy tests.
    monkeypatch.setattr('quarterdeck.service.idle.reading', lambda *args, **kwargs: (None, None))
    path = request_queue.enqueue(ship.runtime, 'relaunch', dict(seat=seat.name, resume=operation == 'restart'),
                                 name='request-busy-0-zz')
    later = request_queue.enqueue(ship.runtime, 'unknown', {}, name='request-healthy-0-zz')

    def no_wait(seconds):
        pytest.fail('service request waited on a lifecycle claim')

    with lifecycle.claim(ship.runtime, seat.name), monkeypatch.context() as immediate:
        # The request must finish while the claim is still held, without trying to sleep.
        immediate.setattr(lifecycle, 'time', SimpleNamespace(monotonic=time.monotonic, sleep=no_wait))
        runner.requests()
        assert path.with_suffix('.done').read_text() == (
            f'refused: {seat.name}: another lifecycle transition is in progress; retry when it ends\n')
        assert later.with_suffix('.done').read_text() == "unknown request 'unknown'\n"
        assert not path.exists() and not path.with_suffix('.claimed').exists()
        assert seat.launched() == prior and seat.pane().pid == prior.pid


def test_service_select_stays_queued_on_a_busy_claim_then_applies(drydock, tmux, monkeypatch, key_file):
    key_file.write(paid='sk-synthetic-busy-select-test')
    file = drydock / 'ship.toml'
    current = shipmod.load(drydock).seat('worker-1').pools[0]
    text = file.read_text() + '\n[pools.paid]\nprovider="synthetic"\napi_key="keys:paid"\n'
    file.write_text(shipmod.edit(text, 'seats.worker-1', 'pool', [current, 'paid']))
    ship = shipmod.load(drydock)
    seat = seatmod.Seat(ship, ship.seat('worker-1'), tmux)
    start_with_conversation(seat)
    wait_for(lambda: idle.reading(seat)[0] is not None and idle.reading(seat)[1] is None)
    prior, record = seat.launched(), seat.record()
    runner = Service(ship, tmux)
    try:
        path = request_queue.enqueue(ship.runtime, 'select', dict(seat=seat.name, pool='paid'),
                                     name='request-busy-0-zz')
        later = request_queue.enqueue(ship.runtime, 'unknown', {}, name='request-healthy-0-zz')

        def no_wait(seconds):
            pytest.fail('service request waited on a lifecycle claim')

        with lifecycle.claim(ship.runtime, seat.name), monkeypatch.context() as immediate:
            immediate.setattr(lifecycle, 'time', SimpleNamespace(monotonic=time.monotonic, sleep=no_wait))
            runner.requests()
            assert later.with_suffix('.done').read_text() == "unknown request 'unknown'\n"
            assert path.exists() and not path.with_suffix('.done').exists()
            assert not path.with_suffix('.claimed').exists()
            [row] = request_queue.pending(ship.runtime, seat.name)
            assert row['dimension'] == 'lifecycle-busy' and row['claimed'] is False
            assert row['reason'] == 'move to pool paid: another lifecycle transition is in progress'
            assert seat.launched() == prior and seat.pane().pid == prior.pid

        runner.requests()
        answer = path.with_suffix('.done').read_text()
        assert answer.startswith(f'{seat.name} moved to paid from {current}')
        assert not path.exists() and not path.with_suffix('.claimed').exists()
        assert request_queue.pending(ship.runtime, seat.name) == []
        moved = seatmod.Seat(ship, ship.seat('worker-1'), tmux)
        assert moved.launched().pool == 'paid' and moved.launched().pool_selected == 'paid'
        assert moved.launched().resume == seat.harness.session_id(record) and moved.record() == record
    finally:
        runner.ledger.close()


@pytest.mark.parametrize('whole_ship', [False, True], ids=['one-seat', 'whole-ship'])
def test_qd_down_waits_for_a_transition_then_retires_the_launch(drydock, tmux, monkeypatch, capsys, whole_ship):
    ship = shipmod.load(drydock)
    seat = seatmod.Seat(ship, ship.seat('worker-1'), tmux)
    seat.up(False)
    prior = seat.launched()
    monkeypatch.setenv('QD_SHIP', str(drydock))
    monkeypatch.setenv('QD_TMUX_SOCKET', tmux.socket)
    monkeypatch.delenv('QD_SEAT', raising=False)
    contended = threading.Event()
    original = lifecycle.fcntl.flock

    def observe(descriptor, operation):
        try:
            return original(descriptor, operation)
        except BlockingIOError:
            contended.set()
            raise

    monkeypatch.setattr(lifecycle.fcntl, 'flock', observe)
    argv = ['down'] if whole_ship else ['down', seat.name]
    with ThreadPoolExecutor(max_workers=1) as callers:
        with lifecycle.claim(ship.runtime, seat.name):
            down = callers.submit(cli.main, argv)
            assert contended.wait(10), 'down did not encounter the held transition'
            assert seat.launched() == prior and seat.pane().pid == prior.pid
        assert down.result(timeout=40) == 0, capsys.readouterr().err
    assert seat.launched() is None
    assert windows(tmux) == []
    wait_for(lambda: process_identity(prior.pid) is None)


@pytest.mark.parametrize('operation', ['up', 'restart', 'fresh'])
def test_explicit_lifecycle_commands_wait_before_starting(drydock, tmux, monkeypatch, capsys, operation):
    ship = shipmod.load(drydock)
    seat = seatmod.Seat(ship, ship.seat('worker-1'), tmux)
    if operation == 'restart':
        start_with_conversation(seat)
    else:
        seat.up(False)
    wait_for(lambda: seat.harness.screen(tmux.capture(seat.pane().id)).working is False)
    if operation == 'up':
        seat.down()
    prior = seat.launched()
    monkeypatch.setenv('QD_SHIP', str(drydock))
    monkeypatch.setenv('QD_TMUX_SOCKET', tmux.socket)
    monkeypatch.delenv('QD_SEAT', raising=False)
    # This test exercises the CLI's seat transition, without starting an unrelated service.
    monkeypatch.setattr(cli.service, 'start', lambda ship: 'service not started by this test')
    contended = threading.Event()
    original = lifecycle.fcntl.flock

    def observe(descriptor, flags):
        try:
            return original(descriptor, flags)
        except BlockingIOError:
            contended.set()
            raise

    monkeypatch.setattr(lifecycle.fcntl, 'flock', observe)
    with ThreadPoolExecutor(max_workers=1) as callers:
        with lifecycle.claim(ship.runtime, seat.name):
            command = callers.submit(cli.main, [operation, seat.name])
            assert contended.wait(10), 'command did not encounter the held transition'
            assert seat.launched() == prior
            assert seat.pane() is None if prior is None else seat.pane().pid == prior.pid
        assert command.result(timeout=40) == 0, capsys.readouterr().err
    saved = seat.launched()
    assert len(windows(tmux)) == 1 and seat.pane().pid == saved.pid
    assert saved is not None and saved != prior
    if prior is not None:
        wait_for(lambda: process_identity(prior.pid) is None)


@pytest.mark.parametrize('operation', ['up', 'down', 'restart', 'fresh'])
def test_explicit_lifecycle_timeout_preserves_the_launch(drydock, tmux, monkeypatch, capsys, operation):
    ship = shipmod.load(drydock)
    seat = seatmod.Seat(ship, ship.seat('worker-1'), tmux)
    seat.up(False)
    prior = seat.launched()
    wait_for(lambda: seat.harness.screen(tmux.capture(seat.pane().id)).working is False)
    monkeypatch.setenv('QD_SHIP', str(drydock))
    monkeypatch.setenv('QD_TMUX_SOCKET', tmux.socket)
    monkeypatch.delenv('QD_SEAT', raising=False)
    monkeypatch.setattr(lifecycle, 'WAIT_SECONDS', 0.1)
    with lifecycle.claim(ship.runtime, seat.name):
        assert cli.main([operation, seat.name]) == 2
    assert capsys.readouterr().err == (
        f'qd: {seat.name}: another lifecycle transition is in progress; retry when it ends\n')
    assert seat.launched() == prior and seat.pane().pid == prior.pid
    assert len(windows(tmux)) == 1
