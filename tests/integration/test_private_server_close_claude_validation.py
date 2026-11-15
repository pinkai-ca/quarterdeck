"""A private tmux server's close on a loaded machine: it waits for the server's own exit, asks its own recorded server
once by pid when kill-server has not ended it, never signals a process it cannot prove is that server, and still holds
with its reason, its socket retained, when the server will not end and cannot be proved for a signal.

Every server here is one this test starts and closes by its own -S socket. The bounds are shortened for each test alone.
"""
from __future__ import annotations

import os
from pathlib import Path
import signal
import time

import pytest

from quarterdeck import tmux as tmuxmod, tmux_socket
from quarterdeck.procstat import EXITED
from quarterdeck.tmux import Tmux


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(tmuxmod, 'TERM_AFTER', 0.3)
    monkeypatch.setattr(tmuxmod, 'SHUTDOWN_WAIT', 2.0)
    started = Tmux.private(Path(os.environ['TMUX_TMPDIR']))
    started.new_session('Synthetic')
    pid = started._owned.pid
    try:
        yield started
    finally:
        fields = tmux_socket.process_state(pid)
        if fields and fields[0] not in EXITED:
            os.kill(pid, signal.SIGCONT)
        if not started.close():
            os.kill(pid, signal.SIGKILL)  # this test's own server, proved by the pid it recorded at birth
            # Then its creator closes it, or the test that made it fails: a namespace is never left to the session.
            if not started.close():
                pytest.fail(f'private tmux cleanup held: {started.held}')


def alive(pid):
    fields = tmux_socket.process_state(pid)
    return bool(fields) and fields[0] not in EXITED


def test_a_server_slow_to_end_is_waited_for_until_it_exits(server, monkeypatch):
    pid = server._owned.pid
    real = tmux_socket.OwnedSocket.ended
    slow_until = time.monotonic() + 2.5  # past the old 2 s bound
    monkeypatch.setattr(tmuxmod, 'SHUTDOWN_WAIT', 6.0)
    monkeypatch.setattr(tmuxmod, 'TERM_AFTER', 5.0)
    monkeypatch.setattr(tmux_socket.OwnedSocket, 'ended', lambda self: real(self) and time.monotonic() >= slow_until)
    assert server.close(), server.held
    assert not alive(pid) and not Path(server.socket).exists()


def test_a_server_kill_server_did_not_end_is_asked_once_by_its_own_pid(server, monkeypatch):
    pid = server._owned.pid
    original = Tmux._run
    asked = []

    def deaf(self, *args, **kwargs):
        if 'kill-server' in args:
            return ''  # the server is not reached by kill-server, as a loaded one may not be in time
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Tmux, '_run', deaf)
    terminate = tmux_socket.OwnedSocket.terminate
    monkeypatch.setattr(tmux_socket.OwnedSocket, 'terminate', lambda self: asked.append(self.pid) or terminate(self))
    assert server.close(), server.held
    assert asked == [pid], 'asked once, its own recorded server'
    assert not alive(pid) and not Path(server.socket).exists()


def test_a_process_not_proved_to_be_the_server_is_never_signalled(server, monkeypatch):
    pid = server._owned.pid
    original = Tmux._run
    monkeypatch.setattr(Tmux, '_run', lambda self, *args, **kwargs: '' if 'kill-server' in args
                        else original(self, *args, **kwargs))
    recorded = server._owned.start
    server._owned.start = str(int(recorded) + 1)  # the pid no longer proves the recorded process
    try:
        assert server._owned.terminate() is False, 'no signal to a process the record does not prove'
        assert not server.close() and 'retained' in server.held, 'held with its reason'
        assert alive(pid) and Path(server.socket).exists(), 'nothing it could not prove was touched'
    finally:
        server._owned.start = recorded
        monkeypatch.setattr(Tmux, '_run', original)
