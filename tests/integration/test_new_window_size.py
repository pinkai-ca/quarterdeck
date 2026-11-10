"""A seat's window opens at the size its session shows when a terminal is attached there; otherwise as before."""
from __future__ import annotations

from dataclasses import replace
import shutil
import subprocess

import pytest

from quarterdeck import seat as seatmod, service, ship as shipmod
from .waiting import wait_for

pytestmark = pytest.mark.skipif(shutil.which('script') is None, reason='no pty helper to attach a client')


def attach(tmux, session, cols, rows):
    """A real terminal of the given size, attached to one session, for the test's lifetime."""
    command = f"stty cols {cols} rows {rows}; exec tmux -S {tmux.socket} -N attach -t ={session}:"
    client = subprocess.Popen(['script', '-qfc', command, '/dev/null'], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wait_for(lambda: f'{session}\t{cols}x' in tmux.run('list-clients', '-F', '#{client_session}\t#{client_width}x#{client_height}'))
    return client


def window_size(tmux, pane_id):
    width, height, local = tmux.run('display-message', '-p', '-t', pane_id,
                                    '#{window_width}\t#{window_height}\t#{window-size}').rstrip('\n').split('\t')
    return int(width), int(height)


@pytest.fixture
def ship_with_terminals(drydock, tmux, monkeypatch):
    monkeypatch.setattr(service.machine, 'processes', lambda: {})
    ship = shipmod.load(drydock)
    ship.seats['worker-2'] = replace(ship.seat('worker-1'), name='worker-2')
    seats = {name: seatmod.Seat(ship, ship.seat(name), tmux) for name in ('worker-1', 'worker-2', 'captain')}
    clients = []
    svc = service.Service(ship, tmux)
    try:
        yield ship, seats, clients, svc
    finally:
        svc.ledger.close()
        for client in clients:
            client.terminate()
            client.wait(timeout=5)


def test_a_window_opens_at_its_watched_sessions_size_and_follow_needs_no_resize(ship_with_terminals, tmux, monkeypatch):
    ship, seats, clients, svc = ship_with_terminals
    seats['worker-1'].up(False)  # creates the Crew session
    seats['captain'].up(False)  # creates the Bridge session
    clients.append(attach(tmux, 'Crew', 100, 30))
    clients.append(attach(tmux, 'Bridge', 70, 20))  # the most recently active terminal is another session's
    shown = tmux.session_view('Crew')
    shown_size = shown.windows[shown.active][1:]
    assert shown_size == (100, 29)

    seats['worker-2'].up(False)
    pane = seats['worker-2'].pane()
    assert (pane.width, pane.height) == shown_size, 'the program started in a window of the watched size'
    assert window_size(tmux, pane.id) == shown_size
    assert tmux.run('show-options', '-wv', '-t', pane.id, 'window-size').strip() == '', 'not pinned: the window keeps tmux sizing'

    monkeypatch.setattr(service, 'TYPING_QUIET', 0)
    before = {ident: size[1:] for ident, size in tmux.session_view('Crew').windows.items()}
    assert svc.show_window(seats['worker-2']) is None, 'follow now succeeds'
    after = tmux.session_view('Crew')
    assert after.pane_windows[pane.id] == after.active
    assert {ident: size[1:] for ident, size in after.windows.items()} == before, 'and nothing was resized'


def test_with_no_terminal_on_its_session_a_window_opens_as_before(ship_with_terminals, tmux):
    ship, seats, clients, svc = ship_with_terminals
    seats['worker-1'].up(False)
    seats['captain'].up(False)
    clients.append(attach(tmux, 'Bridge', 70, 20))  # a terminal only on another session
    assert tmux.session_view('Crew').inputs == ()
    seats['worker-2'].up(False)
    pane = seats['worker-2'].pane()
    assert window_size(tmux, pane.id) == (70, 19), "tmux's own choice, the latest terminal, unchanged"
    assert tmux.run('show-options', '-wv', '-t', pane.id, 'window-size').strip() == ''


def test_several_terminals_on_the_session_give_the_size_it_shows(ship_with_terminals, tmux, monkeypatch):
    """tmux shows a session's window at the size of its most recent viewer; the new window takes that size."""
    ship, seats, clients, svc = ship_with_terminals
    seats['worker-1'].up(False)
    seats['captain'].up(False)
    clients.append(attach(tmux, 'Crew', 100, 30))
    clients.append(attach(tmux, 'Crew', 90, 25))
    clients.append(attach(tmux, 'Bridge', 70, 20))
    shown = tmux.session_view('Crew')
    shown_size = shown.windows[shown.active][1:]
    assert len(shown.inputs) == 2 and shown_size == (90, 24)
    seats['worker-2'].up(False)
    pane = seats['worker-2'].pane()
    assert (pane.width, pane.height) == shown_size
    monkeypatch.setattr(service, 'TYPING_QUIET', 0)
    before = {ident: size[1:] for ident, size in tmux.session_view('Crew').windows.items()}
    assert svc.show_window(seats['worker-2']) is None
    assert {ident: size[1:] for ident, size in tmux.session_view('Crew').windows.items()} == before


def test_an_uncertain_session_reading_sizes_nothing(ship_with_terminals, tmux, monkeypatch):
    from quarterdeck.tmux import PrivateTmuxError
    ship, seats, clients, svc = ship_with_terminals
    seats['worker-1'].up(False)
    clients.append(attach(tmux, 'Crew', 100, 30))
    monkeypatch.setattr(type(tmux), 'session_view', lambda self, session: (_ for _ in ()).throw(PrivateTmuxError('private namespace busy')))
    seats['worker-2'].up(False)
    pane = seats['worker-2'].pane()
    assert tmux.pane(pane.id).pid == pane.pid, 'the seat still comes up, sized by tmux as before'
