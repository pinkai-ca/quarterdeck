"""A stop run again: the live service still gets SIGTERM and its grace, an unreadable barrier says what settles it,
and a tmux server an older stop recorded never holds the barrier."""

from __future__ import annotations

import signal
import subprocess
import sys

import pytest

from quarterdeck import servicectl, shutdown, stop_barrier
from quarterdeck.ship import ShipError
from tests.unit.test_service_processes import _end
from tests.unit.test_stop_owner_lifetime import stand_in, stopping_clock

SLEEPER = 'import signal; signal.pause()'
SERVER = r'''
import signal, subprocess, sys
with open('/proc/self/comm', 'w') as stream:
    stream.write('tmux: server')  # only its name: never a real tmux
pane = subprocess.Popen([sys.executable, '-c', 'import signal; signal.pause()'], stdin=subprocess.DEVNULL)
print(pane.pid, flush=True)
signal.pause()
'''


def annotations(monkeypatch):
    seen, real = [], shutdown.annotate

    def annotate(runtime, expected, *, forced):
        seen.append((expected['pid'] if expected else None, forced))
        return real(runtime, expected, forced=forced)
    monkeypatch.setattr(shutdown, 'annotate', annotate)
    return seen


@pytest.mark.parametrize('leftover', [False, True])
def test_a_stop_run_again_while_its_owner_lives_gives_sigterm_first_and_annotates(tmp_path, monkeypatch, leftover):
    with stand_in(tmp_path, response='exit') as (runtime, child):
        start = shutdown.process_start(child.pid)
        # An earlier stop recorded the owner, then was interrupted before any signal: the owner still holds the lock.
        recorded = {child.pid: start}
        other = None
        if leftover:  # and one process of its own, outside the owner's tree and session, that it never ended
            other = subprocess.Popen([sys.executable, '-c', SLEEPER], start_new_session=True,
                                     stdin=subprocess.DEVNULL)
            recorded[other.pid] = shutdown.process_start(other.pid)
            child.descendants[other.pid] = recorded[other.pid]  # the one other pid the stop may signal
        try:
            stop_barrier.retain(runtime, recorded)
            assert stop_barrier.remaining(stop_barrier.read(runtime)) == recorded
            seen = annotations(monkeypatch)
            signals = stopping_clock(monkeypatch, child, response='exit')
            result = servicectl.stop(runtime)
            if leftover:
                other.wait(timeout=20)  # its exit is the event; the timeout only guards the test
                assert signals == [signal.SIGSTOP, signal.SIGKILL, signal.SIGTERM], 'the leftover at once, then TERM'
                assert f"an earlier stop's leftovers ended: pid {other.pid} start {recorded[other.pid]}" in result
            else:
                assert signals == [signal.SIGTERM], 'the owner is never stopped or killed first'
            assert result.startswith(f'service stopped · pid {child.pid}') and 'killed' not in result.split(' · ')[0]
            assert seen == [(child.pid, False)], 'annotated as an ordinary stop, not a forced one'
            assert child.poll() == 0 and not servicectl.locked(runtime)
            assert not stop_barrier.path(runtime).exists()
        finally:
            if other is not None and other.poll() is None:
                other.kill()
                other.wait(timeout=20)


def test_an_unreadable_barrier_names_its_file_and_what_settles_it(tmp_path):
    runtime = tmp_path / 'runtime'
    target = stop_barrier.path(runtime)
    target.parent.mkdir(parents=True)
    target.write_text('{')
    with pytest.raises(ShipError) as refused:
        servicectl.stop(runtime)
    words = str(refused.value)
    assert words == (f'service stop barrier {target} unreadable (JSONDecodeError); stop and start held: '
                     'qd service --settle-barrier moves it aside unchanged once no process an earlier stop '
                     'recorded can be running, then stop or start again')
    assert target.read_text() == '{', 'never edited'
    target.write_text('{"version": 1, "processes": []}')
    with pytest.raises(ShipError, match=r'unreadable \(unknown identities\)'):
        servicectl.stop(runtime)


def test_a_tmux_server_an_older_stop_recorded_never_holds_the_barrier(tmp_path):
    runtime = tmp_path / 'runtime'
    stop_barrier.path(runtime).parent.mkdir(parents=True)
    server = subprocess.Popen([sys.executable, '-c', SERVER], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              text=True, start_new_session=True)
    pane = int(server.stdout.readline())
    members = {server.pid: shutdown.process_start(server.pid), pane: shutdown.process_start(pane)}
    try:
        assert (servicectl.Path('/proc') / str(server.pid) / 'comm').read_text() == 'tmux: server\n'
        stop_barrier.retain(runtime, members)  # as the code before the spare wrote it: the server and its pane
        assert stop_barrier.remaining(members) == {} and stop_barrier.spared(members) == members
        assert stop_barrier.status(runtime) == ['service stop barrier: recorded processes ended; next start clears it']
        result = servicectl.stop(runtime)
        assert result == (f'service was not running · an earlier stop recorded a tmux server (pid {server.pid}); '
                          'left running with its panes')
        assert not stop_barrier.path(runtime).exists()
        assert all(not shutdown.process_gone(dict(pid=pid, start=start)) for pid, start in members.items())
    finally:
        for pid in (pane, server.pid):
            _end(pid, members[pid])
        server.wait(timeout=20)
