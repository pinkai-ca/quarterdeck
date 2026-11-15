"""A service is known by the holder of its lock, never by a heartbeat's word.

A dead service's last beat outlives it, and after a sync back to an earlier copy the beat a reader finds may name a
process that is no longer the service. Here a synthetic service is a child of the test that holds the ship's lock; the
other processes are the test's own children too, and only the lock's holder may be signalled.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
import time

import pytest

from quarterdeck import servicectl

HOLD = r"""
import sys, time
from pathlib import Path
from quarterdeck import servicectl
lock = servicectl.acquire_lock(Path(sys.argv[1]))
print('held', flush=True)
time.sleep(120)
"""

# An earlier copy's service, from before the lock moved under runtime/locks: it holds the lock's earlier place.
HOLD_EARLIER = r"""
import fcntl, os, sys, time
from pathlib import Path
lock = os.open(Path(sys.argv[1]) / 'service.lock', os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
print('held', flush=True)
time.sleep(120)
"""


@pytest.fixture
def runtime(tmp_path):
    folder = tmp_path / 'runtime'
    folder.mkdir()
    return folder


def child(*argv):
    return subprocess.Popen([sys.executable, *argv], stdout=subprocess.PIPE, text=True)


def holding(runtime, script=HOLD):
    process = child('-c', script, str(runtime))
    assert process.stdout.readline().strip() == 'held'
    return process


def end(process):
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


@pytest.fixture
def service(runtime):
    """A process that holds the ship's lock, as a service does for its whole life; SIGTERM ends it."""
    process = holding(runtime)
    try:
        yield process
    finally:
        end(process)


@pytest.fixture
def bystander():
    """A live process of the test's own that is not the service."""
    process = child('-c', 'import time; time.sleep(120)')
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)


def beat(runtime, pid, age=0.0):
    path = servicectl.heartbeat_path(runtime)
    path.parent.mkdir(exist_ok=True)
    path.write_text(f'{pid} {(datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()}\n')


def dead_pid():
    done = subprocess.run([sys.executable, '-c', 'import os; print(os.getpid())'], capture_output=True, text=True)
    return int(done.stdout)


def test_the_holder_is_the_process_that_holds_the_lock(runtime, service):
    assert servicectl.holder(runtime) == service.pid


def test_right_after_a_kill_a_dead_services_leftover_beat_never_reads_as_a_start(runtime):
    first = holding(runtime)
    try:
        beat(runtime, first.pid)
        assert servicectl.started(runtime)
        first.kill()  # killed: its last beat stays behind, fresh
        first.wait(timeout=10)
        assert not servicectl.alive(runtime) and not servicectl.started(runtime)
        nxt = holding(runtime)  # the next service holds the lock and has not beaten yet
        try:
            assert servicectl.alive(runtime) and servicectl.read_heartbeat(runtime)[0] == first.pid
            assert not servicectl.started(runtime), "the killed service's last beat read as the next one's start"
            beat(runtime, nxt.pid)
            assert servicectl.started(runtime)
        finally:
            end(nxt)
    finally:
        end(first)


@pytest.mark.parametrize('script', [HOLD, HOLD_EARLIER], ids=['lock', 'earlier-lock-place'])
@pytest.mark.parametrize('read', ['another', 'none'])
def test_stop_after_a_back_to_an_earlier_copy_finds_the_live_service(runtime, bystander, script, read):
    """After a sync back to an earlier copy the beat a reader finds may name a process that is not the service, or
    there may be none where this code reads: stop signals the lock's holder alone, at whichever place it holds it."""
    service = holding(runtime, script)
    try:
        if read == 'another':
            beat(runtime, bystander.pid)
        assert servicectl.holder(runtime) == service.pid
        result = servicectl.stop(runtime)
        assert result.startswith(f'service stopped · pid {service.pid}'), result
        assert service.wait(timeout=10) is not None and not servicectl.alive(runtime)
        time.sleep(0.2)
        assert bystander.poll() is None, 'the process the beat named was never signalled'
    finally:
        end(service)


def test_no_holder_when_no_process_holds_the_lock(runtime, service):
    service.terminate()
    service.wait(timeout=10)
    assert servicectl.holder(runtime) is None and not servicectl.alive(runtime)
    assert servicectl.stop(runtime) == 'service was not running'
