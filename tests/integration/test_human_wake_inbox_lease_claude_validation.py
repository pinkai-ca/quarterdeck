"""As the captain ruled for check seats: `qd inbox`, with and without --wait, admits an undeclared temporary seat only while a live
check owns it, proved by the check's own lease in the runtime, never by its name.

The lease is the framework's own: a transient lock and a check-inbox registration, held by a separate process in a
PID namespace of its own, so the pid it records means nothing here and admission must rest on the live lease alone.
The command is the real `qd inbox --wait`, run as a subprocess for a temporary human check seat. Names are synthetic.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from quarterdeck import post, sandbox
from quarterdeck import ship as shipmod

QD = Path(sys.executable).parent / 'qd'  # this checkout's own qd, never an installed copy
SEAT = 'check-wake-fernleaf'

HOLDER = r'''
import sys
from pathlib import Path
from quarterdeck import pending
runtime, name, human = Path(sys.argv[1]), sys.argv[2], sys.argv[3] == 'human'
with pending.transient_lock(runtime, name) as owned:
    if not owned:
        raise SystemExit('busy')
    with pending.check_inbox_registration(runtime, name, human, enabled=True):
        print('held', flush=True)
        sys.stdin.read()
print('released', flush=True)
'''


def namespaces_allowed() -> bool:
    if not sandbox.available():
        return False
    try:
        return subprocess.run(['bwrap', '--unshare-pid', '--dev-bind', '/', '/', 'true'],
                              capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


pytestmark = pytest.mark.skipif(not namespaces_allowed(), reason='bubblewrap cannot make a PID namespace here')


@pytest.fixture
def ship(drydock, monkeypatch):
    monkeypatch.setenv('QD_SHIP', str(drydock))
    monkeypatch.delenv('QD_SEAT', raising=False)
    loaded = shipmod.load(drydock)
    shipmod.fence(loaded)
    assert SEAT not in loaded.seats, 'undeclared: only the lease can admit it'
    return loaded


class Lease:
    """The check's ownership of one temporary seat, held from inside a PID namespace of its own."""

    def __init__(self, ship, human=True):
        self.ship = ship
        self.process = subprocess.Popen(
            ['bwrap', '--unshare-pid', '--die-with-parent', '--dev-bind', '/', '/',
             sys.executable, '-I', '-c', HOLDER, str(ship.runtime), SEAT, 'human' if human else 'crew'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        line = self.process.stdout.readline()
        assert line == 'held\n', (line, self.process.stderr.read() if self.process.poll() is not None else '')
        self.marker = ship.runtime / 'post' / 'transient' / f'{SEAT}.check.json'
        self.recorded = json.loads(self.marker.read_text())

    def end(self):
        self.process.stdin.close()
        assert self.process.stdout.readline() == 'released\n'
        self.process.wait(timeout=20)

    def kill(self):
        os.killpg(self.process.pid, signal.SIGKILL)
        self.process.wait(timeout=20)

    def close(self):
        if self.process.poll() is None:
            self.kill()


@pytest.fixture
def leases():
    held = []
    yield lambda ship, **kw: held.append(Lease(ship, **kw)) or held[-1]
    for lease in held:
        lease.close()


def inbox(ship, *args, timeout=60):
    env = {k: v for k, v in os.environ.items() if not k.startswith(('QD_', 'TMUX'))}
    env.update(QD_SHIP=str(ship.folder), QD_SEAT=SEAT)
    return subprocess.run([str(QD), 'inbox', *args], env=env, capture_output=True, text=True, timeout=timeout)


def waiting(ship):
    return post.Store(ship.runtime).new('order', 'captain', SEAT, 'Synthetic waiting order for the check seat')


def untouched(ship, message):
    assert [a.result for a in post.Attempts(ship.runtime).for_message(message.id)] == [], 'no receipt'
    assert not post.watch_file(ship.runtime, SEAT).exists(), 'no watch registered'


def test_a_live_human_check_lease_admits_the_real_watch_which_prints_registers_and_receipts(ship, leases):
    lease = leases(ship)
    message = waiting(ship)
    host_pid = lease.process.pid
    assert lease.recorded['pid'] != host_pid and lease.recorded['human'] is True, \
        'the lease names a pid of its own namespace; admission cannot rest on it'
    done = inbox(ship, '--wait')
    assert done.returncode == 0, done.stderr
    assert message.short in done.stdout and 'Synthetic waiting order for the check seat' in done.stdout
    assert [a.result for a in post.Attempts(ship.runtime).for_message(message.id)] == ['watch'], 'the watch receipt'
    generation = (post.watch_file(ship.runtime, SEAT).parent / 'watch-generation').read_text().strip()
    assert len(generation) == 32 and all(c in '0123456789abcdef' for c in generation), 'a generation registered'
    plain = inbox(ship)
    assert plain.returncode == 0, plain.stderr
    lease.end()
    assert not lease.marker.exists(), 'the check retires its own registration at teardown'


def test_a_crew_check_seat_reads_its_inbox_but_is_never_given_the_human_watch(ship, leases):
    leases(ship, human=False)
    message = waiting(ship)
    plain = inbox(ship)
    assert plain.returncode == 0 and message.short in plain.stdout, plain.stderr
    done = inbox(ship, '--wait')
    assert done.returncode != 0 and "--wait is for seats in a human's session" in done.stderr
    untouched(ship, message)


def test_a_lease_whose_owner_died_is_refused_by_name(ship, leases):
    lease = leases(ship)
    message = waiting(ship)
    lease.kill()  # no teardown ran: the registration file stays, its lease is gone
    assert lease.marker.exists()
    for args in (('--wait',), ()):
        done = inbox(ship, *args)
        assert done.returncode != 0 and SEAT in done.stderr and 'stale' in done.stderr, done.stderr
        assert message.short not in done.stdout
    untouched(ship, message)


def test_an_undeclared_name_without_a_registration_is_refused_by_name(ship):
    message = waiting(ship)
    for args in (('--wait',), ()):
        done = inbox(ship, *args)
        assert done.returncode != 0 and SEAT in done.stderr and 'no live check-owned temporary inbox' in done.stderr
        assert message.short not in done.stdout
    untouched(ship, message)


def test_a_held_transient_lock_alone_is_no_registration(ship):
    """The lock without the check's registration beside it: a name shape and a lock admit nothing."""
    holder = subprocess.Popen([sys.executable, '-I', '-c', (
        'import sys\nfrom pathlib import Path\nfrom quarterdeck import pending\n'
        'with pending.transient_lock(Path(sys.argv[1]), sys.argv[2]) as owned:\n'
        '    print("held" if owned else "busy", flush=True)\n    sys.stdin.read()\n'), str(ship.runtime), SEAT],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline() == 'held\n'
        message = waiting(ship)
        done = inbox(ship, '--wait')
        assert done.returncode != 0 and SEAT in done.stderr and message.short not in done.stdout
        untouched(ship, message)
    finally:
        holder.stdin.close()
        holder.wait(timeout=20)
