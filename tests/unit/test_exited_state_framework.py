"""The framework reads a process caught as it is reaped as ended.

/proc shows an exited process as Z, a zombie, until its parent reaps it, then as X (x before Linux 4.14) for the
instant of its reaping, then not at all. A read that lands in the X instant was once taken by the framework for a
live process: the close of a private tmux server whose server had ended held it as 'still running', and the service's
shutdown counted its own ended process as present. Every reader here takes the states from quarterdeck.procstat.

The one server here is a private one this test starts, reached by its own -S socket; nothing else is signalled.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from quarterdeck import inbox_watch, shutdown, tmux as tmuxmod, tmux_socket
from quarterdeck.procstat import EXITED
from quarterdeck.tmux import Tmux

LIVING = ['R', 'S', 'D', 'T', 't', 'I']


def fields_in(state, start='4242'):
    """A /proc stat row after the command, in ``state``, with ``start`` as the kernel start marker (field 19)."""
    fields = [state, '1', '77', '77'] + ['0'] * 26
    fields[19] = start
    return fields


class StatRow:
    """Stands in for pathlib.Path in a module under test: every /proc read answers ``row``."""

    def __init__(self, row):
        self.row = row

    def __call__(self, path):
        return self

    def read_text(self):
        return '4242 (synthetic) ' + ' '.join(self.row)


def recorded():
    owned = object.__new__(tmux_socket.OwnedSocket)
    owned.pid, owned.start, owned.socket_fd = 4242, '4242', None
    return owned


def test_the_exited_states_are_z_and_both_spellings_of_x():
    assert EXITED == {'Z', 'X', 'x'}


@pytest.mark.parametrize('state', sorted(EXITED))
def test_a_private_server_in_an_exited_state_has_ended(monkeypatch, state):
    monkeypatch.setattr(tmux_socket, 'process_state', lambda pid: fields_in(state))
    owned = recorded()
    assert owned.ended()
    assert owned.terminate() is False  # never asks an ended process to end


@pytest.mark.parametrize('state', LIVING)
def test_a_private_server_living_with_its_own_start_has_not_ended(monkeypatch, state):
    monkeypatch.setattr(tmux_socket, 'process_state', lambda pid: fields_in(state))
    assert not recorded().ended()


@pytest.mark.parametrize('state', sorted(EXITED))
def test_the_service_reads_its_exited_process_as_gone(monkeypatch, state):
    monkeypatch.setattr(shutdown, 'Path', StatRow(fields_in(state)))
    assert shutdown.process_start(4242) is None
    assert shutdown.process_gone({'pid': 4242, 'start': '4242'})


@pytest.mark.parametrize('state', LIVING)
def test_the_service_reads_its_living_process_as_present(monkeypatch, state):
    monkeypatch.setattr(shutdown, 'Path', StatRow(fields_in(state)))
    assert shutdown.process_start(4242) == '4242'
    assert not shutdown.process_gone({'pid': 4242, 'start': '4242'})


@pytest.mark.parametrize('state', sorted(EXITED))
def test_an_inbox_watch_in_an_exited_state_is_no_process(monkeypatch, state):
    monkeypatch.setattr(inbox_watch, 'Path', StatRow(fields_in(state)))
    assert inbox_watch.process(4242) is None


def test_a_private_server_caught_as_it_is_reaped_closes_not_held(monkeypatch):
    """The reproduction: this test's own private server has ended, but every read of its process lands in the
    reaping instant, X. The close completes and removes the endpoint, as it does for a zombie or a vanished process."""
    monkeypatch.setattr(tmuxmod, 'TERM_AFTER', 0.2)
    monkeypatch.setattr(tmuxmod, 'SHUTDOWN_WAIT', 1.0)
    server = Tmux.private(Path(os.environ['TMUX_TMPDIR']))
    try:
        server.new_session('reaped')
        pid, start, socket = server._owned.pid, server._owned.start, Path(server.socket)
        subprocess.run(['tmux', '-S', str(socket), '-N', 'kill-server'], check=True, timeout=5)
        real = tmux_socket.process_state
        monkeypatch.setattr(tmux_socket, 'process_state',
                            lambda asked: fields_in('X', start) if asked == pid else real(asked))
        assert server.close(), server.held
        assert not socket.exists()
    finally:
        monkeypatch.undo()
        if not server._closed:
            fields = tmux_socket.process_state(server._owned.pid) if server._owned.pid else None
            if fields and fields[0] not in EXITED:
                subprocess.run(['tmux', '-S', server.socket, '-N', 'kill-server'],
                               capture_output=True, timeout=5, check=False)
            if not server.close():
                pytest.fail(f'private tmux cleanup held: {server.held}')
