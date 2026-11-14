"""Explicit lifecycle commands wait for a concurrent seat transition to finish."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from quarterdeck import cli, lifecycle, seat as seatmod, ship as shipmod
from .test_launch_lifecycle import process_identity, windows
from .waiting import wait_for


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
