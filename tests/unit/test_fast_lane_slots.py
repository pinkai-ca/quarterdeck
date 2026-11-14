"""Ordinary scheduling on private synthetic slot files, never the project's live slots."""
from contextlib import contextmanager
import multiprocessing
import os
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from tests.testrunner import layout


@contextmanager
def private_pool(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    pool = None
    try:
        pool = layout.Slot(SimpleNamespace(fd=fd, path=Path(path)))
        yield pool
    finally:
        if pool is not None:
            pool.release()
        os.close(fd)


def hold_slots(path, count, release, connection, notify_wait):
    if notify_wait:
        def sleeping(seconds):
            connection.send(('waiting', None))
            time.sleep(seconds)

        layout.time = SimpleNamespace(monotonic=time.monotonic, strftime=time.strftime, sleep=sleeping)
    try:
        with private_pool(path) as pool:
            connection.send(('requesting', None))
            pool.acquire({'verb': 'ordinary-example'}, wait=15, count=count)
            connection.send(('ready', len(pool.held)))
            if not release.wait(30):
                raise RuntimeError('ordinary holder was not released')
    except BaseException as error:
        connection.send(('error', (type(error).__name__, str(error))))
        raise
    finally:
        connection.close()


def receive(connection, wanted):
    deadline = time.monotonic() + 15
    while True:
        assert connection.poll(max(0, deadline - time.monotonic())), wanted
        kind, value = connection.recv()
        assert kind != 'error', value
        if kind == wanted:
            return value
        assert kind in ('requesting', 'waiting'), (kind, value)


@pytest.fixture
def holders(tmp_path):
    path = tmp_path / 'ordinary-slots'
    path.mkdir(mode=0o700)
    context = multiprocessing.get_context('spawn')
    children = []

    def start(count, *, waiting=False):
        reader, writer = context.Pipe(duplex=False)
        release = context.Event()
        process = context.Process(target=hold_slots, args=(path, count, release, writer, waiting))
        process.start()
        writer.close()
        child = SimpleNamespace(process=process, release=release, reader=reader)
        children.append(child)
        receive(reader, 'requesting')
        return child

    yield path, start
    # A terminated holder must not have its Event touched: a killed waiter can own its semaphore.
    for child in children:
        if child.process.is_alive():
            child.release.set()
    for child in children:
        child.process.join(10)
        if child.process.is_alive():
            child.process.kill()
            child.process.join(5)
            pytest.fail('an owned ordinary holder did not finish')
        child.reader.close()


def test_two_fast_runs_fill_the_four_slots(holders):
    path, start = holders
    first, second = start(2), start(2)
    assert receive(first.reader, 'ready') == 2
    assert receive(second.reader, 'ready') == 2
    with private_pool(path) as probe:
        with pytest.raises(layout.Refused):
            probe.acquire({'verb': 'ordinary-extra'})
        assert probe.held == []


def test_a_fast_and_two_whole_runs_fit_together(holders):
    path, start = holders
    fast = start(2)
    assert receive(fast.reader, 'ready') == 2
    first, second = start(1), start(1)
    assert receive(first.reader, 'ready') == 1
    assert receive(second.reader, 'ready') == 1
    with private_pool(path) as probe:
        with pytest.raises(layout.Refused):
            probe.acquire({'verb': 'ordinary-extra'})


def test_a_third_fast_waits_and_starts_when_two_slots_return(holders):
    path, start = holders
    first, second = start(2), start(2)
    assert receive(first.reader, 'ready') == 2
    assert receive(second.reader, 'ready') == 2
    third = start(2, waiting=True)
    receive(third.reader, 'waiting')
    first.release.set()
    first.process.join(10)
    assert first.process.exitcode == 0
    assert receive(third.reader, 'ready') == 2
    with private_pool(path) as probe:
        with pytest.raises(layout.Refused):
            probe.acquire({'verb': 'ordinary-extra'})


def test_waiting_for_two_never_retains_the_one_free_slot(holders):
    path, start = holders
    fast, whole = start(2), start(1)
    assert receive(fast.reader, 'ready') == 2
    assert receive(whole.reader, 'ready') == 1
    waiting = start(2, waiting=True)
    receive(waiting.reader, 'waiting')
    with private_pool(path) as probe:
        probe.acquire({'verb': 'ordinary-free-slot'})
        assert len(probe.held) == 1
    whole.release.set()
    whole.process.join(10)
    assert whole.process.exitcode == 0
    assert receive(waiting.reader, 'ready') == 2


def test_a_dead_fast_run_returns_both_slots(holders):
    path, start = holders
    fast = start(2)
    assert receive(fast.reader, 'ready') == 2
    first, second = start(1), start(1)
    assert receive(first.reader, 'ready') == 1
    assert receive(second.reader, 'ready') == 1
    fast.process.terminate()
    fast.process.join(10)
    assert fast.process.exitcode is not None
    assert fast.process.exitcode != 0
    replacement = start(2)
    assert receive(replacement.reader, 'ready') == 2
    with private_pool(path) as probe:
        with pytest.raises(layout.Refused):
            probe.acquire({'verb': 'ordinary-extra'})
