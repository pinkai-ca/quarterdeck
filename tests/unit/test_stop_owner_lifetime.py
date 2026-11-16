"""Stop proof lines agree with the lifetime of private stand-in service owners."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import signal
import select
import subprocess
import sys
from types import SimpleNamespace

import pytest

from quarterdeck import lock_holders, servicectl, shutdown
from quarterdeck.ship import ShipError


HOLDER = r'''
import fcntl, os, signal, subprocess, sys
from contextlib import nullcontext
from pathlib import Path
from quarterdeck import servicectl, shutdown
runtime, place, response = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
exit_event = int(sys.argv[4])
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
        if response == 'release-tree':
            descendant = subprocess.Popen([sys.executable, '-c',
                "import signal; print('ready', flush=True); signal.pause()"], stdout=subprocess.PIPE, text=True)
            assert descendant.stdout.readline().strip() == 'ready'
            print(f'child {descendant.pid} {shutdown.process_start(descendant.pid)}', flush=True)
        if response.startswith('release'):
            lock.close()
            print('released', flush=True)
        else:
            print('ignored', flush=True)
    signal.signal(signal.SIGTERM, term)
    # Detached members of the scope, each started by a program that exits before the owner reports it holds.
    detached = [subprocess.run([sys.executable, '-c', sys.argv[5], kind], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, text=True, check=True).stdout.strip()
                for kind in sys.argv[6:]]
    print('held', flush=True)
    for line in detached:
        print(line, flush=True)
    # The owner remains alive across lock release until the driver explicitly
    # permits exit. A signal interrupt or a stdio EOF cannot end this wait.
    assert os.read(exit_event, 1) == b'x'
    os.close(exit_event)
lock.close()
'''


DETACH = r'''
import os, subprocess, sys
MEMBER = """
import os, signal, subprocess, sys
if sys.argv[1] == 'server':
    with open('/proc/self/comm', 'w') as stream:
        stream.write('tmux: server')  # only its name: never a real tmux
    pane = subprocess.Popen([sys.executable, '-c', 'import signal; signal.pause()'], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(os.getpid(), pane.pid, flush=True)
else:
    print(os.getpid(), flush=True)
signal.pause()
"""
member = subprocess.Popen([sys.executable, '-c', MEMBER, sys.argv[1]], stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
print(member.stdout.readline().strip(), flush=True)
'''


@contextmanager
def stand_in(tmp_path, *, place="current", response="ignore", detach=()):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    exit_read, exit_write = os.pipe()
    try:
        child = subprocess.Popen([sys.executable, "-c", HOLDER, str(runtime), place, response, str(exit_read),
                                  DETACH, *detach],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, pass_fds=(exit_read,),
                                 stderr=subprocess.PIPE, text=True, start_new_session=True)
    except BaseException:
        os.close(exit_write)
        raise
    finally:
        os.close(exit_read)
    child.exit_event = exit_write
    try:
        assert child.stdout.readline().strip() == "held"
        child.descendants = {}
        child.runtime = runtime
        yield runtime, child
    finally:
        if child.poll() is None:
            child.kill()
        try:
            child.communicate(timeout=20)
        finally:
            os.close(exit_write)


def release_owner(child):
    """The one explicit event that lets a released stand-in exit normally."""
    os.write(child.exit_event, b'x')
    child.wait(timeout=20)


def stopping_clock(monkeypatch, child, response="ignore"):
    """Advance the driver's clock; actual child transitions use pipe/exit events."""
    ticks = [0.0]
    signals = []
    kill = os.kill

    def send(pid, sig):
        from quarterdeck import stop_barrier
        assert pid == child.pid or pid in child.descendants
        assert stop_barrier.read(child.runtime).get(pid) == shutdown.process_start(pid)
        signals.append(sig)
        kill(pid, sig)
        if pid != child.pid:
            return
        if sig == signal.SIGTERM and response != "exit":
            line = child.stdout.readline().strip()
            if line.startswith("child "):
                _, descendant, start = line.split()
                child.descendants[int(descendant)] = start
                line = child.stdout.readline().strip()
            assert line == ("released" if response.startswith("release") else "ignored")
            assert child.poll() is None, "the owner waits for its explicit exit event"
        elif sig in (signal.SIGTERM, signal.SIGKILL):
            child.wait(timeout=20)

    def sleep(seconds):
        if response == "release-exit" and signal.SIGTERM in signals and child.poll() is None:
            release_owner(child)
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
        assert signals == [signal.SIGTERM, signal.SIGSTOP, signal.SIGKILL]
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


@pytest.mark.parametrize("response", ["release", "release-tree"])
def test_a_released_lock_does_not_prove_its_owner_has_ended(tmp_path, monkeypatch, response):
    with stand_in(tmp_path, response=response) as (runtime, child):
        expected = shutdown.owner(runtime, child.pid)
        assert expected is not None
        signals = stopping_clock(monkeypatch, child, response=response)
        result = servicectl.stop(runtime)
        assert result.startswith("service stopped")
        assert f"pid {child.pid}" in result
        assert "released its lock but did not exit; killed with its tree" in result
        assert signal.SIGKILL in signals
        assert child.poll() is not None and shutdown.process_gone(expected)
        assert not servicectl.locked(runtime)
        if response == "release-tree":
            assert child.descendants
            assert all(shutdown.process_gone(dict(pid=pid, start=start))
                       for pid, start in child.descendants.items())


def test_a_normal_exit_after_release_is_not_killed(tmp_path, monkeypatch):
    with stand_in(tmp_path, response="release-exit") as (runtime, child):
        signals = stopping_clock(monkeypatch, child, response="release-exit")
        result = servicectl.stop(runtime)
        assert result.startswith("service stopped")
        assert "killed" not in result
        assert signals == [signal.SIGTERM]
        assert child.poll() == 0 and not servicectl.locked(runtime)


def test_an_unconfirmed_exit_refuses_by_process_name(tmp_path, monkeypatch):
    from quarterdeck import install, stop_barrier, write_bound

    with stand_in(tmp_path, response="release") as (runtime, child):
        stopping_clock(monkeypatch, child, response="release")
        monkeypatch.setattr(write_bound, "kill", lambda *args, **kwargs: [])
        with pytest.raises(ShipError, match=f"service {child.pid} exit cannot be confirmed"):
            servicectl.stop(runtime)
        assert child.poll() is None and not servicectl.locked(runtime)
        assert stop_barrier.read(runtime)[child.pid] == shutdown.process_start(child.pid)
        with pytest.raises(ShipError, match=f"pid {child.pid} start"):
            install._restart_old(SimpleNamespace(runtime=runtime),
                                 {'pending': {'was_running': True}}, bootstrap=False)
        assert child.poll() is None


def test_a_live_child_barrier_holds_start_until_the_child_ends(tmp_path, monkeypatch):
    from quarterdeck import stop_barrier

    with stand_in(tmp_path) as (runtime, child):
        identity = {child.pid: shutdown.process_start(child.pid)}
        stop_barrier.retain(runtime, identity)
        ship = SimpleNamespace(runtime=runtime)
        with pytest.raises(ShipError, match=f"pid {child.pid} start {identity[child.pid]}"):
            servicectl.start(ship)
        child.kill()
        child.wait(timeout=20)
        # No service is raised: after the automatic clear, the ordinary already-up path supplies the proof.
        monkeypatch.setattr(servicectl, 'alive', lambda _: True)
        assert servicectl.start(ship).startswith('service alive')
        assert not stop_barrier.path(runtime).exists()


def test_a_child_discovered_during_the_kill_holds_both_restart_paths(tmp_path, monkeypatch):
    from quarterdeck import install, stop_barrier

    with stand_in(tmp_path, response="release-tree") as (runtime, child):
        stopping_clock(monkeypatch, child, response="release-tree")
        send = os.kill

        def temporarily_unavailable(pid, sig):
            if pid in child.descendants:
                raise PermissionError('synthetic child temporarily cannot be ended')
            send(pid, sig)

        monkeypatch.setattr(servicectl.os, 'kill', temporarily_unavailable)
        try:
            with pytest.raises(ShipError, match='exit cannot be confirmed'):
                servicectl.stop(runtime)
            assert child.poll() is not None
            assert child.descendants
            known = stop_barrier.read(runtime)
            assert all(known[pid] == start for pid, start in child.descendants.items())
            for start in (lambda: servicectl.start(SimpleNamespace(runtime=runtime)),
                          lambda: install._restart_old(SimpleNamespace(runtime=runtime),
                                   {'pending': {'was_running': True}}, bootstrap=False)):
                with pytest.raises(ShipError, match='service stop barrier') as error:
                    start()
                assert all(f'pid {pid} start {began}' in str(error.value)
                           for pid, began in child.descendants.items())
        finally:
            monkeypatch.setattr(servicectl.os, 'kill', send)
            for pid in child.descendants:
                descriptor = os.pidfd_open(pid)
                try:
                    send(pid, signal.SIGKILL)
                    # Wait for the exit event; the timeout is only a teardown guard.
                    assert select.select([descriptor], [], [], 20)[0]
                finally:
                    os.close(descriptor)
        stop_barrier.require_clear(runtime)
        assert not stop_barrier.path(runtime).exists()


def test_recovery_clears_a_barrier_whose_process_has_ended(tmp_path):
    from quarterdeck import install, stop_barrier

    with stand_in(tmp_path) as (runtime, child):
        stop_barrier.retain(runtime, {child.pid: shutdown.process_start(child.pid)})
        child.kill()
        child.wait(timeout=20)
        assert install._restart_old(SimpleNamespace(runtime=runtime),
                                    {'pending': {'was_running': False}}, bootstrap=False) == []
        assert not stop_barrier.path(runtime).exists()


def test_an_unreadable_barrier_holds_start_and_recovery_and_stays_visible(tmp_path):
    from quarterdeck import install, presentation, status_snapshot, stop_barrier, watch

    runtime = tmp_path / 'runtime'
    target = stop_barrier.path(runtime)
    target.parent.mkdir(parents=True)
    target.write_text('{')
    ship = SimpleNamespace(runtime=runtime)
    for action in (lambda: servicectl.start(ship),
                   lambda: install._restart_old(ship, {'pending': {'was_running': True}}, bootstrap=False)):
        with pytest.raises(ShipError, match='service stop barrier') as error:
            action()
        assert str(target) in str(error.value)
        assert target.read_text() == '{'
    lines = stop_barrier.status(runtime)
    assert len(lines) == 1 and str(target) in lines[0] and 'unreadable' in lines[0]
    facts = watch.Facts(service='down', waiting=0, oldest=0, blind=0, server_recovery=lines,
                        pending=['synthetic long detail ' * 30])
    assert lines[0] in facts.line(), 'the named barrier survives status overflow'
    assert presentation.cached_line(tmp_path) is None
    assert presentation.published_reading(tmp_path) == (None, None)
    assert status_snapshot.served(ship) is None


def test_collection_reads_the_standing_barrier(drydock, tmux):
    from quarterdeck import ship as shipmod, stop_barrier, watch

    ship = shipmod.load(drydock)
    target = stop_barrier.path(ship.runtime)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{')
    facts = watch.facts(ship, tmux)
    assert any(str(target) in line for line in facts.server_recovery)
    assert 'service stop barrier' in facts.line()
