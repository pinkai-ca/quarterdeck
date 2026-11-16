"""The tmux servers a run's tests leave are ended at the run's end, by the proof of their own environment.

A private tmux server forks into a session of its own, so a test interrupted before its finally leaves it running
(a stopped one stopped) for good. A server started inside a run inherits the run's TMPDIR, in its scratch: the run's
end ends every server of this user whose environment puts TMPDIR there, through its own socket, resumed first if
stopped, only once that socket answers with that very pid. One that cannot be ended holds the cleanup by name. No
socket of a ship's runtime, and no default socket, is ever ended. Every server here is the test's own, on its own -S
socket in a short folder of its own, and each is ended in a finally.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time

import pytest

from tests.testrunner import procs, services

pytestmark = pytest.mark.skipif(shutil.which('tmux') is None, reason='no tmux')


def clean_env(tmpdir: Path) -> dict:
    env = {key: value for key, value in os.environ.items() if key not in ('TMUX', 'TMUX_PANE', 'QD_TMUX_SOCKET')}
    env['TMPDIR'] = str(tmpdir)
    return env


class Servers:
    """Private servers this test starts, each ended in the end by its own socket (or its kept pid)."""

    def __init__(self):
        self.started = []
        self.folder = Path(tempfile.mkdtemp(prefix='qdts-', dir='/tmp'))  # a socket path must stay short

    def start(self, name: str, tmpdir: Path, socket: Path | None = None) -> dict:
        socket = socket or self.folder / name
        subprocess.run(['tmux', '-S', str(socket), '-f', '/dev/null', 'new-session', '-d', '-s', name, 'sleep 300'],
                       env=clean_env(tmpdir), check=True, timeout=10)
        pid = int(subprocess.run(['tmux', '-S', str(socket), 'display-message', '-p', '#{pid}'],
                                 env=clean_env(tmpdir), capture_output=True, text=True, check=True,
                                 timeout=10).stdout)
        server = {'pid': pid, 'start': procs.start_time(pid), 'socket': str(socket)}
        self.started.append(server)
        return server

    def close(self):
        for server in self.started:
            if not procs.alive({server['pid']: server['start']}):
                continue
            os.kill(server['pid'], signal.SIGCONT)
            os.kill(server['pid'], signal.SIGUSR1)  # a server makes its socket again on SIGUSR1
            deadline = time.monotonic() + 5
            while not Path(server['socket']).exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            subprocess.run(['tmux', '-S', server['socket'], 'if-shell', '-F', '#{==:#{pid},%d}' % server['pid'],
                            'kill-server'], env=clean_env(Path('/tmp')), capture_output=True, timeout=5)
        shutil.rmtree(self.folder, ignore_errors=True)


@pytest.fixture
def servers():
    owned = Servers()
    try:
        yield owned
    finally:
        owned.close()


def gone(server) -> bool:
    return not procs.alive({server['pid']: server['start']})


def test_an_interrupted_runs_servers_legacy_and_never_recorded_are_ended(tmp_path, servers):
    """The interrupted case: a server under a run's socket folder recorded only as legacy (no birth record), and one
    never recorded at all (in a folder of its own, outside the run's socket roots), the second stopped as a test of
    a stopped server leaves it. Both carry TMPDIR in the scratch, and both are ended, the stopped one resumed."""
    scratch = tmp_path / 'scratch'
    (scratch / 'tmp').mkdir(parents=True)
    legacy_folder = servers.folder / 'u-legacy'
    legacy_folder.mkdir()
    legacy = servers.start('legacy', scratch / 'tmp', legacy_folder / 's')
    never = servers.start('never', scratch / 'tmp')
    os.kill(never['pid'], signal.SIGSTOP)
    stopped = services.stop(None, scratch)
    named = {entry['pid']: entry for entry in stopped['servers']}
    assert set(named) == {legacy['pid'], never['pid']} and stopped['outcome'] == 'stopped'
    assert all(entry['outcome'] == 'ended' and entry['root'] == os.path.realpath(scratch) for entry in named.values())
    assert named[never['pid']].get('resumed') and not named[legacy['pid']].get('resumed')
    assert gone(legacy) and gone(never)


def test_a_server_of_another_folder_is_untouched_and_not_named(tmp_path, servers):
    scratch, elsewhere = tmp_path / 'scratch', tmp_path / 'elsewhere'
    scratch.mkdir(), elsewhere.mkdir()
    other = servers.start('other', elsewhere)
    assert services.stop(None, scratch) == {'outcome': 'none', 'entries': [], 'servers': []}
    assert not gone(other)


def test_a_server_its_socket_no_longer_reaches_holds_the_cleanup_and_is_untouched(tmp_path, servers):
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    lost = servers.start('lost', scratch)
    os.unlink(lost['socket'])
    stopped = services.stop(None, scratch)
    [entry] = stopped['servers']
    assert stopped['outcome'] == 'held' and entry['outcome'] == 'held' and 'answers' in entry['reason']
    assert not gone(lost), 'nothing was signalled'


def test_a_socket_reused_by_another_server_holds_and_that_server_is_untouched(tmp_path, servers):
    """The run's server lost its socket path to a newer server of no run: the path answers with another pid, so
    nothing is ended through it, and the newer server is untouched."""
    scratch, elsewhere = tmp_path / 'scratch', tmp_path / 'elsewhere'
    scratch.mkdir(), elsewhere.mkdir()
    ours = servers.start('ours', scratch)
    os.unlink(ours['socket'])
    newer = servers.start('newer', elsewhere, Path(ours['socket']))
    stopped = services.stop(None, scratch)
    [entry] = stopped['servers']
    assert entry['pid'] == ours['pid'] and entry['outcome'] == 'held' and 'another server' in entry['reason']
    assert not gone(newer) and not gone(ours)


def test_a_server_whose_socket_cannot_be_named_holds(tmp_path, monkeypatch, servers):
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    unnamed = servers.start('unnamed', scratch)
    monkeypatch.setattr(services, 'socket_of', lambda argv, environment: None)
    [entry] = services.stop(None, scratch)['servers']
    assert entry['outcome'] == 'held' and 'cannot be named' in entry['reason'] and not gone(unnamed)


def test_the_socket_is_read_from_its_flags_and_its_environment():
    uid = os.getuid()
    assert services.socket_of(['tmux', '-S', '/tmp/x/s', 'new-session'], {}) == '/tmp/x/s'
    assert services.socket_of(['tmux', '-S/tmp/x/s'], {}) == '/tmp/x/s'
    assert services.socket_of(['tmux', '-L', 'named'], {'TMUX_TMPDIR': '/run/t'}) == f'/run/t/tmux-{uid}/named'
    assert services.socket_of(['tmux', 'new-session'], {}) == f'/tmp/tmux-{uid}/default'
    assert services.socket_of(['tmux', '-S', 'relative/s'], {}) is None


@pytest.mark.parametrize('socket, excluded', [
    ('/home/someone/dev/quarterdeck/ships/enterprise/runtime/tmux/server', True),
    ('/tmp/x/ships/forum/runtime/tmux/server', True),
    (f'/tmp/tmux-{os.getuid()}/default', True),
    (f'/run/user/1000/tmux-{os.getuid()}/default', True),
    ('/tmp/qd-1000-abc/r/u-x/qd-pt-y/s', False),
    ('/tmp/qdo-x/.testruns/r/u-larch/qd-pt-z/s', False),
    (f'/tmp/tmux-{os.getuid()}/named', False),
    ('/tmp/ships/runtime/tmux/server', False),
])
def test_no_ship_socket_and_no_default_socket_is_ever_ended(socket, excluded):
    assert services.excluded_socket(socket) is excluded


def test_an_excluded_socket_holds_whatever_the_environment_says(tmp_path, monkeypatch, servers):
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    server = servers.start('excluded', scratch)
    monkeypatch.setattr(services, 'excluded_socket', lambda socket: True)
    [entry] = services.stop(None, scratch)['servers']
    assert entry['outcome'] == 'held' and 'never ended' in entry['reason'] and not gone(server)


def test_a_held_server_holds_the_cleanup_by_name(tmp_path, monkeypatch):
    from tests.testrunner import clean
    from types import SimpleNamespace
    removed = []
    monkeypatch.setattr(clean, 'kept', lambda root, run: None)
    monkeypatch.setattr(clean.storage, 'path', lambda root, run: tmp_path / 'scratch')
    held = {'outcome': 'held', 'entries': [], 'servers': [{'pid': 1, 'socket': '/tmp/x/s', 'outcome': 'held',
                                                             'reason': 'its socket did not answer'}]}
    monkeypatch.setattr(clean.services, 'stop', lambda folder, scratch: held)
    monkeypatch.setattr(clean.owned, 'retire', lambda *args, **kwargs: removed.append(args))
    root = SimpleNamespace(path=tmp_path, project=tmp_path)
    cleanup = clean._retire_run(root, -1, 'run-synthetic', {})
    assert cleanup['outcome'] == 'held' and 'tmux server' in cleanup['reason'] and removed == []
    assert cleanup['services']['servers'][0]['socket'] == '/tmp/x/s', 'named in the cleanup'
