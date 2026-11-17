"""Private tmux servers a test starts, each kept by the pid it ran as and that pid's start time, and ended by them.

A test that starts a tmux server must end it, and ending it through its socket fails whenever the socket is gone (a
test may unlink it; a busy machine may not have made it again), and a SIGTERM alone fails a stopped server. So a server
is kept as it starts: its pid, read from the server itself on its own private socket, and that process's start time
(field 22 of /proc/<pid>/stat). close() ends each by that pid, never through its socket, and only while the pid is
still that server: the same pid with the same start time, read again just before each signal, so a reused pid is never
signalled and no other server is ever named. SIGCONT (a stopped server wakes), SIGTERM, a wait on its exit up to
SAFETY; past it SIGKILL. It returns one line per server, and raises if any is still alive.

Private sockets only, as the standing orders require: every server here is on an explicit -S socket the test chose,
never tmux's default server and never a bare kill-server.
"""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import time

from tests.testrunner import procs

SAFETY = 30.0  # seconds a server has to end after SIGTERM before SIGKILL: far above any stall of a busy machine


def tmux_env(env: dict | None = None) -> dict:
    """The environment for a test's own server: never the caller's server through TMUX or TMUX_PANE."""
    return {key: value for key, value in (os.environ if env is None else env).items()
            if key not in ('TMUX', 'TMUX_PANE')}


class OwnServers:
    """The tmux servers one test started, ended in the end by the pids it kept (close)."""

    def __init__(self):
        self.kept: list[dict] = []

    def start(self, socket, *args, env: dict | None = None) -> dict:
        """Start a server on this private socket with ``tmux -S <socket> -f /dev/null new-session -d <args>``, and keep
        it."""
        private(socket)
        subprocess.run(['tmux', '-S', str(socket), '-f', '/dev/null', 'new-session', '-d', *args],
                       env=tmux_env(env), check=True, capture_output=True, timeout=10)
        return self.keep(socket, env=env)

    def keep(self, socket, env: dict | None = None) -> dict:
        """Keep the server answering on this private socket, which the test started some other way: its pid, read from
        the server itself, and that pid's start time."""
        private(socket)
        pid = int(subprocess.run(['tmux', '-S', str(socket), 'display-message', '-p', '#{pid}'],
                                 env=tmux_env(env), capture_output=True, text=True, check=True,
                                 timeout=10).stdout)
        server = {'pid': pid, 'start': procs.start_time(pid), 'socket': str(socket)}
        self.kept.append(server)
        return server

    def close(self) -> list[str]:
        """End each kept server by its pid, never through its socket; what was done, one line a server."""
        done = []
        for server in self.kept:
            pid, identity = server['pid'], {server['pid']: server['start']}
            if not procs.alive(identity):
                done.append(f'{pid}: already gone')
                continue
            for sig in (signal.SIGCONT, signal.SIGTERM):
                if procs.alive(identity):
                    os.kill(pid, sig)
            deadline = time.monotonic() + SAFETY
            while procs.alive(identity) and time.monotonic() < deadline:
                time.sleep(0.02)
            if not procs.alive(identity):
                done.append(f'{pid}: ended by SIGTERM')
                continue
            if procs.alive(identity):
                os.kill(pid, signal.SIGKILL)
            for _ in range(250):  # a fixed count, not the clock: SIGKILL cannot be refused, only take a moment
                if not procs.alive(identity):
                    break
                time.sleep(0.02)
            done.append(f'{pid}: killed past {SAFETY:g}s')
        left = [server['pid'] for server in self.kept if procs.alive({server['pid']: server['start']})]
        if left:
            raise AssertionError(f'test tmux servers still alive after SIGKILL: {left}')
        return done


def gone(server: dict) -> bool:
    return not procs.alive({server['pid']: server['start']})


def private(socket) -> None:
    """A test's server is on an explicit socket path of its own, never in tmux's default folder."""
    path = Path(socket)
    if not path.is_absolute() or path.parent.name == f'tmux-{os.getuid()}':
        raise ValueError(f'{socket}: not a private socket of the test')
