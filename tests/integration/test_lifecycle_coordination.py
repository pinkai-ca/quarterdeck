"""Independent lifecycle callers must not forget each other's live launches."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from quarterdeck import seat as seatmod, ship as shipmod
from quarterdeck.ship import ShipError
from quarterdeck.tmux import Tmux
from .test_launch_lifecycle import process_identity, windows
from .test_seat import wait_for


def peer_call(ship, tmux, operation, process):
    """A fresh caller shares only the declared runtime and private transport binding."""
    if not process:
        peer = seatmod.Seat(shipmod.load(ship.folder), ship.seat('worker-1'), Tmux(tmux.socket))
        try:
            result = getattr(peer, operation)(False) if operation == 'up' else getattr(peer, operation)()
            return {'outcome': 'success', 'result': result}
        except ShipError as error:
            return {'outcome': 'refused', 'reason': str(error)}
    script = '''import json,sys
from quarterdeck.ship import load,ShipError
from quarterdeck.seat import Seat
from quarterdeck.tmux import Tmux
ship=load(sys.argv[1]); seat=Seat(ship,ship.seat('worker-1'),Tmux(sys.argv[2]))
try:
 result=seat.up(False) if sys.argv[3]=='up' else getattr(seat,sys.argv[3])()
 outcome=dict(outcome='success',result=result)
except ShipError as error:
 outcome=dict(outcome='refused',reason=str(error))
print(json.dumps(outcome))
'''
    done = subprocess.run([sys.executable, '-c', script, str(ship.folder), tmux.socket, operation],
                          capture_output=True, text=True, timeout=10)
    assert done.returncode == 0, done.stdout + done.stderr
    return json.loads(done.stdout)


@pytest.mark.parametrize('process', [False, True], ids=['independent-seat', 'independent-process'])
def test_overlapping_startup_refuses_without_forgotten_live_window(drydock, tmux, monkeypatch, process):
    ship = shipmod.load(drydock)
    outer = seatmod.Seat(ship, ship.seat('worker-1'), tmux)
    tmux.new_session('Crew', env={'QD_SHIP': str(ship.folder)})  # creator starts transport before observer callers
    original = outer.harness.build_home
    entered = False
    outcomes = []

    def overlap(*args, **kwargs):
        nonlocal entered
        result = original(*args, **kwargs)
        if not entered:
            entered = True
            outcomes.append(peer_call(ship, tmux, 'up', process))
        return result

    monkeypatch.setattr(outer.harness, 'build_home', overlap)
    outer.up(False)
    panes = tmux.panes('Crew')
    saved = outer.launched()
    print('STARTUP_COORDINATION=' + json.dumps(dict(peer=outcomes, windows=len(windows(tmux)),
          live_pids=[p.pid for p in panes if not p.dead], saved_pane=saved.pane)))
    assert len(outcomes) == 1 and outcomes[0]['outcome'] == 'refused'
    assert 'lifecycle' in outcomes[0]['reason']
    assert len(panes) == len(windows(tmux)) == 1
    assert panes[0].id == saved.pane and panes[0].pid == saved.pid and not panes[0].dead


def test_start_during_retirement_cannot_replace_launch_before_unlink(drydock, tmux, monkeypatch):
    ship = shipmod.load(drydock)
    outer = seatmod.Seat(ship, ship.seat('worker-1'), tmux)
    outer.up(False)
    prior = outer.launched()
    original = tmux.kill_window
    outcomes = []

    def overlap(ident, *, expected_pid=None):
        original(ident, expected_pid=expected_pid)
        outcomes.append(peer_call(ship, tmux, 'up', True))

    monkeypatch.setattr(tmux, 'kill_window', overlap)
    error = None
    try:
        outer.down()
    except ShipError as caught:
        error = str(caught)
    print('RETIREMENT_COORDINATION=' + json.dumps(dict(peer=outcomes, outer_error=error,
          windows=len(windows(tmux)), launch_present=outer.launched() is not None)))
    assert len(outcomes) == 1 and outcomes[0]['outcome'] == 'refused'
    assert 'lifecycle' in outcomes[0]['reason']
    assert error is None
    assert outer.launched() is None and windows(tmux) == []
    wait_for(lambda: process_identity(prior.pid) is None)


@pytest.mark.parametrize('operation', ['fresh', 'restart'])
def test_relaunch_owns_stop_and_start_as_one_transition(drydock, tmux, monkeypatch, operation):
    ship = shipmod.load(drydock)
    outer = seatmod.Seat(ship, ship.seat('worker-1'), tmux)
    outer.up(False)
    prior = outer.launched()
    original = outer.harness.build_home
    outcomes = []

    def overlap(*args, **kwargs):
        result = original(*args, **kwargs)
        outcomes.append(peer_call(ship, tmux, 'down', True))
        return result

    monkeypatch.setattr(outer.harness, 'build_home', overlap)
    getattr(outer, operation)()
    assert len(outcomes) == 1 and outcomes[0]['outcome'] == 'refused'
    assert 'lifecycle' in outcomes[0]['reason']
    wait_for(lambda: process_identity(prior.pid) is None)
    assert len(windows(tmux)) == 1 and outer.pane().pid == outer.launched().pid


def test_failed_preparation_releases_coordination_for_next_caller(drydock, tmux, monkeypatch):
    ship = shipmod.load(drydock)
    outer = seatmod.Seat(ship, ship.seat('worker-1'), tmux)
    original = outer.harness.build_home
    def refuse(*args, **kwargs):
        raise ShipError('synthetic preparation failure')
    monkeypatch.setattr(outer.harness, 'build_home', refuse)
    with pytest.raises(ShipError, match='synthetic preparation failure'):
        outer.up(False)
    assert windows(tmux) == [] and outer.launched() is None
    monkeypatch.setattr(outer.harness, 'build_home', original)
    tmux.new_session('Crew', env={'QD_SHIP': str(ship.folder)})
    result = peer_call(ship, tmux, 'up', True)
    assert result['outcome'] == 'success'
    assert len(windows(tmux)) == 1


def test_different_seat_can_start_during_another_seat_preparation(drydock, tmux, monkeypatch):
    ship = shipmod.load(drydock)
    outer = seatmod.Seat(ship, ship.seat('worker-1'), tmux)
    other = seatmod.Seat(ship, ship.seat('captain'), tmux)
    original = outer.harness.build_home
    entered = False
    def overlap(*args, **kwargs):
        nonlocal entered
        result = original(*args, **kwargs)
        if not entered:
            entered = True
            other.up(False)
        return result
    monkeypatch.setattr(outer.harness, 'build_home', overlap)
    outer.up(False)
    assert len(windows(tmux)) == 2
    assert outer.pane().pid == outer.launched().pid
    assert other.pane().pid == other.launched().pid


def test_failover_owns_selection_and_start_as_one_transition(drydock, tmux, monkeypatch):
    ship = shipmod.load(drydock)
    outer = seatmod.Seat(ship, ship.seat('worker-1'), tmux)
    outer.up(False)
    prior = outer.launched()
    original = outer.move_to
    outcomes = []
    def overlap(pool):
        original(pool)
        outcomes.append(peer_call(ship, tmux, 'up', True))
    monkeypatch.setattr(outer, 'move_to', overlap)
    outer.failover_to('fake')
    assert len(outcomes) == 1 and outcomes[0]['outcome'] == 'refused'
    assert 'lifecycle' in outcomes[0]['reason']
    wait_for(lambda: process_identity(prior.pid) is None)
    assert outer.pool.name == 'fake'
    assert len(windows(tmux)) == 1 and outer.pane().pid == outer.launched().pid


def test_process_exit_releases_lease_without_removing_lock_inode(drydock, tmux):
    import select
    ship = shipmod.load(drydock)
    outer = seatmod.Seat(ship, ship.seat('worker-1'), tmux)
    script = '''import os,sys
from quarterdeck.ship import load
from quarterdeck.seat import Seat
from quarterdeck.tmux import Tmux
ship=load(sys.argv[1]); seat=Seat(ship,ship.seat('worker-1'),Tmux(sys.argv[2]))
with seat._lifecycle():
 print('LEASE_HELD',flush=True)
 sys.stdin.read(1)
 os._exit(0)
'''
    child = subprocess.Popen([sys.executable, '-c', script, str(ship.folder), tmux.socket],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert select.select([child.stdout], [], [], 10)[0], 'owned lease process did not become ready'
        assert child.stdout.readline().strip() == 'LEASE_HELD'
        path = ship.runtime / 'post/lifecycle/worker-1.lock'
        before = path.stat()
        with pytest.raises(ShipError, match='lifecycle'):
            outer.up(False)
        assert windows(tmux) == [] and outer.launched() is None
        child.stdin.write('x')
        child.stdin.flush()
        assert child.wait(timeout=10) == 0
        assert (path.stat().st_dev, path.stat().st_ino) == (before.st_dev, before.st_ino)
        outer.up(False)
        assert len(windows(tmux)) == 1
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


@pytest.mark.parametrize('kind', ['fifo', 'symlink'])
def test_unfamiliar_coordination_file_is_refused_and_retained(drydock, tmux, kind):
    import os
    ship = shipmod.load(drydock)
    path = ship.runtime / 'post/lifecycle/worker-1.lock'
    path.parent.mkdir(parents=True)
    if kind == 'fifo':
        os.mkfifo(path)
    else:
        target = path.parent / 'owned-peer'
        target.write_text('owned peer evidence')
        path.symlink_to(target)
    before = path.lstat()
    # A separate owned process bounds a malformed-file open independently of pytest.
    outcome = peer_call(ship, tmux, 'up', True)
    assert outcome['outcome'] == 'refused' and 'lifecycle' in outcome['reason']
    after = path.lstat()
    assert (after.st_dev, after.st_ino, after.st_mode) == (before.st_dev, before.st_ino, before.st_mode)
    assert windows(tmux) == []
    if kind == 'symlink':
        assert target.read_text() == 'owned peer evidence'
