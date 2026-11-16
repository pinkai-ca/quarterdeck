"""A seat's stop ends its whole sandbox; what does not end holds the seat; the relay reaps what it adopts; the sweep
ends a stray sandbox and never a live or starting one.

Real processes, all this test's own: the drydock's fake program on its private tmux server, the relay with a child
that leaves an orphan behind, and throwaway jails of the drydock's seat worker-1. tmux runs only on private servers
(the drydock's own, or -S in a short folder of the test's own), and only those are stopped.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import credential, jail_end, sandbox, seat as seatmod, service, ship as shipmod, trusted_files, \
    watch, write_bound

from .test_write_bound import jail_for, jailed
from .waiting import wait_for

SRC = Path(jail_end.__file__).resolve().parent.parent
READY = "import sys, time; open(sys.argv[1] + '/ready', 'w').close(); time.sleep(600)"


def sleeper():
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])
    return process, {process.pid: write_bound._processes()[process.pid][1]}


@pytest.fixture
def seat_world(drydock: Path, tmux, monkeypatch):
    ship = shipmod.load(drydock)
    seat = seatmod.Seat(ship, ship.seat('captain'), tmux)
    monkeypatch.setattr(credential, 'reseed', lambda ship, seats, **bounds: [])
    return SimpleNamespace(ship=ship, seat=seat)


# -- the stop and its hold --------------------------------------------------------------------------------------------

def test_a_stop_whose_sandbox_does_not_end_holds_the_seat_on_every_path_until_it_has(seat_world, monkeypatch):
    w = seat_world
    w.seat.up(resume=False)
    launched = w.seat.launched()
    process, pids = sleeper()
    try:
        monkeypatch.setattr(jail_end, 'end', lambda seat, tree: dict(pids))  # what the kill could not end
        proof = w.seat.down()
        expected = 'its old sandbox did not end (1 process still running)'
        assert expected in proof, proof
        assert w.seat.launched() == launched, 'the launch stays while it is held'
        words = w.seat.relaunch_refused()
        assert words.startswith('captain: ' + expected)
        with pytest.raises(shipmod.ShipError, match=r'\(1 process still running\)'):
            w.seat.up(resume=False)
        with pytest.raises(shipmod.ShipError, match=r'\(1 process still running\)'):
            w.seat.fresh()  # every relaunch path meets the one hold in up: nothing is launched beside it
        assert not w.seat.is_up and w.seat.launched() == launched
        facts = watch.facts(w.ship, w.seat.tmux)
        assert words in facts.ending and words in facts.line()
        monkeypatch.setattr(write_bound, 'kill', lambda pids, rounds=10: [])
        assert w.seat.finish_retirement() is None and jail_end.held(w.seat.trusted) is not None, 'held while it runs'
        monkeypatch.undo()
        monkeypatch.setattr(credential, 'reseed', lambda ship, seats, **bounds: [])
        proof = w.seat.finish_retirement()
        assert proof == 'captain retired · its old sandbox has ended · credential reseeded, launch removed'
        assert jail_end.held(w.seat.trusted) is None and w.seat.launched() is None
        assert write_bound.alive(pids) == {}
        w.seat.up(resume=False)
        assert w.seat.is_up
    finally:
        process.kill()
        process.wait()
        if w.seat.is_up:
            w.seat.down()


def test_the_tree_is_read_before_the_window_is_killed_and_ended_after_the_program(seat_world, monkeypatch):
    w = seat_world
    w.seat.up(resume=False)
    order = []
    real_snapshot, real_retire, real_end = jail_end.snapshot, w.seat._retire_window, jail_end.end
    monkeypatch.setattr(jail_end, 'snapshot', lambda launched: order.append('tree') or real_snapshot(launched))
    monkeypatch.setattr(w.seat, '_retire_window', lambda launched: order.append('window') or real_retire(launched))
    monkeypatch.setattr(jail_end, 'end', lambda seat, tree: order.append(('end', len(tree))) or real_end(seat, tree))
    proof = w.seat.down()
    assert proof.endswith('process ended'), proof
    assert order[:2] == ['tree', 'window'] and order[2][0] == 'end' and order[2][1] >= 1
    assert not (w.seat.trusted / trusted_files.JAIL_STOPPING).exists(), 'the marker goes with the stop'


# -- the relay --------------------------------------------------------------------------------------------------------

LEAVES_AN_ORPHAN = r'''
import os, sys, time
middle = os.fork()
if middle == 0:
    if os.fork() == 0:
        with open(sys.argv[1] + '.tmp', 'w') as out:
            out.write(str(os.getpid()))
        os.rename(sys.argv[1] + '.tmp', sys.argv[1])
        time.sleep(600)
        os._exit(0)
    os._exit(0)  # the orphan's parent ends: it goes to the nearest subreaper, which is the relay
os.waitpid(middle, 0)
time.sleep(float(sys.argv[2]))
sys.exit(3)
'''


def relay_words(folder: Path, pidfile: Path, seconds: float) -> list[str]:
    return ['env', f'PYTHONPATH={SRC}', sys.executable, '-m', 'quarterdeck.endpoint_relay', str(folder / 'r'),
            '127.0.0.1', '9', '', '--', sys.executable, '-c', LEAVES_AN_ORPHAN, str(pidfile), str(seconds)]


def orphan_of(pidfile: Path) -> dict[int, str]:
    pid = int(pidfile.read_text())
    return {pid: write_bound._processes()[pid][1]}


@pytest.fixture
def short():
    folder = Path(tempfile.mkdtemp(prefix='qdjt-', dir='/tmp'))  # short: a relay's socket fits the Unix path limit
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


def test_the_relay_adopts_its_jails_orphan_ends_it_before_it_exits_and_keeps_its_childs_status(short):
    pidfile = short / 'orphan'
    relay = subprocess.Popen(relay_words(short, pidfile, 1.0))
    try:
        assert wait_for(pidfile.exists, 30)
        orphan = orphan_of(pidfile)
        assert jail_end._stat(next(iter(orphan)))[0] == relay.pid, 'the relay is the orphan\'s subreaper'
        assert relay.wait(timeout=60) == 3, "the relay's exit status is its child's"
        assert write_bound.alive(orphan) == {}, 'ended and reaped before the relay exited'
    finally:
        if relay.poll() is None:
            relay.kill()
            relay.wait()


def test_a_relay_ended_by_its_panes_hangup_ends_and_reaps_what_it_adopted(short):
    pidfile, socket = short / 'orphan', short / 's'
    env = {key: value for key, value in os.environ.items() if key not in ('TMUX', 'TMUX_PANE')}
    tmux = lambda *args: subprocess.run(['tmux', '-S', str(socket), *args], env=env, capture_output=True, text=True,
                                        timeout=30)
    script = short / 'launch.sh'
    script.write_text(f'#!/bin/sh\nexec {shlex.join(relay_words(short, pidfile, 600))}\n')
    assert tmux('new-session', '-d', '-s', 'relay', f'sh {shlex.quote(str(script))}').returncode == 0
    try:
        assert wait_for(pidfile.exists, 30)
        orphan = orphan_of(pidfile)
        pane_pid = int(tmux('list-panes', '-t', 'relay', '-F', '#{pane_pid}').stdout.split()[0])
        relay = {pane_pid: write_bound._processes()[pane_pid][1]}
        tmux('kill-window', '-t', 'relay')
        assert wait_for(lambda: not write_bound.alive(relay), 30), 'the relay ended at its hangup'
        assert write_bound.alive(orphan) == {}, 'and it ended what it adopted before it did'
    finally:
        tmux('kill-server')
        if pidfile.exists():
            pid = int(pidfile.read_text())
            if (left := write_bound._processes().get(pid)) is not None:
                write_bound.kill({pid: left[1]})


# -- the sweep on throwaway jails ------------------------------------------------------------------------------------

@pytest.fixture
def ship(drydock: Path) -> shipmod.Ship:
    return shipmod.load(drydock)


def argv_of(ship, workspace: Path) -> list[str]:
    workspace.mkdir(exist_ok=True)
    return sandbox.command(jail_for(ship, workspace), ['/usr/bin/python3', '-c', READY, str(workspace)])


def orphaned_jail(ship, workspace: Path) -> tuple[int, str]:
    """A throwaway jail whose launcher has ended: no pane and no launch above it, as a stop's survivor is."""
    words = argv_of(ship, workspace)
    subprocess.run([sys.executable, '-c', 'import os, sys\nif os.fork() == 0:\n    os.setsid()\n'
                    '    os.execvp(sys.argv[1], sys.argv[1:])\nos._exit(0)', *words], check=True)
    assert wait_for(lambda: (workspace / 'ready').exists(), 60), 'the jailed program runs'
    found, _ = jail_end.orphans(ship.runtime, set(), seat='worker-1')
    assert len(found) == 1
    aged(found[0][2])
    return found[0][1], found[0][2]


def aged(start: str) -> None:
    """Past the sweep's margin: started more than a second before the roots are read."""
    assert wait_for(lambda: jail_end.ticks_now() - int(start) > 2 * os.sysconf('SC_CLK_TCK'), 30)


_SERVER = []


def stand_in_server() -> int:
    """A live process no jail descends from, standing in for the ship's tmux server's pid in an injected server."""
    if not _SERVER:
        import atexit
        process = subprocess.Popen(['sleep', '3600'])
        atexit.register(process.kill)
        _SERVER.append(process.pid)
    return _SERVER[0]


def sweeper(ship, panes=()):
    logs = []
    return SimpleNamespace(ship=ship, tmux=SimpleNamespace(panes=lambda: [SimpleNamespace(pid=pid) for pid in panes],
                                                           server_pid=stand_in_server),
                           log=lambda event, **data: logs.append((event, data)), logs=logs)


def cleanup(ship):
    found, _ = jail_end.orphans(ship.runtime, set())
    for _, pid, start in found:
        write_bound.kill(write_bound.tree(pid, start))


@jailed
def test_the_sweep_ends_a_down_seats_stray_sandbox_and_says_what_it_killed(ship, tmp_path):
    try:
        pid, start = orphaned_jail(ship, tmp_path / 'work')
        assert not (trusted_files.folder(ship.runtime, 'worker-1') / trusted_files.LAUNCH).exists(), 'a seat down'
        svc = sweeper(ship)
        [record] = jail_end.sweep(svc)
        assert (record['seat'], record['pid'], record['start'], record['confirmed']) == ('worker-1', pid, start, True)
        assert record['program_running'] and len(record['killed']) >= 2 and record['ran_seconds'] >= 0
        assert write_bound.alive({p: s for p, s in write_bound._processes().items() if p in record['killed']}) == {}
        assert jail_end.orphans(ship.runtime, set(), seat='worker-1')[0] == []
        [line] = jail_end.status(ship)
        assert line.startswith(f'worker-1: a stray sandbox was ended at ') and f'pid {pid}, start {start}' in line
        assert list((ship.runtime / 'evidence' / jail_end.EVIDENCE).glob('worker-1-*.json'))
        assert [event for event, data in svc.logs] == ['jail-swept']
    finally:
        cleanup(ship)


@jailed
def test_the_sweep_never_takes_a_jail_under_a_pane_starting_or_between_relaunch_steps(ship, tmp_path):
    jail = subprocess.Popen(argv_of(ship, tmp_path / 'work'))
    try:
        assert wait_for(lambda: jail_end.orphans(ship.runtime, set(), seat='worker-1')[0], 60)
        # The real shape: the pane's own process is the outer bwrap, as launch.sh execs it; and once more with the
        # pane a step above it, as a relay is.
        assert jail_end.sweep(sweeper(ship, panes=[jail.pid])) == [] and jail.poll() is None
        assert jail_end.sweep(sweeper(ship, panes=[os.getpid()])) == [] and jail.poll() is None
    finally:
        jail.kill()
        jail.wait()
        cleanup(ship)


@jailed
def test_the_sweep_never_takes_a_seats_live_launch(ship, tmp_path):
    jail = subprocess.Popen(argv_of(ship, tmp_path / 'work'))
    try:
        trusted = trusted_files.make(ship.runtime, 'worker-1')
        assert wait_for(lambda: jail_end.orphans(ship.runtime, set(), seat='worker-1')[0], 60)
        (trusted / trusted_files.LAUNCH).write_text(json.dumps(dict(  # the launched program is the outer bwrap
            pid=jail.pid, start=write_bound._processes()[jail.pid][1])))
        assert jail_end.sweep(sweeper(ship)) == [] and jail.poll() is None
    finally:
        jail.kill()
        jail.wait()
        cleanup(ship)


@jailed
def test_the_sweep_never_takes_another_ships_or_a_test_runs_throwaway(ship, tmp_path, drydock):
    other_folder = tmp_path / 'other' / 'ships' / 'drydock'
    shutil.copytree(drydock, other_folder, ignore=shutil.ignore_patterns('runtime'))
    other = shipmod.load(other_folder)
    try:
        pid, start = orphaned_jail(other, tmp_path / 'work')
        assert jail_end.sweep(sweeper(ship)) == []
        assert write_bound.alive({pid: start}), 'another ship\'s jail binds no folder of this ship'
    finally:
        cleanup(other)


@jailed
@pytest.mark.parametrize('marker', ['live', 'stale'])
def test_the_sweep_leaves_a_seat_to_its_own_stop_while_that_stop_runs(ship, tmp_path, marker):
    try:
        pid, start = orphaned_jail(ship, tmp_path / 'work')
        trusted = trusted_files.make(ship.runtime, 'worker-1')
        if marker == 'live':
            with jail_end.stopping(trusted):
                assert jail_end.sweep(sweeper(ship)) == []
            assert write_bound.alive({pid: start})
        else:
            ended = subprocess.run([sys.executable, '-c', 'import os; print(os.getpid())'], capture_output=True,
                                   text=True).stdout.strip()
            (trusted / trusted_files.JAIL_STOPPING).write_text(json.dumps(dict(pid=int(ended), start='1')))
            [record] = jail_end.sweep(sweeper(ship))
            assert record['pid'] == pid, 'a marker whose stop has ended leaves nothing out'
    finally:
        cleanup(ship)


@jailed
def test_the_sweep_leaves_a_held_seats_processes_to_its_hold(ship, tmp_path):
    try:
        pid, start = orphaned_jail(ship, tmp_path / 'work')
        trusted = trusted_files.make(ship.runtime, 'worker-1')
        jail_end.hold(trusted, 'worker-1', {pid: start})
        assert jail_end.sweep(sweeper(ship)) == []
        assert write_bound.alive({pid: start})
    finally:
        cleanup(ship)


@jailed
def test_the_sweep_reads_pid_and_start_again_before_the_kill(ship, tmp_path, monkeypatch):
    try:
        pid, start = orphaned_jail(ship, tmp_path / 'work')
        real = write_bound.alive
        monkeypatch.setattr(write_bound, 'alive', lambda pids: {} if set(pids) == {pid} else real(pids))
        assert jail_end.sweep(sweeper(ship)) == [], 'a pid read as another process now is never struck'
        monkeypatch.undo()
        assert write_bound.alive({pid: start})
    finally:
        cleanup(ship)


class Panes:
    """An injected server whose panes change between readings: what each reading of the roots sees, in turn."""

    def __init__(self, *readings):
        self.readings, self.calls = list(readings), 0

    def panes(self):
        seen = self.readings[min(self.calls, len(self.readings) - 1)]
        self.calls += 1
        return [SimpleNamespace(pid=pid) for pid in seen]

    def server_pid(self):
        return stand_in_server()


@jailed
def test_a_jail_started_after_the_roots_were_read_is_never_a_target(ship, tmp_path, monkeypatch):
    """A seat launched between the roots' reading and the scan: its fresh pane is missing from the roots that were
    read, so its outer bwrap reads as rooted nowhere. Its start is later than the reading, so it is left."""
    jail = subprocess.Popen(argv_of(ship, tmp_path / 'work'))
    try:
        assert wait_for(lambda: jail_end.orphans(ship.runtime, set(), seat='worker-1')[0], 60)
        start = write_bound._processes()[jail.pid][1]
        monkeypatch.setattr(jail_end, 'ticks_now', lambda: int(start) - 1)  # the roots were read just before it
        svc = sweeper(ship)
        svc.tmux = Panes([])  # the fresh pane is in no reading the sweep made
        assert jail_end.sweep(svc) == [] and jail.poll() is None
    finally:
        jail.kill()
        jail.wait()
        cleanup(ship)


@jailed
def test_the_roots_are_read_again_just_before_each_kill(ship, tmp_path):
    """A jail the first reading of the roots missed, but rooted in a fresh pane by the time of the kill, is left."""
    jail = subprocess.Popen(argv_of(ship, tmp_path / 'work'))
    try:
        assert wait_for(lambda: jail_end.orphans(ship.runtime, set(), seat='worker-1')[0], 60)
        aged(write_bound._processes()[jail.pid][1])
        svc = sweeper(ship)
        svc.tmux = Panes([], [jail.pid])  # missed by the first reading, its pane there in the one before the kill
        assert jail_end.sweep(svc) == [] and jail.poll() is None
        assert svc.tmux.calls == 2, 'read twice: once for the scan, once just before the kill'
    finally:
        jail.kill()
        jail.wait()
        cleanup(ship)


@jailed
def test_a_pane_opened_between_the_roots_reading_and_the_scan_keeps_its_jail(ship, tmp_path, tmux, monkeypatch):
    """La'an's case, on a real private server: right after the sweep reads its roots, a new pane opens and a throwaway
    jail is exec'd in it, before the scan. The pane is in no reading the sweep made; the server it descends from is.
    The jail survives, with no alarm and no evidence."""
    real = jail_end.live_roots
    opened = []

    def then_a_pane(runtime, server, ship=None):
        roots = real(runtime, server, ship)
        if not opened:  # the first reading only: the scan follows it
            workspace = tmp_path / 'work'
            script = tmp_path / 'launch.sh'
            script.write_text(f'#!/bin/sh\nexec {shlex.join(argv_of(ship, workspace))}\n')
            opened.append(tmux.new_window('Sweep', 'fresh', tmp_path, ['sh', str(script)]))
            assert wait_for(lambda: jail_end.orphans(runtime, set(), seat='worker-1')[0], 60), 'its jail runs'
        return roots

    tmux.new_session('Sweep')
    monkeypatch.setattr(jail_end, 'live_roots', then_a_pane)
    monkeypatch.setattr(jail_end, 'ticks_now', lambda: 10 ** 12)  # the start guard aside: the server root alone
    svc = SimpleNamespace(ship=ship, tmux=tmux, log=lambda event, **data: None)
    try:
        assert jail_end.sweep(svc) == []
        [pane] = opened
        assert tmux.pane(pane.id) is not None and write_bound.alive({pane.pid: write_bound._processes()[pane.pid][1]})
        assert jail_end.alarm(ship.runtime, 'worker-1') == (None, None, False)
        assert not (ship.runtime / 'evidence' / jail_end.EVIDENCE).exists()
    finally:
        cleanup(ship)


def test_a_server_whose_pid_cannot_be_read_makes_no_orphan(tmp_path):
    logs = []
    svc = SimpleNamespace(ship=SimpleNamespace(runtime=tmp_path),
                          tmux=SimpleNamespace(panes=lambda: [], server_pid=lambda: None),
                          log=lambda event, **data: logs.append((event, data)))
    assert jail_end.sweep(svc) == [] and logs[-1][1]['outcome'] == 'unknown'


@jailed
def test_a_jail_descending_from_the_server_is_never_an_orphan_though_no_pane_reading_shows_it(ship, tmp_path,
                                                                                              monkeypatch):
    """The server root alone: a jail whose pane no reading of the panes shows, before the scan or before the kill,
    still descends from the ship's server, so it is no orphan. Here a stand-in server starts it as its child."""
    words = argv_of(ship, tmp_path / 'work')
    server = subprocess.Popen([sys.executable, '-c', 'import subprocess, sys\nsubprocess.run(sys.argv[1:])', *words])
    try:
        assert wait_for(lambda: jail_end.orphans(ship.runtime, set(), seat='worker-1')[0], 60)
        monkeypatch.setattr(jail_end, 'ticks_now', lambda: 10 ** 12)  # the start guard aside: the server root alone
        svc = SimpleNamespace(ship=ship, tmux=SimpleNamespace(panes=lambda: [], server_pid=lambda: server.pid),
                              log=lambda event, **data: None)
        assert jail_end.sweep(svc) == [] and server.poll() is None
        assert jail_end.orphans(ship.runtime, set(), seat='worker-1')[0], 'the jail still runs'
    finally:
        server.kill()
        server.wait()
        cleanup(ship)


# -- a lost server, told from an unreadable one ------------------------------------------------------------------------

class NoAnswer:
    """A server reading that gives no pid and no panes, at the socket named."""

    def __init__(self, socket):
        self.socket = str(socket)

    def server_pid(self):
        return None

    def panes(self):
        raise OSError('synthetic: tmux gave no answer')


def refusing_socket(folder: Path) -> Path:
    """A socket file no server listens on: what a lost server leaves behind."""
    import socket as sockets
    path = folder / 's'
    bound = sockets.socket(sockets.AF_UNIX, sockets.SOCK_STREAM)
    bound.bind(str(path))
    bound.close()
    return path


def test_the_server_state_is_read_from_its_socket(short):
    import socket as sockets
    assert jail_end.server_state(NoAnswer(short / 'absent')) == jail_end.GONE
    assert jail_end.server_state(NoAnswer(refusing_socket(short))) == jail_end.GONE
    listening = sockets.socket(sockets.AF_UNIX, sockets.SOCK_STREAM)
    listening.bind(str(short / 'live'))
    listening.listen(1)
    try:
        assert jail_end.server_state(NoAnswer(short / 'live')) == jail_end.LIVE
    finally:
        listening.close()
    (short / 'plain').write_text('not a socket')
    assert jail_end.server_state(NoAnswer(short / 'plain')) == jail_end.UNKNOWN


@jailed
@pytest.mark.parametrize('lost', ['absent', 'refusing'])
def test_with_no_server_the_sweep_takes_the_launches_alone_as_roots(ship, tmp_path, short, lost):
    """After a lost server nothing can descend from it: the sweep goes on, and a seat's live launch still holds."""
    socket = short / 'absent' if lost == 'absent' else refusing_socket(short)
    live = subprocess.Popen(argv_of(ship, tmp_path / 'live'))
    try:
        assert wait_for(lambda: jail_end.orphans(ship.runtime, set(), seat='worker-1')[0], 60)
        trusted = trusted_files.make(ship.runtime, 'worker-1')
        (trusted / trusted_files.LAUNCH).write_text(json.dumps(dict(
            pid=live.pid, start=write_bound._processes()[live.pid][1])))  # worker-1's live launch
        other = orphaned_jail_of(ship, tmp_path / 'other', 'worker-2')
        logs = []
        svc = SimpleNamespace(ship=ship, tmux=NoAnswer(socket), log=lambda event, **data: logs.append((event, data)))
        ended = jail_end.sweep(svc)
        assert [(record['seat'], record['pid']) for record in ended] == [('worker-2', other[0])]
        assert live.poll() is None, "a seat's live launch is a root without any server"
    finally:
        live.kill()
        live.wait()
        cleanup(ship)


@jailed
def test_a_server_running_but_unreadable_makes_no_orphan(ship, tmp_path, short):
    import socket as sockets
    listening = sockets.socket(sockets.AF_UNIX, sockets.SOCK_STREAM)
    listening.bind(str(short / 'live'))
    listening.listen(1)
    try:
        pid, start = orphaned_jail(ship, tmp_path / 'work')
        logs = []
        svc = SimpleNamespace(ship=ship, tmux=NoAnswer(short / 'live'),
                              log=lambda event, **data: logs.append((event, data)))
        assert jail_end.sweep(svc) == [] and logs[-1][1]['outcome'] == 'unknown'
        assert write_bound.alive({pid: start}), 'it stands down: nothing is swept'
    finally:
        listening.close()
        cleanup(ship)


def orphaned_jail_of(ship, workspace: Path, seat: str) -> tuple[int, str]:
    """A throwaway orphan of another seat of the drydock's, aged past the sweep's margin."""
    workspace.mkdir(exist_ok=True)
    own = ship.runtime / 'seats' / seat
    (own / 'home').mkdir(parents=True, exist_ok=True)
    jail = sandbox.plan(ship, seat, own, own / 'home', workspace, '/usr/bin/python3', '/usr/bin', home=Path.home())
    words = sandbox.command(jail, ['/usr/bin/python3', '-c', READY, str(workspace)])
    subprocess.run([sys.executable, '-c', 'import os, sys\nif os.fork() == 0:\n    os.setsid()\n'
                    '    os.execvp(sys.argv[1], sys.argv[1:])\nos._exit(0)', *words], check=True)
    assert wait_for(lambda: (workspace / 'ready').exists(), 60)
    [(_, pid, start)] = jail_end.orphans(ship.runtime, set(), seat=seat)[0]
    aged(start)
    return pid, start
