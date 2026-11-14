"""Ordinary admission/registration concurrency: invented runs, no program or protection probe."""
import multiprocessing
import os
from pathlib import Path
from queue import Empty

import pytest

from tests.testrunner import layout


def root_at(folder):
    folder = Path(folder)
    folder.mkdir(mode=0o700, exist_ok=True)
    root = layout.Root.__new__(layout.Root)
    root.path = root.project = folder
    root.fd = os.open(folder, layout.DIRECTORY)
    root.device = os.fstat(root.fd).st_dev
    return root


def holding(folder, name, entered, release, waiting=None):
    root = root_at(folder)
    pool = layout.Slot(root)
    if waiting is not None:
        original = layout.time.sleep
        def observed(seconds):
            waiting.set()
            original(seconds)
        layout.time.sleep = observed
    try:
        pool.acquire({'run': name}, wait=15)
        entered.put((name, pool.selected))
        if not release.wait(15):
            raise AssertionError('synthetic holder was not released')
    finally:
        pool.release()
        root.close()


@pytest.fixture
def holders(tmp_path):
    context = multiprocessing.get_context('spawn')
    entered = context.Queue()
    workers = []
    def start(name, waiting=None):
        stop = context.Event()
        p = context.Process(target=holding, args=(str(tmp_path), name, entered, stop, waiting))
        p.start()
        workers.append((p, stop))
        return p, stop
    yield context, entered, start, workers
    for p, stop in workers:
        stop.set()
    for p, stop in workers:
        p.join(5)
        if p.is_alive():
            p.terminate()
            p.join(5)
        assert not p.is_alive()
    entered.close()
    entered.join_thread()


def test_four_runs_hold_distinct_slots_and_fifth_waits_then_enters(holders):
    context, entered, start, workers = holders
    for i in range(4):
        start(f'synthetic-run-{i}')
    first = [entered.get(timeout=10) for _ in range(4)]
    assert {slot for name, slot in first} == {0, 1, 2, 3}
    assert all(p.is_alive() for p, stop in workers)
    waiting = context.Event()
    fifth, fifth_stop = start('synthetic-fifth', waiting)
    assert waiting.wait(10), 'the fifth must encounter occupied locks and wait'
    with pytest.raises(Empty):
        entered.get_nowait()
    released, stop = workers[0]
    freed = dict(first)['synthetic-run-0']
    stop.set()
    released.join(5)
    assert released.exitcode == 0
    assert entered.get(timeout=5) == ('synthetic-fifth', freed)
    assert sum(p.is_alive() for p, stop in workers) == 4


def test_a_dead_run_returns_only_its_slot(holders):
    context, entered, start, workers = holders
    for i in range(4):
        start(f'synthetic-run-{i}')
    first = dict(entered.get(timeout=10) for _ in range(4))
    dead, stop = workers[0]
    dead.terminate()
    dead.join(5)
    assert dead.exitcode is not None
    replacement, done = start('synthetic-replacement')
    assert entered.get(timeout=5) == ('synthetic-replacement', first['synthetic-run-0'])
    assert all(p.is_alive() for p, stop in workers[1:])


def test_refused_fifth_closes_its_files_without_clearing_other_holder_notes(tmp_path):
    root = root_at(tmp_path)
    pools = [layout.Slot(root) for _ in range(5)]
    try:
        for i, pool in enumerate(pools[:4]):
            pool.acquire({'run': f'synthetic-{i}'})
        before = [os.pread(pool.fd, 4096, 0) for pool in pools[:4]]
        with pytest.raises(layout.Refused, match='all 4 test slots are busy'):
            pools[4].acquire({'run': 'synthetic-refused'})
        pools[4].release()
        assert not pools[4].fds
        assert [os.pread(pool.fd, 4096, 0) for pool in pools[:4]] == before
    finally:
        for pool in pools:
            pool.release()
        root.close()


def test_result_directory_guards_are_per_run_and_cover_setup_before_state(tmp_path):
    root = root_at(tmp_path)
    first = layout.RunGuard(root, '20000101-000000-0001', create=True)
    second = layout.RunGuard(root, '20000101-000000-0002', create=True)
    competing = layout.RunGuard(root, first.run)
    try:
        first.acquire()
        second.acquire()
        assert layout.run_active(root, first.run)
        assert layout.run_active(root, second.run)
        assert not (tmp_path / 'results' / first.run / 'state.json').exists()
        with pytest.raises(layout.RunBusy):
            competing.acquire()
        first.release()
        first = None
        competing.acquire()
        assert layout.run_active(root, second.run)
    finally:
        if first is not None:
            first.release()
        second.release()
        competing.release()
        root.close()
