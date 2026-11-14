"""A slow owned tmux command or an exiting server does not fail lifecycle or cleanup; a stuck holder still refuses."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from quarterdeck import seat as seatmod, ship as shipmod, tmux_socket
from quarterdeck.tmux import TmuxError

SLOW = .6  # longer than the old 250 ms budget, as a tmux client on a loaded machine can take
BUDGET = getattr(tmux_socket, 'EXCLUSIVE_WAIT', .25)


def hold_shared(tmux, seconds):
    """Another process holds the namespace's shared transport lease, as an owned tmux query in flight does."""
    code = ('import fcntl,sys,time\n'
            'with open(sys.argv[1],"rb") as lock:\n'
            ' fcntl.flock(lock,fcntl.LOCK_SH)\n'
            ' print("held",flush=True)\n'
            ' time.sleep(float(sys.argv[2]))\n')
    child = subprocess.Popen([sys.executable, '-c', code, str(Path(tmux.socket).parent / tmux_socket.LOCK), str(seconds)],
                             stdout=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == 'held'
    return child


def race_stat_read(monkeypatch, pid):
    """Open /proc/<pid>/stat when armed, let the process end and be reaped, then read: the kernel answers ESRCH."""
    state = {'handle': None}
    seen = []

    class Racing(type(tmux_socket.Path())):
        def read_text(self, *args, **kwargs):
            handle = state['handle']
            if str(self) != f'/proc/{pid}/stat' or handle is None:
                return super().read_text(*args, **kwargs)
            state['handle'] = None  # one raced read; later reads see the ordinary absence
            with handle:
                deadline = time.monotonic() + 5
                while os.path.exists(f'/proc/{pid}') and time.monotonic() < deadline:
                    time.sleep(.01)
                assert not os.path.exists(f'/proc/{pid}'), 'the process never ended'
                seen.append(pid)
                return handle.read()

    def arm():
        state['handle'] = open(f'/proc/{pid}/stat')
    monkeypatch.setattr(tmux_socket, 'Path', Racing)
    return arm, seen


def test_a_process_reaped_during_its_stat_read_reads_as_ended(monkeypatch):
    child = subprocess.Popen(['sleep', '30'])
    arm, seen = race_stat_read(monkeypatch, child.pid)
    arm()
    child.kill()
    child.wait()
    assert tmux_socket.process_state(child.pid) == []
    assert seen == [child.pid]


def test_other_proc_read_errors_still_raise(monkeypatch):
    class Denied(type(tmux_socket.Path())):
        def read_text(self, *args, **kwargs):
            raise PermissionError(13, 'Permission denied', str(self))
    monkeypatch.setattr(tmux_socket, 'Path', Denied)
    with pytest.raises(PermissionError):
        tmux_socket.process_state(os.getpid())


@pytest.mark.parametrize('route', ['kill_server', 'close'])
def test_a_server_exiting_during_cleanup_is_proved_ended(tmux, monkeypatch, route):
    tmux.new_session('Synthetic')
    pid = tmux._owned.pid
    arm, seen = race_stat_read(monkeypatch, pid)
    real_run = type(tmux)._run

    def run(self, *args, **kwargs):
        if 'kill-server' in args:
            arm()  # the cleanup's next read of this server's state races its exit
        return real_run(self, *args, **kwargs)
    monkeypatch.setattr(type(tmux), '_run', run)
    if route == 'kill_server':
        tmux.kill_server()
        assert not tmux.running()
    else:
        assert tmux.close(), getattr(tmux, 'held', None)
    assert seen == [pid], 'the read really raced the exiting server'


def test_a_slow_owned_command_does_not_fail_a_fresh_start(drydock, tmux):
    ship = shipmod.load(drydock)
    seat = seatmod.Seat(ship, ship.seat('worker-1'), tmux)
    seat.up(False)
    old = seat.launched()
    holder = hold_shared(tmux, SLOW)
    try:
        started = time.monotonic()
        seat.fresh()
        waited = time.monotonic() - started
    finally:
        holder.wait(timeout=5)
    new = seat.launched()
    assert new.pane != old.pane and tmux.pane(old.pane) is None and tmux.pane(new.pane).pid == new.pid
    assert waited >= .2, 'the retirement waited for the slow command rather than skipping the lock'


def test_a_slow_owned_command_does_not_hold_cleanup(tmux):
    tmux.new_session('Synthetic')
    endpoint = Path(tmux.socket)
    holder = hold_shared(tmux, SLOW)
    try:
        assert tmux.close(), tmux.held
    finally:
        holder.wait(timeout=5)
    assert not endpoint.exists() and not endpoint.parent.exists()


def readers_taking_turns(tmux, seconds, lease=.05, count=2):
    """Processes taking shared transport leases through the framework's own lock, overlapping one another, as the
    service's pane reads and an observer's do on a loaded machine; each retries a busy answer."""
    code = ('import sys,time\nfrom quarterdeck import tmux_socket\n'
            'socket,lease,offset,seconds=sys.argv[1],*map(float,sys.argv[2:])\n'
            'time.sleep(offset)\nprint("reading",flush=True)\nend=time.monotonic()+seconds\nbusy=0\n'
            'while time.monotonic()<end:\n'
            ' try:\n  with tmux_socket.locked(socket,exclusive=False):\n   time.sleep(lease)\n'
            ' except tmux_socket.OwnershipError:\n  busy+=1\n'
            ' time.sleep(.005)\nprint("busy",busy,flush=True)\n')
    readers = [subprocess.Popen([sys.executable, '-c', code, tmux.socket, str(lease), str(lease * n / count), str(seconds)],
                                stdout=subprocess.PIPE, text=True) for n in range(count)]
    for reader in readers:
        assert reader.stdout.readline().strip() == 'reading'
    time.sleep(lease * 2)  # the leases overlap before the exclusive operation begins
    return readers


def test_readers_taking_turns_do_not_starve_a_fresh_start(drydock, tmux):
    """The red on 005a163: shared leases overlapping one another kept the lock shared for the whole budget, so the
    drill's fresh start found it never free and its retirement failed busy, though no read was slow."""
    ship = shipmod.load(drydock)
    seat = seatmod.Seat(ship, ship.seat('worker-1'), tmux)
    seat.up(False)
    old = seat.launched()
    readers = readers_taking_turns(tmux, BUDGET * 3)
    try:
        started = time.monotonic()
        seat.fresh()
        waited = time.monotonic() - started
    finally:
        reports = [reader.communicate(timeout=BUDGET * 3 + 10)[0] for reader in readers]
    new = seat.launched()
    assert new.pane != old.pane and tmux.pane(old.pane) is None and tmux.pane(new.pane).pid == new.pid
    assert waited < BUDGET, 'the waiting operation went before new leases instead of using up its budget'
    assert any(int(report.split()[-1]) > 0 for report in reports), 'a reader met the waiting operation and was turned away'


def test_a_waiting_exclusive_operation_turns_new_readers_away_and_leaves_no_entry(tmux):
    import threading

    tmux.new_session('Synthetic')
    folder = Path(tmux.socket).parent
    entries = sorted(os.listdir(folder))
    holder = hold_shared(tmux, SLOW)
    done = []
    writer = threading.Thread(target=lambda: done.append(tmux.run('set-option', '-g', 'status', 'off')))
    try:
        writer.start()
        time.sleep(SLOW / 3)  # the operation now waits for the holder's lease
        with pytest.raises(tmux_socket.OwnershipError, match='busy'):
            with tmux_socket.locked(tmux.socket, exclusive=False):
                pass
        assert sorted(os.listdir(folder)) == entries, 'the mark is on the folder itself, never a new entry in it'
    finally:
        holder.wait(timeout=5)
        writer.join(timeout=BUDGET + 5)
    assert done == [''], 'the operation ran once the lease ended'
    with tmux_socket.locked(tmux.socket, exclusive=False):
        pass  # and new readers are welcome again
    assert sorted(os.listdir(folder)) == entries


def test_a_holder_past_the_budget_still_refuses_and_retains(drydock, tmux):
    ship = shipmod.load(drydock)
    seat = seatmod.Seat(ship, ship.seat('worker-1'), tmux)
    seat.up(False)
    launch = seat.launched()
    path = seat.folder / 'launch.json'
    retained = path.read_bytes()
    holder = hold_shared(tmux, BUDGET * 2 + 5)
    try:
        started = time.monotonic()
        with pytest.raises((TmuxError, seatmod.ShipError), match='busy'):
            seat.down()
        assert time.monotonic() - started < BUDGET + 1
        assert path.read_bytes() == retained and tmux.pane(launch.pane).pid == launch.pid
        assert not tmux.close() and 'busy' in tmux.held
        assert Path(tmux.socket).exists()
    finally:
        holder.terminate()
        holder.wait(timeout=5)
    assert tmux.close(), tmux.held
