"""The ship's own tmux server is born in a systemd scope of its own, never in its caller's. No real scope or ship server."""

from __future__ import annotations

import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import servicectl, service_processes
from quarterdeck.jail_environment import unjailed_environment
from quarterdeck.tmux import Tmux


class Runner:
    """Every command Tmux would run, answered without running anything."""

    def __init__(self, *, server_up=False, manager=0):
        self.calls, self.server_up, self.manager = [], server_up, manager

    def __call__(self, command, **kwargs):
        self.calls.append((list(command), kwargs.get('env')))
        if command[-1] == 'list-sessions':
            code = 0 if self.server_up else 1
        elif command[-1] == 'true':  # the scope probe
            code = self.manager
        else:
            code = 0
        return SimpleNamespace(returncode=code, stdout='$1\n' if 'new-session' in command else '',
                               stderr='' if code == 0 else 'Failed to connect to bus')

    def commands(self, word):
        return [(command, env) for command, env in self.calls if word in command]


@pytest.fixture
def systemd_run(tmp_path, monkeypatch):
    """A systemd-run that is only ever found on PATH, never run: the runner answers in its place."""
    folder = tmp_path / 'bin'
    folder.mkdir()
    program = folder / 'systemd-run'
    program.write_text('#!/bin/sh\nexit 99\n')
    program.chmod(0o755)
    monkeypatch.setenv('PATH', f'{folder}{os.pathsep}{os.environ.get("PATH", "")}')
    return program


def ship_tmux(tmp_path, runner, **kwargs):
    tmux = Tmux(str(tmp_path / 'a-ship' / 'runtime' / 'tmux' / 'server'), ship=True, name='test ship', **kwargs)
    tmux._runner = runner
    return tmux


NEW = ('new-session', '-d', '-P', '-F', '#{session_id}', '-s', 'bridge', '-n', 'qd')


def test_the_ships_first_new_session_starts_its_server_in_a_scope_of_its_own(tmp_path, systemd_run):
    runner = Runner()
    tmux = ship_tmux(tmp_path, runner)
    assert tmux._run(*NEW) == '$1\n'
    [(command, env)] = runner.commands('new-session')
    tail = ['tmux', '-u', '-S', tmux.socket, *NEW]
    assert command[-len(tail):] == tail, 'the same tmux command, only wrapped'
    scope = command[:-len(tail)]
    assert scope[0] == str(systemd_run) and scope[-1] == '--'
    assert {'--user', '--scope', '--quiet', '--collect', '--expand-environment=no'} <= set(scope)
    unit = servicectl.scope_unit(scope)
    assert re.fullmatch(r'qd-tmux-server-test_ship-[0-9a-f]{8}\.scope', unit)
    assert not service_processes.UNIT.fullmatch(unit), "the stop's service pattern never matches the server's scope"
    assert '--description=qd tmux server of test ship' in scope
    assert env == unjailed_environment(os.environ), 'the declared environment, unchanged by the scope'
    [(probe, probe_env)] = runner.commands('true')
    assert probe[0] == str(systemd_run) and probe_env == env
    assert tmux.started_server == f'tmux server started in its own scope {unit}'


def test_a_new_session_on_a_running_server_is_not_wrapped(tmp_path, systemd_run):
    runner = Runner(server_up=True)
    tmux = ship_tmux(tmp_path, runner)
    tmux._run(*NEW)
    [(command, env)] = runner.commands('new-session')
    assert command[0] == 'tmux' and not runner.commands('true')
    assert env == unjailed_environment(os.environ) and tmux.started_server is None


def test_other_commands_never_ask_and_are_never_wrapped(tmp_path, systemd_run):
    runner = Runner()
    tmux = ship_tmux(tmp_path, runner)
    tmux._run('set-option', '-g', 'history-limit', '1')
    assert [command[0] for command, _ in runner.calls] == ['tmux'] and len(runner.calls) == 1


@pytest.mark.parametrize('server', ['private', 'named', 'legacy'])
def test_a_private_named_or_legacy_server_is_never_wrapped(tmp_path, systemd_run, server):
    runner = Runner()
    tmux = {'private': lambda: Tmux(str(tmp_path / 'private' / 'server')),
            'named': lambda: Tmux('qd-test-private'),
            'legacy': lambda: Tmux(str(tmp_path / 'default' / 'server'), legacy=True)}[server]()
    tmux._runner = runner
    tmux._run(*NEW)
    assert [command[0] for command, _ in runner.calls] == ['tmux'], 'no probe, no scope'
    assert tmux.started_server is None


def test_without_systemd_run_the_server_starts_as_before_and_says_why(tmp_path, monkeypatch):
    monkeypatch.setenv('PATH', str(tmp_path / 'nothing-here'))
    runner = Runner()
    tmux = ship_tmux(tmp_path, runner)
    tmux._run(*NEW)
    [(command, env)] = runner.commands('new-session')
    assert command[0] == 'tmux' and env == unjailed_environment(os.environ)
    assert tmux.started_server == "tmux server started in the caller's scope: systemd-run is absent"


def test_when_the_users_manager_cannot_be_reached_the_server_starts_as_before_and_says_why(tmp_path, systemd_run):
    runner = Runner(manager=1)
    tmux = ship_tmux(tmp_path, runner)
    tmux._run(*NEW)
    [(command, _)] = runner.commands('new-session')
    assert command[0] == 'tmux'
    assert tmux.started_server == ("tmux server started in the caller's scope: systemd-run --user cannot reach "
                                   "the user's service manager (Failed to connect to bus)")


def test_for_ship_names_its_own_server_for_the_scope(tmp_path, monkeypatch):
    monkeypatch.delenv('QD_TMUX_SOCKET', raising=False)
    monkeypatch.setattr(Tmux, 'defaults', classmethod(lambda cls, own: []))
    ship = SimpleNamespace(name='test ship', runtime=tmp_path / 'runtime', folder=tmp_path)
    tmux = Tmux.for_ship(ship)
    assert tmux.ship and not tmux.legacy and tmux.name == 'test ship'


def test_a_server_question_that_hangs_reads_as_not_answering_and_the_start_is_scoped(tmp_path, systemd_run):
    import subprocess
    from quarterdeck.tmux import PRIVATE_COMMAND_WAIT
    runner = Runner()
    asked = []

    def hung(command, **kwargs):
        if command[-1] == 'list-sessions':
            asked.append(kwargs.get('timeout'))
            raise subprocess.TimeoutExpired(command, kwargs.get('timeout'))
        return runner(command, **kwargs)

    tmux = ship_tmux(tmp_path, hung)
    assert tmux._run(*NEW) == '$1\n'
    assert asked == [PRIVATE_COMMAND_WAIT], 'the question is bounded'
    [(command, _)] = runner.commands('new-session')
    assert command[0] == str(systemd_run) and tmux.started_server.startswith('tmux server started in its own scope')
