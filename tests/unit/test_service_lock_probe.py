"""Reading who holds the service's lock never takes the lock.

A reader that probes by taking the lock for an instant makes a service starting at that instant refuse a lock nobody
keeps. This code's readers read the kernel's table (/proc/locks) by device and inode instead, and a start tries again
for a bounded moment, since a reader of an earlier copy still probes. A row whose pid is 0 or less names no holder.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from quarterdeck import lock_holders, servicectl
from quarterdeck.ship import ShipError

HOLD = r"""
import sys, time
from pathlib import Path
from quarterdeck import lock_holders, servicectl
lock = servicectl.acquire_lock(Path(sys.argv[1]))
print('held', flush=True)
time.sleep(120)
"""

# A reader of an earlier copy: it probes by taking the lock for an instant, over and over, wherever the file is.
PROBE_BY_TAKING = r"""
import fcntl, os, sys
print('probing', flush=True)
while True:
    try:
        descriptor = os.open(sys.argv[1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        continue
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        os.close(descriptor)
"""

# A service started for an earlier copy's installer: it holds the lock and its earlier place too.
HOLD_BOTH = r"""
import sys, time
from pathlib import Path
from quarterdeck import lock_holders, servicectl
runtime = Path(sys.argv[1])
lock = servicectl.acquire_lock(runtime)
legacy = servicectl.hold_legacy(runtime)
print('held', flush=True)
time.sleep(120)
"""

# Eight processes each taking and releasing fifty locks of their own, over and over: rows come and go in the table.
CHURN = r"""
import fcntl, os, sys, tempfile
folder = tempfile.mkdtemp(dir=sys.argv[1])
locks = [os.open(os.path.join(folder, str(i)), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600) for i in range(50)]
first = True
while True:
    for lock in locks:
        fcntl.flock(lock, fcntl.LOCK_EX)
    for lock in locks:
        fcntl.flock(lock, fcntl.LOCK_UN)
    if first:
        print('churning', flush=True)
        first = False
"""

STARTS = 300
READINGS = 3000


@pytest.fixture
def runtime(tmp_path):
    folder = tmp_path / 'runtime'
    folder.mkdir()
    return folder


def started_child(marker, *argv):
    process = subprocess.Popen([sys.executable, *argv], stdout=subprocess.PIPE, text=True)
    assert process.stdout.readline().strip() == marker
    return process


def end(process):
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


def start_many(runtime, before=None):
    """Start and end a service's lock STARTS times; the refusals, by message."""
    refused = []
    for _ in range(STARTS):
        if before:
            before()
        try:
            servicectl.acquire_lock(runtime).close()
        except ShipError as error:
            refused.append(str(error))
    return refused


@pytest.mark.parametrize('place', ['lock', 'earlier-lock-place'])
def test_a_reader_probing_in_a_loop_never_makes_a_starting_service_refuse(runtime, place):
    """An earlier copy's reader takes the lock for an instant, at the lock's place or its earlier place (which a start
    takes too while it exists): a start meeting that probe tries again, and never refuses a lock nobody keeps."""
    servicectl.acquire_lock(runtime).close()  # makes runtime/locks and the lock file
    legacy = servicectl.legacy_lock_path(runtime)
    target = servicectl.lock_path(runtime) if place == 'lock' else legacy
    reader = started_child('probing', '-c', PROBE_BY_TAKING, str(target))
    try:
        refused = start_many(runtime, before=(lambda: legacy.touch()) if place == 'earlier-lock-place' else None)
    finally:
        end(reader)
    assert refused == [], f'{len(refused)} of {STARTS} starts refused: {refused[:2]}'


@pytest.mark.parametrize('held', [True, False], ids=['held', 'free'])
def test_this_codes_readers_never_take_the_lock(runtime, monkeypatch, held):
    """locked(), holder(), started() and alive() read the kernel's table and never call flock, whether a service
    holds the lock or it is free, at its place and at its earlier place."""
    servicectl.acquire_lock(runtime).close()
    servicectl.legacy_lock_path(runtime).touch()
    service = started_child('held', '-c', HOLD_BOTH, str(runtime)) if held else None
    try:
        def taken(*args):
            raise AssertionError('a reader took the lock')
        monkeypatch.setattr(servicectl.fcntl, 'flock', taken)
        assert servicectl.locked(runtime) is held and servicectl.alive(runtime) is held
        assert servicectl.holder(runtime) == (service.pid if held else None)
        assert servicectl.started(runtime) is False  # no beat yet
    finally:
        monkeypatch.undo()
        if service:
            end(service)


class Clock:
    """An injected clock: a pause moves it on at once, so the bound is measured in its time, not the machine's."""
    def __init__(self):
        self.now, self.pauses = 0.0, []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.pauses.append(seconds)
        self.now += seconds


def test_a_lock_held_for_good_is_refused_once_the_bounded_moment_has_passed(runtime, monkeypatch):
    """A start keeps trying a held lock until TAKE_SECONDS have passed on its clock, then refuses it."""
    service = started_child('held', '-c', HOLD, str(runtime))
    try:
        clock = Clock()
        monkeypatch.setattr(servicectl, 'time', clock)
        with pytest.raises(ShipError, match='another service holds'):
            servicectl.acquire_lock(runtime)
        assert len(clock.pauses) > 1, 'it tried again'
        assert servicectl.TAKE_SECONDS <= clock.now < servicectl.TAKE_SECONDS + max(clock.pauses)
        assert servicectl.holder(runtime) == service.pid and servicectl.locked(runtime)
    finally:
        end(service)


def table_row(path: Path, pid: int, device: str | None = None) -> str:
    info = os.lstat(path)
    device = device or f'{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}'
    return f'1: FLOCK  ADVISORY  WRITE {pid} {device}:{info.st_ino} 0 EOF\n'


@pytest.mark.parametrize('pid', [0, -1])
def test_a_row_whose_pid_is_not_positive_names_no_holder(runtime, monkeypatch, pid):
    """Older kernels show a lock held outside the reader's namespace, or a freed owner's, with pid 0: the lock reads
    held, no holder is named, and stop refuses by name rather than signal pid 0, which is the caller's own group."""
    servicectl.acquire_lock(runtime).close()
    path = servicectl.lock_path(runtime)
    monkeypatch.setattr(lock_holders, 'table', lambda: table_row(path, pid))
    signalled = []
    monkeypatch.setattr(servicectl.os, 'kill', lambda *args: signalled.append(args))
    assert lock_holders.reading(path) == (True, None)
    assert servicectl.locked(runtime) and servicectl.holder(runtime) is None
    with pytest.raises(ShipError, match='its holder cannot be read'):
        servicectl.stop(runtime)
    assert signalled == []


def test_the_table_is_read_by_device_and_inode(runtime, monkeypatch):
    """The same inode on another device is another file; where only such a row is found, the table cannot say (a file
    system whose table and stat number devices differently), and the lock is probed instead."""
    servicectl.acquire_lock(runtime).close()
    path = servicectl.lock_path(runtime)
    monkeypatch.setattr(lock_holders, 'table', lambda: table_row(path, 4242, device='ff:ff'))
    assert lock_holders.reading(path) is None
    assert servicectl._held(path) is False, 'probed, and free'
    monkeypatch.setattr(lock_holders, 'table', lambda: '')
    assert lock_holders.reading(path) == (False, None)
    monkeypatch.setattr(lock_holders, 'table', lambda: table_row(path, 4242) + table_row(path, 4242, 'ff:ff'))
    assert lock_holders.reading(path) == (True, 4242)


def test_a_reading_that_passed_over_a_held_locks_row_still_reads_it_held(runtime, monkeypatch):
    """The kernel serves its table a page per read and starts each page again at its position, so a row removed above
    shifts a held lock's row past the reader: a table with no row is confirmed from the descriptors that hold the
    file open, and the lock reads held, by its holder."""
    service = started_child('held', '-c', HOLD, str(runtime))
    try:
        monkeypatch.setattr(lock_holders, 'table', lambda: '')
        assert lock_holders.reading(servicectl.lock_path(runtime)) == (True, service.pid)
        assert servicectl.locked(runtime) and servicectl.holder(runtime) == service.pid
    finally:
        end(service)
    assert lock_holders.reading(servicectl.lock_path(runtime)) == (False, None) and not servicectl.locked(runtime)


def test_a_held_lock_reads_held_while_other_locks_churn_in_the_table(runtime, tmp_path):
    """Rows removed above a held lock's row while the table is read shift it past the reader; measured, a lock held
    throughout went missing from about one reading in forty. Every one of READINGS readings still names its holder.
    Bounded by events: the churners have each churned once before the first reading, and the readings are counted."""
    service = started_child('held', '-c', HOLD, str(runtime))
    churners = []
    try:
        churners = [started_child('churning', '-c', CHURN, str(tmp_path)) for _ in range(8)]
        path = servicectl.lock_path(runtime)
        readings = [lock_holders.reading(path) for _ in range(READINGS)]
        wrong = [found for found in readings if found != (True, service.pid)]
        assert all(churner.poll() is None for churner in churners), 'the churn ran throughout'
        assert wrong == [], f'{len(wrong)} of {READINGS} readings missed the holder: {wrong[:3]}'
    finally:
        for process in [*churners, service]:
            end(process)


def test_in_a_nested_pid_namespace_an_empty_table_is_not_read_as_free(runtime, monkeypatch):
    """There the table leaves out a lock held outside the namespace: an empty table cannot say the lock is free, so
    the lock is probed; a row it does show still names its holder."""
    service = started_child('held', '-c', HOLD, str(runtime))
    try:
        path = servicectl.lock_path(runtime)
        monkeypatch.setattr(lock_holders, 'nested_pid_namespace', lambda: True)
        monkeypatch.setattr(lock_holders, 'table', lambda: '')
        assert lock_holders.reading(path) is None
        assert servicectl._held(path) is True and servicectl.locked(runtime), 'probed, and held'
        assert servicectl.holder(runtime) is None
        monkeypatch.setattr(lock_holders, 'table', lambda: table_row(path, service.pid))
        assert servicectl.holder(runtime) == service.pid
    finally:
        end(service)


def test_the_table_is_unreadable_and_the_lock_is_probed(runtime, monkeypatch):
    def unreadable():
        raise PermissionError
    service = started_child('held', '-c', HOLD, str(runtime))
    try:
        monkeypatch.setattr(lock_holders, 'table', unreadable)
        assert lock_holders.reading(servicectl.lock_path(runtime)) is None
        assert servicectl.locked(runtime) and servicectl.holder(runtime) is None
    finally:
        end(service)
    assert not servicectl.locked(runtime)
