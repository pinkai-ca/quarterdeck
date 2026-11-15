"""Capacity and lifecycle examples using small modeled filesystems, never memory pressure."""
from concurrent.futures import ThreadPoolExecutor
import fcntl
import os
import subprocess
import threading
from types import SimpleNamespace

import pytest

from tests.testrunner import clean, owned, procs, scratch
from tests.testrunner.layout import RESULTS, Root, identity, new_run_id, reserve_run_id, write_json


@pytest.fixture
def capacity(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    project.mkdir()
    subprocess.run(['git', 'init', '-q', str(project)], check=True)
    (project / '.gitignore').write_text('.testruns/\n')
    base = tmp_path / 'memory'
    base.mkdir()
    free = SimpleNamespace(bytes=16 * 1024 ** 3)
    monkeypatch.setattr(scratch, 'candidates', lambda: [base])
    monkeypatch.setattr(scratch, 'filesystem', lambda fd: 'tmpfs')
    monkeypatch.setattr(os, 'fstatvfs', lambda fd: SimpleNamespace(
        f_bavail=free.bytes, f_frsize=1, f_flag=0))
    root = Root(project)
    try:
        yield root, free
    finally:
        root.close()


def test_two_reservations_fit_and_third_uses_disk(capacity, capsys):
    root, free = capacity
    runs = [reserve_run_id(root) for _ in range(3)]
    assert len(set(runs)) == 3, 'each modeled run owns a distinct result directory'
    selected = [scratch.prepare(root, run) for run in runs]
    assert [row['kind'] for row in selected] == ['tmpfs', 'tmpfs', 'disk']
    assert selected[0]['path'] != selected[1]['path']
    assert all(row['bound_bytes'] == scratch.BOUND for row in selected[:2])
    assert scratch.pending(root) == sorted(runs[:2])
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 3
    assert sum('working files on disk:' in line for line in lines) == 1
    assert '8 GiB margin' in lines[-1]
    assert scratch.record(root, runs[2]) is None


def test_concurrent_callers_cannot_promise_the_last_capacity_twice(capacity):
    root, free = capacity
    free.bytes = scratch.BOUND + scratch.MARGIN
    start = threading.Barrier(2)
    def reserve(run):
        start.wait(timeout=10)
        return scratch.prepare(root, run)
    runs = [new_run_id(), new_run_id()]
    with ThreadPoolExecutor(max_workers=2) as workers:
        selected = list(workers.map(reserve, runs))
    assert sorted(row['kind'] for row in selected) == ['disk', 'tmpfs']
    assert len(scratch.pending(root)) == 1


def test_capacity_measurement_is_inside_reservation_lock(capacity, monkeypatch):
    root, free = capacity
    observed = []
    original = os.fstatvfs
    def measure(fd):
        peer = os.open(root.path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(peer, fcntl.LOCK_EX | fcntl.LOCK_NB)
            observed.append(True)
        finally:
            os.close(peer)
        return original(fd)
    monkeypatch.setattr(os, 'fstatvfs', measure)
    assert scratch.prepare(root, new_run_id())['kind'] == 'tmpfs'
    assert len(observed) >= 2  # selection and the fresh admission check


def test_dead_run_retirement_releases_only_its_own_reservation(capacity):
    root, free = capacity
    dead, live, next_run = [new_run_id() for _ in range(3)]
    scratch.prepare(root, dead)
    scratch.prepare(root, live)
    live_storage = (root.path / RESULTS / live / scratch.RECORD).read_bytes()
    live_fd = scratch.child(root, live)
    live_lock = os.open('lock', os.O_RDWR | os.O_CREAT, 0o600, dir_fd=live_fd)
    fcntl.flock(live_lock, fcntl.LOCK_EX)
    live_file = scratch.path(root, live) / 'work.txt'
    live_file.write_text('still working\n')
    dead_fd = scratch.child(root, dead)
    try:
        token = owned.mark(dead_fd)
        state = {'run': dead, 'change': 'finished-example', 'scratch': identity(os.fstat(dead_fd)),
                 'owner': token, 'boot': procs.boot_id(), 'groups': []}
        write_json(dead_fd, 'state.json', state)
        result_fd = root.child(RESULTS, dead)
        try:
            write_json(result_fd, 'result.json', {'run': dead, 'outcome': {'status': 'passed'}})
        finally:
            os.close(result_fd)
        assert clean.retire_within(root, dead) == 0
        assert not scratch.path(root, dead).exists()
        assert scratch.record(root, dead) is None
        assert (root.path / RESULTS / live / scratch.RECORD).read_bytes() == live_storage
        assert live_file.read_text() == 'still working\n'
        assert scratch.pending(root) == [live]
        assert scratch.prepare(root, next_run)['kind'] == 'tmpfs'
    finally:
        os.close(dead_fd)
        os.close(live_lock)
        os.close(live_fd)


def test_each_sampler_measures_its_own_run(capacity):
    root, free = capacity
    runs = [new_run_id(), new_run_id()]
    for run in runs:
        scratch.prepare(root, run)
        os.close(scratch.child(root, run))
    paths = [scratch.path(root, run) for run in runs]
    (paths[0] / 'one').write_bytes(b'a' * 100)
    (paths[1] / 'two').write_bytes(b'b' * 9000)
    monitors = [scratch.Monitor(path) for path in paths]
    for monitor in monitors:
        monitor.check(force=True)
    assert [monitor.peak['files'] for monitor in monitors] == [1, 1]
    assert monitors[0].peak['file_bytes'] < monitors[1].peak['file_bytes']
    assert all(monitor.bound == scratch.BOUND for monitor in monitors)


def test_capacity_fallback_leaves_unused_candidate_bases_empty(capacity, monkeypatch):
    root, free = capacity
    first = new_run_id()
    assert scratch.prepare(root, first)['kind'] == 'tmpfs'
    bases = [root.project.parent / name for name in ('another-memory-base', 'last-memory-base')]
    for base in bases:
        base.mkdir()
    monkeypatch.setattr(scratch, 'candidates', lambda: bases)
    free.bytes = scratch.BOUND + scratch.MARGIN
    assert scratch.prepare(root, new_run_id())['kind'] == 'disk'
    assert all(list(base.iterdir()) == [] for base in bases)
    assert scratch.pending(root) == [first]
