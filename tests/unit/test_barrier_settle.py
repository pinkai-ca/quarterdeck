"""qd service --settle-barrier: an unreadable stop barrier moved aside unchanged only under (a), (b) and (c).

Stand-ins only: a throwaway ship under tmp_path, a process that renames itself 'tmux: server' (never a real tmux), a
child carrying QD_SHIP in a private session, a child that holds the service's lock, a child that makes itself not
dumpable, and an injected cgroup reading. The real ship's barrier, service and tmux are never touched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import barrier_settle, servicectl, shutdown, stop_barrier
from quarterdeck.ship import Pool, SeatSpec, Session, Ship
from tests.unit.test_service_processes import _end

SLEEPER = 'import signal; print(1, flush=True); signal.pause()'
SERVER = r'''
import signal, subprocess, sys
with open('/proc/self/comm', 'w') as stream:
    stream.write('tmux: server')  # only its name: never a real tmux
pane = subprocess.Popen([sys.executable, '-c', 'import signal; signal.pause()'], stdin=subprocess.DEVNULL)
print(pane.pid, flush=True)
signal.pause()
'''
HOLDER = r'''
import fcntl, os, signal, sys
descriptor = os.open(sys.argv[1], os.O_RDWR)
fcntl.flock(descriptor, fcntl.LOCK_EX)
print(1, flush=True)
signal.pause()
'''
UNDUMPABLE = r'''
import ctypes, signal
ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE 0: its /proc environ is no longer this user's to read
print(1, flush=True)
signal.pause()
'''


def ship_at(tmp_path):
    folder = tmp_path / 'synthetic-ship'
    folder.mkdir()
    (folder / 'ship.toml').write_text('# synthetic\n')
    seats = {'ops': SeatSpec('ops', 'crew', 'systems', 'claude', ('local',)),
             'builder': SeatSpec('builder', 'crew', 'worker', 'claude', ('local',))}
    ship = Ship(folder, 'synthetic', 'operator', tmp_path, {'local': Pool('local', 'synthetic-endpoint')},
                {'crew': Session('crew', 'crew')}, seats)
    lock = servicectl.lock_path(ship.runtime)
    lock.parent.mkdir(parents=True)
    lock.write_text('')  # the service's lock, made and free, as a stopped service leaves it
    barrier = stop_barrier.path(ship.runtime)
    barrier.write_bytes(b'{"version": 1, "processes": [')  # unreadable: cut short
    return ship


def tmux(server=None, socket=None):
    """The ship's tmux, as the act reads it: its server's pid, or none at an absent socket."""
    return SimpleNamespace(server_pid=lambda: server, socket=str(socket) if socket else '/nonexistent/qd-test-socket')


def started(script, *args, env=None, cwd=None):
    child = subprocess.Popen([sys.executable, '-c', script, *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             text=True, start_new_session=True, env=env, cwd=cwd)
    first = child.stdout.readline().strip()
    child.start = shutdown.process_start(child.pid)
    return child, first


def comm(pid):
    return (Path('/proc') / str(pid) / 'comm').read_text().strip()


def ended(*children):
    for child in children:
        _end(child.pid, child.start)
        child.wait(timeout=20)


def scope_of(ship, members, unit=None):
    """An injected cgroup reading: the given pids in a scope of this ship's service, every other in none of it."""
    unit = unit or f'qd-service-{ship.name}-0123abcd.scope'
    return lambda pid: (f'/user.slice/user-1000.slice/user@1000.service/app.slice/{unit}' if pid in members
                        else '/user.slice/user-1000.slice/session-1.scope')


def log_lines(ship):
    path = ship.runtime / 'log' / 'service.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_with_all_three_holding_the_barrier_moves_aside_unchanged_and_is_logged(tmp_path):
    ship = ship_at(tmp_path)
    barrier = stop_barrier.path(ship.runtime)
    raw = barrier.read_bytes()
    server, pane = started(SERVER, env={**os.environ, 'QD_SHIP': str(ship.folder)})  # the ship's server and a pane
    pane = int(pane)
    try:
        reading = scope_of(ship, {server.pid, pane})  # a tmux server in the scope, with everything under it, is left out
        result = barrier_settle.settle(ship, tmux(server.pid), 'ops', cgroup_of=reading)
        assert result.startswith('service stop barrier moved aside unchanged: '), result
        [moved] = (ship.runtime / 'evidence' / 'stop-barrier').iterdir()
        assert moved.read_bytes() == raw and not barrier.exists()
        [line] = log_lines(ship)
        assert line['event'] == 'stop-barrier-settled' and line['moved'] == str(moved) and line['caller'] == 'ops'
        assert 'QD_SHIP=' not in json.dumps(line) and 'QD_SHIP=' not in result, 'no value is ever written'
        assert barrier_settle.settle(ship, tmux(server.pid), 'ops', cgroup_of=reading).startswith(
            'no service stop barrier'), 'nothing left to settle'
    finally:
        _end(pane, shutdown.process_start(pane))
        ended(server)


def test_a_barrier_that_is_a_link_moves_as_the_link(tmp_path):
    ship = ship_at(tmp_path)
    barrier = stop_barrier.path(ship.runtime)
    barrier.unlink()
    elsewhere = tmp_path / 'elsewhere.json'
    elsewhere.write_text('{"version": 1, "processes": [{"pid": 1, "start": "1"}]}')
    barrier.symlink_to(elsewhere)  # read as unreadable: opened without following a link
    result = barrier_settle.settle(ship, tmux(), None, cgroup_of=scope_of(ship, set()))
    assert result.startswith('service stop barrier moved aside unchanged'), result
    [moved] = (ship.runtime / 'evidence' / 'stop-barrier').iterdir()
    assert moved.is_symlink() and os.readlink(moved) == str(elsewhere) and elsewhere.is_file()


def test_a_held_lock_refuses_under_a(tmp_path):
    ship = ship_at(tmp_path)
    holder, _ = started(HOLDER, str(servicectl.lock_path(ship.runtime)))
    try:
        result = barrier_settle.settle(ship, tmux(), 'ops', cgroup_of=scope_of(ship, set()))
        assert result.startswith('refused: ') and "(a) the service's lock is held" in result, result
        assert stop_barrier.path(ship.runtime).exists() and not (ship.runtime / 'evidence').exists()
        assert log_lines(ship)[-1]['event'] == 'stop-barrier-settle-refused'
    finally:
        ended(holder)


def test_a_process_in_the_ships_service_scope_refuses_under_b_and_another_ships_does_not(tmp_path):
    ship = ship_at(tmp_path)
    member, _ = started(SLEEPER)
    try:
        result = barrier_settle.settle(ship, tmux(), 'ops', cgroup_of=scope_of(ship, {member.pid}))
        assert f"(b) in this ship's service scope: pid {member.pid} {comm(member.pid)}" in result, result
        assert f'qd-service-{ship.name}-0123abcd.scope' in result and stop_barrier.path(ship.runtime).exists()
        other = scope_of(ship, {member.pid}, unit='qd-service-synthetic-other-0123abcd.scope')
        assert barrier_settle.settle(ship, tmux(), 'ops', cgroup_of=other).startswith(
            'service stop barrier moved aside unchanged'), "another ship's scope is not this one's"
    finally:
        ended(member)


@pytest.mark.parametrize('form', ['absolute', 'relative'])
def test_a_qd_ship_carrier_outside_the_ships_tmux_server_refuses_under_c(tmp_path, form):
    ship = ship_at(tmp_path)
    value, cwd = (str(ship.folder), None) if form == 'absolute' else ('.', ship.folder)
    carrier, _ = started(SLEEPER, env={**os.environ, 'QD_SHIP': value}, cwd=cwd)  # its own session, outside the tree
    try:
        result = barrier_settle.settle(ship, tmux(), 'ops', cgroup_of=scope_of(ship, set()))
        assert f"(c) outside the ship's tmux server, QD_SHIP names this ship in: pid {carrier.pid} {comm(carrier.pid)}" in result
        assert 'QD_SHIP=' not in result
        if form == 'absolute':
            assert value not in result.replace(str(ship.runtime), ''), 'no value is shown'
        assert stop_barrier.path(ship.runtime).exists()
        assert 'QD_SHIP=' not in json.dumps(log_lines(ship)), 'the log names pids, never a value'
    finally:
        ended(carrier)


def test_another_ships_qd_ship_does_not_refuse(tmp_path):
    ship = ship_at(tmp_path)
    other = tmp_path / 'other-ship'
    other.mkdir()
    carrier, _ = started(SLEEPER, env={**os.environ, 'QD_SHIP': str(other)})
    try:
        assert barrier_settle.settle(ship, tmux(), 'ops', cgroup_of=scope_of(ship, set())).startswith(
            'service stop barrier moved aside unchanged')
    finally:
        ended(carrier)


def test_a_running_server_whose_pid_cannot_be_read_refuses_under_c(tmp_path, monkeypatch):
    from quarterdeck import jail_end
    ship = ship_at(tmp_path)
    monkeypatch.setattr(jail_end, 'server_state', lambda tmux: jail_end.LIVE)  # it answers at its socket
    result = barrier_settle.settle(ship, tmux(None), 'ops', cgroup_of=scope_of(ship, set()))
    assert "(c) the ship's tmux server runs but its pid cannot be read" in result, result
    assert stop_barrier.path(ship.runtime).exists()


def test_an_unreadable_environment_is_skipped_and_named_unless_it_sits_in_a_service_scope(tmp_path):
    """The engineer's terms: a process this user cannot read is skipped by (c) and named by pid and process name,
    in the output and the log; one that sits in any qd-service-* scope is not skipped, and refuses."""
    ship = ship_at(tmp_path)
    hidden, _ = started(UNDUMPABLE)
    try:
        with pytest.raises(PermissionError):
            (Path('/proc') / str(hidden.pid) / 'environ').read_bytes()
        result = barrier_settle.settle(ship, tmux(), 'ops', cgroup_of=scope_of(ship, set()))
        assert result.startswith('service stop barrier moved aside unchanged'), result
        assert f'pid {hidden.pid} {comm(hidden.pid)}' in result.split('skipped: ')[1]
        assert f'pid {hidden.pid} {comm(hidden.pid)}' in log_lines(ship)[-1]['environment_unreadable']
        stop_barrier.path(ship.runtime).write_text('{')  # unreadable again
        elsewhere = scope_of(ship, {hidden.pid}, unit='qd-service-another-0123abcd.scope')
        result = barrier_settle.settle(ship, tmux(), 'ops', cgroup_of=elsewhere)
        assert (f"(c) environment unreadable to this user, in a service's scope: pid {hidden.pid} {comm(hidden.pid)} in "
                'qd-service-another-0123abcd.scope') in result, result
        assert stop_barrier.path(ship.runtime).exists()
    finally:
        ended(hidden)


def test_a_readable_or_absent_barrier_is_not_moved(tmp_path):
    ship = ship_at(tmp_path)
    barrier = stop_barrier.path(ship.runtime)
    barrier.write_text('{"version": 1, "processes": [{"pid": 1, "start": "1"}]}')
    result = barrier_settle.settle(ship, tmux(), 'ops', cgroup_of=scope_of(ship, set()))
    assert result.startswith('refused: ') and 'reads cleanly' in result and barrier.exists()
    barrier.unlink()
    assert barrier_settle.settle(ship, tmux(), 'ops', cgroup_of=scope_of(ship, set())).startswith(
        'no service stop barrier')


def test_only_a_systems_seat_or_the_operator_may_settle(tmp_path):
    ship = ship_at(tmp_path)
    reading = scope_of(ship, set())
    assert barrier_settle.settle(ship, tmux(), 'builder', cgroup_of=reading) == (
        'refused: builder is a worker seat; only a systems seat or the operator settles the barrier')
    assert barrier_settle.settle(ship, tmux(), 'stranger', cgroup_of=reading).startswith(
        'refused: stranger is not a seat of synthetic')
    assert stop_barrier.path(ship.runtime).exists() and not log_lines(ship)
    assert barrier_settle.settle(ship, tmux(), None, cgroup_of=reading).startswith(
        'service stop barrier moved aside unchanged'), "the operator's own shell"


def test_the_act_is_run_by_the_ships_own_copy_while_the_service_and_controls_by_harness_are_not(tmp_path, monkeypatch):
    """qd service runs the service itself (it bootstraps its own copy), and qd controls --harness reads no ship; the
    settling act is an ordinary act, handed to the ship's installed copy like any other."""
    from quarterdeck import cli
    handed = []
    monkeypatch.setattr(cli, 'handoff', lambda args, argv: handed.append(list(argv)))
    monkeypatch.setattr(cli, 'cmd_service', lambda args: None)
    monkeypatch.setattr(cli, 'cmd_controls', lambda args: None)
    folder = str(tmp_path)
    for argv, expected in ((['--ship', folder, 'service', '--settle-barrier'], True),
                           (['--ship', folder, 'service'], False),
                           (['--ship', folder, 'controls', '--harness', 'fake'], False)):
        handed.clear()
        assert cli.main(argv) == 0
        assert bool(handed) is expected, argv
