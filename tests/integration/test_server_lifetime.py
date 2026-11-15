"""A launch saved in an earlier tmux server lifetime is gone; a mismatched pane in this lifetime still refuses."""
from __future__ import annotations

import json
import os
import time

import pytest

from quarterdeck import seat as seatmod, ship as shipmod
from quarterdeck.ship import ShipError
from .test_launch_lifecycle import process_identity, windows


def foreign_pane_with_id(tmux, wanted):
    """Open foreign windows on the restarted server until one carries the saved pane id."""
    tmux.new_session('Other')
    for _ in range(64):
        pane = tmux.run('new-window', '-d', '-P', '-F', '#{pane_id}\t#{pane_pid}', '-t', '=Other:',
                        'sleep 300').strip().split('\t')
        if pane[0] == wanted:
            return pane[0], int(pane[1])
    pytest.fail(f'the restarted server never reused {wanted}')


def restart_server_after(tmux, path):
    """End the private server, wait past the launch file's second, and let the next command start a new one."""
    tmux.kill_server()
    written = path.stat().st_mtime
    while int(time.time()) <= written:
        time.sleep(.02)


@pytest.mark.parametrize('operation', ['up', 'down', 'fresh'])
def test_a_launch_from_an_earlier_server_lifetime_is_gone(drydock, tmux, operation):
    ship = shipmod.load(drydock)
    seat = seatmod.Seat(ship, ship.seat('worker-1'), tmux)
    seat.up(False)
    old = seat.launched()
    path = seat.trusted / 'launch.json'
    restart_server_after(tmux, path)
    foreign, foreign_pid = foreign_pane_with_id(tmux, old.pane)
    assert foreign_pid != old.pid
    started = int(tmux.run('display-message', '-p', '#{start_time}').strip())
    assert started > path.stat().st_mtime, 'the new server began after the launch was written'
    before = windows(tmux)

    proof = getattr(seat, operation)() if operation != 'up' else seat.up(False)

    assert tmux.pane(foreign).pid == foreign_pid, 'the foreign pane that reused the id is untouched'
    assert process_identity(foreign_pid) is not None
    if operation == 'down':
        assert not path.exists() and windows(tmux) == before
        assert 'was not up' in proof
    else:
        new = seat.launched()
        assert new is not None and new.pane != foreign and tmux.pane(new.pane).pid == new.pid
        assert len(windows(tmux)) == len(before) + 1, 'one new launch window, the foreign window kept'
        seat.down()
        assert windows(tmux) == before and not path.exists()


@pytest.mark.parametrize('operation', ['up', 'down', 'fresh'])
def test_a_mismatched_pane_in_the_same_lifetime_still_refuses(drydock, tmux, monkeypatch, operation):
    """Even when the launch started its own server, so its `at` precedes the server's start."""
    ship = shipmod.load(drydock)
    seat = seatmod.Seat(ship, ship.seat('worker-1'), tmux)
    assert not tmux.running()
    opened = seat._open_window

    def late(*args, **kwargs):
        stamped = time.time()
        while int(time.time()) <= stamped:  # the server this launch starts begins in a later second than `at`
            time.sleep(.02)
        return opened(*args, **kwargs)
    monkeypatch.setattr(seat, '_open_window', late)
    seat.up(False)
    launch = seat.launched()
    started = int(tmux.run('display-message', '-p', '#{start_time}').strip())
    assert started > launch.when.timestamp(), 'a server-start-after-`at` rule would call this launch stale'
    tmux.run('respawn-pane', '-k', '-t', launch.pane, 'sleep 300')
    respawned = tmux.pane(launch.pane)
    assert respawned.pid != launch.pid
    path = seat.trusted / 'launch.json'
    retained, before = path.read_bytes(), windows(tmux)

    with pytest.raises(ShipError, match='no longer matches its launch PID'):
        getattr(seat, operation)() if operation != 'up' else seat.up(False)

    assert path.read_bytes() == retained and json.loads(retained)['pane'] == launch.pane
    assert windows(tmux) == before
    assert tmux.pane(launch.pane).pid == respawned.pid, 'the mismatched pane is kept as evidence'


def test_an_unreadable_server_start_keeps_the_refusal(drydock, tmux, monkeypatch):
    ship = shipmod.load(drydock)
    seat = seatmod.Seat(ship, ship.seat('worker-1'), tmux)
    seat.up(False)
    old = seat.launched()
    path = seat.trusted / 'launch.json'
    restart_server_after(tmux, path)
    foreign_pane_with_id(tmux, old.pane)
    monkeypatch.setattr(type(tmux), 'started', lambda self: None, raising=False)
    retained = path.read_bytes()
    with pytest.raises(ShipError, match='no longer matches its launch PID'):
        seat.up(False)
    assert path.read_bytes() == retained
