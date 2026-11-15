"""Creator bootstrap and timer completion; only synthetic commands run here."""

import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from quarterdeck import cli, daily_live
from quarterdeck.ship import ShipError
from quarterdeck.tmux import TmuxError
from .test_daily_live import ship


def test_check_child_can_open_a_session_on_the_creators_server(tmp_path, monkeypatch):
    """The actual observer transport cannot start a missing server (-N)."""
    origin = ship(tmp_path / 'origin')
    command = tmp_path / 'synthetic-check'
    source = Path(daily_live.__file__).resolve().parents[1]
    command.write_text(f'''#!{sys.executable}
import os, sys
sys.path.insert(0, {str(source)!r})
from quarterdeck.ship import load
from quarterdeck.tmux import Tmux
assert 'TMUX' not in os.environ and 'TMUX_PANE' not in os.environ
server = Tmux.for_ship(load(os.environ['QD_SHIP']))
assert server._owned is None
assert server.sessions() == []
session = server.new_session('synthetic-check')
server.kill_session_id(session)
print('synthetic observer completed once')
''')
    command.chmod(0o700)
    monkeypatch.setenv('TMUX', '/synthetic/origin/socket,one,two')
    monkeypatch.setenv('TMUX_PANE', '%synthetic')
    before = {key: os.environ[key] for key in ('TMUX', 'TMUX_PANE')}
    scratch = tmp_path / 'scratch'
    result = daily_live.invoke(origin, 'fake', scratch, str(command))
    assert result.exit_code == 0, result.output
    assert result.output == 'synthetic observer completed once'
    assert result.retained is None
    assert not Path(result.place).exists()
    assert not Path(result.socket).parent.exists()
    assert {key: os.environ[key] for key in before} == before


@pytest.mark.parametrize('inherited', [False, True])
@pytest.mark.parametrize('failure', [False, True])
def test_bootstrap_uses_creator_identity_and_restores_environment(monkeypatch, inherited, failure):
    for key in ('TMUX', 'TMUX_PANE'):
        if inherited:
            monkeypatch.setenv(key, 'synthetic-origin')
        else:
            monkeypatch.delenv(key, raising=False)
    identity = object()
    events = []
    def new_session(name):
        assert 'TMUX' not in os.environ and 'TMUX_PANE' not in os.environ
        events.append('started')
        if failure:
            raise TmuxError('synthetic startup failure')
        return identity
    def kill_session_id(value):
        assert value is identity
        events.append('retired')
    server = SimpleNamespace(new_session=new_session, kill_session_id=kill_session_id)
    if failure:
        with pytest.raises(TmuxError, match='synthetic startup failure'):
            daily_live.start_private(server)
    else:
        daily_live.start_private(server)
    assert events == (['started'] if failure else ['started', 'retired'])
    for key in ('TMUX', 'TMUX_PANE'):
        assert os.environ.get(key) == ('synthetic-origin' if inherited else None)


def test_startup_failure_is_reported_and_creator_cleanup_runs(tmp_path, monkeypatch):
    origin = ship(tmp_path / 'origin')
    events = []
    def fail(name):
        events.append('start')
        raise TmuxError('synthetic startup failure')
    server = SimpleNamespace(new_session=fail, close=lambda: events.append('close') or True)
    monkeypatch.setattr(daily_live, 'open_private', lambda parent: server)
    monkeypatch.setattr(daily_live.subprocess, 'run', lambda *a, **kw: pytest.fail('delegated before ready'))
    result = daily_live.invoke(origin, 'fake', tmp_path / 'scratch', '/synthetic/check')
    assert result.exit_code is None
    assert 'synthetic startup failure' in result.output
    assert result.retained is None
    assert events == ['start', 'close']
    assert not Path(result.place).exists()


def test_a_filed_check_failure_is_successful_daily_reporting(monkeypatch):
    report = SimpleNamespace(short='synthetic-report', to='synthetic-desk', body='fake: check failed (exit 2)\n')
    monkeypatch.setattr(cli, 'load_ship', lambda args: object())
    monkeypatch.setattr(daily_live, 'run', lambda origin: (report, True))
    args = cli.build_parser().parse_args(['daily-live'])
    result = cli.cmd_daily_live(args)
    assert args.exit_code == 0
    assert result == ['synthetic-report to synthetic-desk · stored', 'fake: check failed (exit 2)']


def test_reporting_failure_is_still_a_command_error(monkeypatch):
    monkeypatch.setattr(cli, 'load_ship', lambda args: object())
    def fail(origin):
        raise ShipError('synthetic report unavailable')
    monkeypatch.setattr(daily_live, 'run', fail)
    with pytest.raises(ShipError, match='synthetic report unavailable'):
        cli.cmd_daily_live(cli.build_parser().parse_args(['daily-live']))
