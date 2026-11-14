"""Every tmux client command to a private server carries PRIVATE_COMMAND_WAIT as its timeout and fails as unanswered
past it; a ship's server, tmux's default and a named -L server are not bounded here."""
from __future__ import annotations

import subprocess

import pytest

from quarterdeck import tmux as tmuxmod
from quarterdeck.tmux import PrivateTmuxError, Tmux, TmuxError, TmuxUnanswered


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def run(command, **kwargs):
        seen.append(kwargs.get('timeout'))
        return subprocess.CompletedProcess(command, 0, '', '')
    monkeypatch.setattr(tmuxmod.subprocess, 'run', run)
    return seen


def test_a_private_command_is_bounded_and_others_are_not(calls, tmp_path):
    Tmux(str(tmp_path / 's'))._run('list-sessions')
    Tmux(str(tmp_path / 'ship.sock'), ship=True)._run('list-sessions')
    Tmux(str(tmp_path / 'old.sock'), legacy=True)._run('list-sessions')
    Tmux('named')._run('list-sessions')
    Tmux()._run('list-sessions')
    assert calls == [tmuxmod.PRIVATE_COMMAND_WAIT, None, None, None, None]


def test_an_unanswered_private_command_fails_as_unanswered_and_private(monkeypatch, tmp_path):
    def run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs['timeout'])
    monkeypatch.setattr(tmuxmod.subprocess, 'run', run)
    with pytest.raises(TmuxUnanswered) as caught:
        Tmux(str(tmp_path / 's'))._run('capture-pane', '-p')
    assert isinstance(caught.value, PrivateTmuxError) and isinstance(caught.value, TmuxError)
    assert str(caught.value) == ('tmux capture-pane: private server did not answer within 10 s (stopped or hung); '
                                 'retained')


def test_the_bound_outlasts_the_namespace_lock_budget():
    from quarterdeck.tmux_socket import EXCLUSIVE_WAIT
    assert tmuxmod.PRIVATE_COMMAND_WAIT >= 5 * EXCLUSIVE_WAIT
