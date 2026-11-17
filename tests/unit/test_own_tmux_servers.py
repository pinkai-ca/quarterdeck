"""The shared ending of a test's own tmux servers: by the pid each ran as, never through its socket.

Each case is a weak case of an ending the tests used before this helper: a server whose socket is gone (an ending by
'tmux -S <socket> kill-server' misses it), and a stopped server (a SIGTERM by pid alone, waited on or not, leaves it).
Every server is the test's own, on a private -S socket in a short folder of its own. The reused pid and the safety
bound are proved in test_testrun_tmux_servers.py.
"""
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile

import pytest

from tests.own_tmux_servers import OwnServers, gone, tmux_env

pytestmark = pytest.mark.skipif(shutil.which('tmux') is None, reason='no tmux')


@pytest.fixture
def folder():
    made = Path(tempfile.mkdtemp(prefix='qdts-', dir='/tmp'))  # a socket path must stay short
    yield made
    shutil.rmtree(made, ignore_errors=True)


def test_a_server_whose_socket_is_gone_is_ended_by_its_pid(folder):
    owned = OwnServers()
    server = owned.start(folder / 's', 'sleep 300')
    os.unlink(server['socket'])
    try:
        assert owned.close() == [f"{server['pid']}: ended by SIGTERM"] and gone(server)
    finally:
        os.kill(server['pid'], signal.SIGKILL) if not gone(server) else None


def test_a_stopped_server_is_woken_and_ended(folder):
    owned = OwnServers()
    server = owned.start(folder / 's', 'sleep 300')
    os.kill(server['pid'], signal.SIGSTOP)
    try:
        assert owned.close() == [f"{server['pid']}: ended by SIGTERM"] and gone(server)
    finally:
        if not gone(server):
            os.kill(server['pid'], signal.SIGCONT)
            os.kill(server['pid'], signal.SIGKILL)


def test_a_server_the_test_started_otherwise_is_kept_and_ended(folder):
    socket = folder / 's'
    subprocess.run(['tmux', '-S', str(socket), '-f', '/dev/null', 'new-session', '-d', 'sleep 300'],
                   env=tmux_env(), check=True, timeout=10)
    owned = OwnServers()
    server = owned.keep(socket)
    assert server['socket'] == str(socket) and not gone(server)
    assert owned.close() == [f"{server['pid']}: ended by SIGTERM"] and gone(server)
    assert owned.close() == [f"{server['pid']}: already gone"], 'a second close signals nothing'


@pytest.mark.parametrize('socket', ['relative/s', f'/tmp/tmux-{os.getuid()}/default'])
def test_only_a_private_socket_of_the_test_is_taken(socket):
    with pytest.raises(ValueError, match='not a private socket'):
        OwnServers().start(socket, 'sleep 300')
    with pytest.raises(ValueError, match='not a private socket'):
        OwnServers().keep(socket)
