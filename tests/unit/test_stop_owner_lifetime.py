"""Stop proof lines agree with the lifetime of private stand-in service owners."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

from quarterdeck import lock_holders, servicectl, shutdown
from quarterdeck.ship import ShipError


HOLDER = r'''
import fcntl, os, signal, sys
from contextlib import nullcontext
from pathlib import Path
from quarterdeck import servicectl, shutdown
runtime, place, response = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
if place == 'earlier':
    lock = open(runtime / 'service.lock', 'a')
    fcntl.flock(lock, fcntl.LOCK_EX)
    scope = nullcontext()
else:
    lock = servicectl.acquire_lock(runtime)
    scope = shutdown.Stop(runtime, lock).installed()
with scope:
    def term(*_):
        if response == 'exit':
            raise SystemExit(0)
        if response == 'release':
            lock.close()
            print('released', flush=True)
        else:
            print('ignored', flush=True)
    signal.signal(signal.SIGTERM, term)
    print('held', flush=True)
    sys.stdin.read(1)
lock.close()
'''


@contextmanager
def stand_in(tmp_path, *, place="current", response="ignore"):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    child = subprocess.Popen([sys.executable, "-c", HOLDER, str(runtime), place, response],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "held"
        yield runtime, child
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=20)


def stopping_clock(monkeypatch, child, response="ignore"):
    """Advance the driver's clock; actual child transitions use pipe/exit events."""
    ticks = [0.0]
    signals = []
    kill = os.kill

    def send(pid, sig):
        assert pid == child.pid
        signals.append(sig)
        kill(pid, sig)
        if sig == signal.SIGTERM and response != "exit":
            assert child.stdout.readline().strip() == ("released" if response == "release" else "ignored")
        else:
            child.wait(timeout=20)

    def sleep(seconds):
        ticks[0] += seconds

    monkeypatch.setattr(servicectl, "time", SimpleNamespace(monotonic=lambda: ticks[0], sleep=sleep))
    monkeypatch.setattr(servicectl.os, "kill", send)
    return signals


@pytest.mark.parametrize("place", ["current", "earlier"])
def test_ignoring_term_is_killed_before_the_proof_returns(tmp_path, monkeypatch, place):
    with stand_in(tmp_path, place=place) as (runtime, child):
        assert servicectl.holder(runtime) == child.pid
        signals = stopping_clock(monkeypatch, child)
        result = servicectl.stop(runtime)
        assert result.startswith("service killed")
        assert signals == [signal.SIGTERM, signal.SIGKILL]
        assert child.poll() is not None
        assert not servicectl.locked(runtime)


def test_a_missing_table_row_is_confirmed_by_the_owners_descriptor(tmp_path, monkeypatch):
    with stand_in(tmp_path) as (runtime, child):
        table, descriptors = lock_holders.table, lock_holders.descriptor_locks
        reads, confirmations = [], []

        def briefly_missing():
            reads.append(True)
            return "" if len(reads) == 1 else table()

        def confirmed(path, info):
            result = descriptors(path, info)
            confirmations.append(result)
            return result

        monkeypatch.setattr(lock_holders, "table", briefly_missing)
        monkeypatch.setattr(lock_holders, "nested_pid_namespace", lambda: False)
        monkeypatch.setattr(lock_holders, "descriptor_locks", confirmed)
        stopping_clock(monkeypatch, child)
        result = servicectl.stop(runtime)
        assert result.startswith("service killed")
        assert {child.pid} in confirmations
        assert child.poll() is not None and not servicectl.locked(runtime)


@pytest.mark.parametrize("uncertain", ["nested", "unreadable", "unnamed"])
def test_an_unreadable_holder_is_named_as_a_refusal(tmp_path, monkeypatch, uncertain):
    with stand_in(tmp_path) as (runtime, child):
        path = servicectl.lock_path(runtime)
        info = path.stat()

        def reading():
            if uncertain == "unreadable":
                raise OSError("synthetic table unavailable")
            if uncertain == "unnamed":
                device = f"{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}"
                return f"1: FLOCK ADVISORY WRITE 0 {device}:{info.st_ino} 0 EOF\n"
            return ""

        monkeypatch.setattr(lock_holders, "table", reading)
        monkeypatch.setattr(lock_holders, "nested_pid_namespace", lambda: True)
        with pytest.raises(ShipError, match="holder cannot be read") as error:
            servicectl.stop(runtime)
        assert str(path) in str(error.value)
        assert child.poll() is None and servicectl.locked(runtime)


def test_an_owner_that_ends_between_readings_is_not_running(tmp_path, monkeypatch):
    with stand_in(tmp_path, response="exit") as (runtime, child):
        holder = servicectl.holder
        kill = os.kill
        assert servicectl.alive(runtime)

        def ended_before_holder(where):
            kill(child.pid, signal.SIGTERM)
            child.wait(timeout=20)
            return holder(where)

        monkeypatch.setattr(servicectl, "holder", ended_before_holder)
        assert servicectl.stop(runtime) == "service was not running"
        assert child.poll() is not None and not servicectl.locked(runtime)


def test_a_released_lock_does_not_prove_its_owner_has_ended(tmp_path, monkeypatch):
    with stand_in(tmp_path, response="release") as (runtime, child):
        expected = shutdown.owner(runtime, child.pid)
        assert expected is not None
        stopping_clock(monkeypatch, child, response="release")
        try:
            result = servicectl.stop(runtime)
        except ShipError:
            return  # Refusing an owner that is still alive is an honest outcome.
        assert child.poll() is not None or not result.startswith(("service stopped", "service was not running"))
