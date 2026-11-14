"""A private tmux server that does not answer: stopped (SIGSTOP) or hung, it accepts a client's connection and never
replies. Every client command to it fails as unanswered within PRIVATE_COMMAND_WAIT instead of blocking forever; close
holds the server with that reason, its socket retained, and signals nothing; once the server is resumed, the same
creator closes it as usual.

Every server here is this test's own, started and ended by its own -S socket. The bound is shortened for the test alone.
"""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import time

import pytest

from quarterdeck import tmux as tmuxmod, tmux_socket
from quarterdeck.tmux import Tmux, TmuxUnanswered

BOUND = 1.0


def state(pid):
    fields = tmux_socket.process_state(pid)
    return fields[0] if fields else None


def pending_signals(pid):
    """The process's pending signal masks, private and shared: a signal sent to a stopped process waits here."""
    lines = Path(f'/proc/{pid}/status').read_text().splitlines()
    return {line.split(':')[0]: int(line.split()[1], 16) for line in lines if line.startswith(('SigPnd:', 'ShdPnd:'))}


def wait_for(predicate, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        time.sleep(.01)
    return True


@pytest.fixture
def stopped(tmp_path, monkeypatch):
    monkeypatch.setattr(tmuxmod, 'PRIVATE_COMMAND_WAIT', BOUND)
    server = Tmux.private(Path(os.environ['TMUX_TMPDIR']))
    server.new_session('Synthetic')
    pid, socket = server._owned.pid, server.socket
    os.kill(pid, signal.SIGSTOP)  # this test's own server, by the pid it recorded at birth
    try:
        assert wait_for(lambda: state(pid) == 'T'), state(pid)
        yield server
    finally:
        if state(pid) not in (None, 'Z', 'X', 'x'):
            os.kill(pid, signal.SIGCONT)
        if not server.close():
            # Still held: end this test's own server by its own socket, never through the ship's server or tmux's default.
            subprocess.run(['tmux', '-S', socket, '-N', '-f', '/dev/null', 'kill-server'],
                           capture_output=True, timeout=10, check=False)


def test_close_of_a_stopped_server_returns_within_the_bound_and_holds_it_signalling_nothing(stopped, monkeypatch):
    pid, socket = stopped._owned.pid, stopped.socket
    asked = []
    monkeypatch.setattr(tmux_socket.OwnedSocket, 'terminate', lambda self: asked.append(self.pid) or False)

    started = time.monotonic()
    closed = stopped.close()
    took = time.monotonic() - started

    assert not closed
    # One unanswered kill-server, under one exclusive lock acquisition: the bound plus slack, never forever.
    assert took < BOUND + tmux_socket.EXCLUSIVE_WAIT + 1.0, took
    assert took >= BOUND * 0.9, took
    assert 'did not answer within 1 s (stopped or hung)' in stopped.held, stopped.held
    assert f'endpoint {socket}' in stopped.held
    assert Path(socket).exists() and stopped._owned.socket_fd is not None  # retained, ownership kept
    assert state(pid) == 'T'
    assert asked == []  # not asked by pid either
    assert pending_signals(pid) == {'SigPnd': 0, 'ShdPnd': 0}  # nothing was sent to it


def test_a_stopped_server_is_never_read_as_absent(stopped):
    started = time.monotonic()
    with pytest.raises(TmuxUnanswered, match='did not answer within 1 s'):
        stopped.running()
    with pytest.raises(TmuxUnanswered):
        stopped.sessions()
    with pytest.raises(TmuxUnanswered):
        stopped.pane('%0')
    assert time.monotonic() - started < 3 * (BOUND + 1.0)

    # A bound observer of the same endpoint is bounded too.
    observer = Tmux(stopped.socket)
    with pytest.raises(TmuxUnanswered):
        observer.running()


def test_the_held_server_resumed_is_closed_by_its_creator(stopped):
    pid, socket = stopped._owned.pid, stopped.socket
    assert not stopped.close()
    assert 'did not answer' in stopped.held

    os.kill(pid, signal.SIGCONT)
    assert wait_for(lambda: state(pid) != 'T')
    assert stopped.close(), stopped.held
    assert wait_for(lambda: state(pid) in (None, 'Z', 'X', 'x'))
    assert not Path(socket).exists()
