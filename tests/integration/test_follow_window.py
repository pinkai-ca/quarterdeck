"""Showing a working seat's window on an owned private server: real reads, real switches, no resize."""
from __future__ import annotations

from dataclasses import replace
import shutil
import subprocess

import pytest

from quarterdeck import seat as seatmod, service, ship as shipmod
from quarterdeck.tmux import TmuxError
from .test_seat import wait_for


@pytest.fixture
def crew(drydock, tmux, monkeypatch):
    monkeypatch.setattr(service.machine, 'processes', lambda: {})
    ship = shipmod.load(drydock)
    ship.seats['worker-2'] = replace(ship.seat('worker-1'), name='worker-2')
    seats = {name: seatmod.Seat(ship, ship.seat(name), tmux) for name in ('worker-1', 'worker-2')}
    for seat in seats.values():
        seat.up(False)
    svc = service.Service(ship, tmux)
    try:
        yield svc, seats
    finally:
        svc.ledger.close()


def shown(tmux):
    view = tmux.session_view('Crew')
    return view.active, dict(view.windows)


def other(svc, seats, tmux):
    """The crew seat whose window the session is not showing."""
    view = tmux.session_view('Crew')
    return next(seat for seat in seats.values() if view.pane_windows[seat.pane().id] != view.active)


def sizes(windows):
    return {ident: size[1:] for ident, size in windows.items()}


def test_with_no_client_the_working_seat_is_shown_and_nothing_resized(crew, tmux):
    svc, seats = crew
    view = tmux.session_view('Crew')
    assert set(view.pane_windows) >= {seat.pane().id for seat in seats.values()} and view.inputs == ()
    seat = other(svc, seats, tmux)
    before, windows = shown(tmux)
    assert svc.show_window(seat) is None
    after, now = shown(tmux)
    assert after == tmux.session_view('Crew').pane_windows[seat.pane().id] != before
    assert sizes(now) == sizes(windows)


def test_a_pane_in_copy_mode_refuses(crew, tmux):
    svc, seats = crew
    seat = other(svc, seats, tmux)
    shown_pane = next(s for s in seats.values() if s is not seat).pane()
    tmux.run('copy-mode', '-t', shown_pane.id)
    assert tmux.session_view('Crew').scrolling
    before = shown(tmux)
    assert svc.show_window(seat) == 'a human is scrolling in the session'
    assert shown(tmux) == before


def test_another_process_in_the_pane_refuses_in_the_server(crew, tmux):
    svc, seats = crew
    seat = other(svc, seats, tmux)
    pane = seat.pane()
    before = shown(tmux)
    with pytest.raises(TmuxError, match='changed process identity'):
        tmux.select_window(pane.id, expected_pid=pane.pid + 1)
    assert shown(tmux) == before


@pytest.mark.skipif(shutil.which('script') is None, reason='no pty helper to attach a client')
def test_an_attached_client_typing_or_a_different_size_refuses_and_an_equal_size_switches(crew, tmux, monkeypatch):
    svc, seats = crew
    attach = f"stty cols 80 rows 24; exec tmux -S {tmux.socket} -N attach -t =Crew:"
    client = subprocess.Popen(['script', '-qfc', attach, '/dev/null'], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_for(lambda: tmux.session_view('Crew').inputs)
        seat = other(svc, seats, tmux)
        before, windows = shown(tmux)
        assert svc.show_window(seat) == 'a human is typing in the session', 'attaching is input'
        monkeypatch.setattr(service, 'TYPING_QUIET', 0)
        target = tmux.session_view('Crew').pane_windows[seat.pane().id]
        assert windows[target][1:] != windows[before][1:], 'the client fitted only the window it shows'
        assert svc.show_window(seat) == 'showing it would resize its window'
        assert shown(tmux) == (before, windows), 'nothing switched and nothing resized'
        width, height = windows[before][1:]
        tmux.run('resize-window', '-t', target, '-x', str(width), '-y', str(height))  # the test's own setup
        _, windows = shown(tmux)
        assert svc.show_window(seat) is None
        after, now = shown(tmux)
        assert after == target and sizes(now) == sizes(windows)
    finally:
        client.terminate()
        client.wait(timeout=5)
