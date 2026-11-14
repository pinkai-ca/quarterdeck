"""The ship's locks are out of every jail's reach, and nothing the service waits on can be held from one.

Real bubblewrap. flock needs no write access, so any file a jailed seat can open is one it can hold. A jail
walks the whole ship folder and holds an exclusive lock on every file it can open; meanwhile, outside, the
service's pass and every ``qd`` path that takes a ship lock run, with each flock they make recorded. They must
finish within a bound, and no file they lock may be one the jail could open.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import (check_outcome, install, lifecycle, pending_lifecycle, post, provenance, raise_ship,
                         record_owner, request_queue, sandbox, servicectl, ship as shipmod, ship_locks)

BOUND = 60.0  # seconds the whole outside run may take while the jail holds everything it can

# The one lock a jail can still reach: the temporary check seat's lease. Its own jailed `qd inbox` must read it,
# so it stays under runtime/post. Every take of it is non-blocking, so nothing waits on it. A foreign jail holding
# it could make a check refuse as busy, or make a dead check's registration read as live. Named here, never
# silently allowed.
NAMED_EXCEPTIONS = ('post/transient/',)


def namespaces_allowed() -> bool:
    if not sandbox.available():
        return False
    try:
        return subprocess.run(['bwrap', '--ro-bind', '/', '/', 'true'], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


PROBE = r'''
import fcntl, json, os, sys, time
ship, work = sys.argv[1:3]
held, seen_locks = [], []
for folder, folders, files in os.walk(ship, followlinks=False):
    for name in files + folders:
        path = os.path.join(folder, name)
        if os.path.islink(path):
            continue
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            continue
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(descriptor)
            continue
        held.append((descriptor, path))
        if name.endswith('.lock'):
            seen_locks.append(path)
locks = os.path.join(ship, 'runtime', 'locks')
report = dict(held=[path for _, path in held], lock_files=seen_locks,
              locks_folder=sorted(os.listdir(locks)) if os.path.isdir(locks) else None)
with open(os.path.join(work, 'held.json'), 'w') as stream:
    json.dump(report, stream)
deadline = time.monotonic() + 120
while not os.path.exists(os.path.join(work, 'done')) and time.monotonic() < deadline:
    time.sleep(0.02)
'''


def descriptor_path(target) -> str | None:
    descriptor = target if isinstance(target, int) else target.fileno()
    try:
        return os.readlink(f'/proc/self/fd/{descriptor}')
    except OSError:
        return None


def every_lock_once(ship: shipmod.Ship, tmux) -> None:
    """Make every lock file the ship's code takes, so the jail finds each one if it can be found."""
    from quarterdeck.service import Service

    runtime = ship.runtime
    own = runtime / 'seats' / 'worker-1'
    record = own / 'home' / 'synthetic-record.jsonl'
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text('')
    with servicectl.acquire_lock(runtime):
        svc = Service(ship, tmux)
        try:
            svc.tick()
            message = svc.store.new('order', 'captain', 'worker-1', 'Synthetic order for the lock gate.')
            with post.admission(runtime, 'worker-1'):
                pass
            svc.attempts.close(svc.attempts.open(message, 'worker-1', None, None, None), 'delivered')
            with lifecycle.claim(runtime, 'worker-1'):
                pass
            with request_queue.coordination(runtime) as owned:
                assert owned
            with check_outcome._lock(runtime, 'fake'):
                pass
            pending_lifecycle._retirement_log(runtime, 'worker-1', 'synthetic.json')
            record_owner.register(own, record, 'fake')
            provenance.remember_launch(own, record, SimpleNamespace(at='2026-01-01T00:00:00+00:00', pool='synthetic',
                                                                    harness='fake', price=None))
            with install.install_lock(ship, blocking=False):
                pass
            with raise_ship.locked(runtime, 'gamma'):
                pass
        finally:
            svc.ledger.close()


@pytest.mark.skipif(not namespaces_allowed(), reason='bubblewrap is not installed, or cannot make a namespace here')
def test_no_lock_the_ship_takes_can_be_held_from_a_jail_and_nothing_waits_on_one_that_can(drydock: Path, tmux, tmp_path, monkeypatch):
    ship = shipmod.load(drydock)
    shipmod.fence(ship)
    runtime = ship.runtime
    workspace = tmp_path / 'lock-work'
    workspace.mkdir()
    own = runtime / 'seats' / 'worker-1'
    (own / 'home').mkdir(parents=True, exist_ok=True)
    every_lock_once(ship, tmux)  # every lock file now exists where the code keeps it
    for legacy in ('service.lock', 'request.lock', 'install.lock', 'post/admission', 'post/lifecycle', 'post/attempts/.lock'):
        assert not (runtime / legacy).exists(), f'{legacy}: a lock at its earlier place'
    assert sorted(os.listdir(runtime / 'locks')), 'the ship keeps its locks under runtime/locks'

    jail = sandbox.plan(ship, 'worker-1', own, own / 'home', workspace, '/usr/bin/python3', '/usr/bin', home=Path.home())
    env = {key: value for key, value in os.environ.items() if not key.startswith(('QD_', 'TMUX'))}
    child = subprocess.Popen(sandbox.command(jail, [sys.executable, '-c', PROBE, str(ship.folder), str(workspace)]),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    taken: list[tuple[str, int]] = []
    finished: list[float] = []
    failure: list[BaseException] = []
    try:
        deadline = time.monotonic() + 60
        while not (workspace / 'held.json').exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (workspace / 'held.json').exists(), child.communicate(timeout=10)
        seen = json.loads((workspace / 'held.json').read_text())
        assert seen['held'], 'the jail holds whatever it can open: the probe works'

        real = fcntl.flock

        def recorded(target, operation):
            if not operation & fcntl.LOCK_UN and (path := descriptor_path(target)):
                taken.append((path, operation))
            return real(target, operation)

        monkeypatch.setattr(fcntl, 'flock', recorded)

        def outside():
            started = time.monotonic()
            try:
                every_lock_once(ship, tmux)
            except BaseException as error:  # reported below, with the bound
                failure.append(error)
            finished.append(time.monotonic() - started)

        worker = threading.Thread(target=outside, daemon=True)
        worker.start()
        worker.join(BOUND)
        monkeypatch.setattr(fcntl, 'flock', real)
    finally:
        (workspace / 'done').write_text('')
        try:
            child.communicate(timeout=60)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)

    assert finished, f'the service and qd blocked behind locks the jail held, past {BOUND:g}s'
    assert not failure, failure
    assert seen['locks_folder'] == [], 'runtime/locks is empty inside the jail'
    reachable = set(seen['held'])
    locked = {path for path, _ in taken}
    assert locked, 'the outside run took the ship locks: the recording works'
    assert all(path.startswith(str(runtime / 'locks') + '/') or any(part in path for part in NAMED_EXCEPTIONS)
               or not path.startswith(str(ship.folder)) for path in locked), sorted(locked)
    overlap = sorted(path for path in locked & reachable if not any(part in path for part in NAMED_EXCEPTIONS))
    assert overlap == [], f'locks the ship takes that a jail can hold: {overlap}'
    visible_locks = [path for path in seen['lock_files'] if not any(part in path for part in NAMED_EXCEPTIONS)]
    assert visible_locks == [], f'lock files a jail can open: {visible_locks}'


@pytest.mark.skipif(not namespaces_allowed(), reason='bubblewrap is not installed, or cannot make a namespace here')
def test_a_service_of_the_earlier_code_or_anything_holding_its_old_lock_reads_as_alive_and_blocks_a_start(drydock: Path):
    """During a sync the old service holds runtime/service.lock: the new code must see it, and must not start a second."""
    ship = shipmod.load(drydock)
    runtime = ship.runtime
    legacy = servicectl.legacy_lock_path(runtime)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    with legacy.open('a') as old:
        fcntl.flock(old, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert servicectl.alive(runtime), 'a service of the earlier code still owns the ship'
        with pytest.raises(shipmod.ShipError, match='earlier code'):
            servicectl.acquire_lock(runtime)
        assert not servicectl._held(servicectl.lock_path(runtime)), 'the refused start keeps no lock'
    handle = servicectl.acquire_lock(runtime)
    try:
        assert not legacy.exists(), 'the first service of this code removes the earlier lock'
        assert servicectl.alive(runtime)
    finally:
        handle.close()
    assert not servicectl.alive(runtime)


def test_every_wait_on_a_ship_lock_is_bounded_and_says_which(tmp_path):
    runtime = tmp_path / 'runtime'
    lock = ship_locks.path(runtime, 'admission', 'synthetic.lock')
    with ship_locks.held(lock, 'first'):
        started = time.monotonic()
        with pytest.raises(ship_locks.Busy, match='synthetic.lock still held after 0.2s'):
            with ship_locks.held(lock, 'second', wait=0.2):
                pass
        assert time.monotonic() - started < 2
    with ship_locks.held(lock, 'again', wait=0):
        pass
    assert post.LockBusy is ship_locks.Busy
    with pytest.raises(shipmod.ShipError, match='not a plain name'):
        ship_locks.path(runtime, '../escape')
    outside = tmp_path / 'elsewhere' / 'x.lock'
    with pytest.raises(shipmod.ShipError, match='outside runtime/locks'):
        with ship_locks.held(outside, 'stray'):
            pass
