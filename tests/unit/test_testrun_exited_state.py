"""A process caught as it is reaped has exited.

/proc shows an exited process as Z, a zombie, until its parent reaps it, then as X (x before Linux 4.14) for the
instant of its reaping, then not at all. The test tool reads every one of these as ended. A read that lands in the
X instant was once taken for a live process: a stop then held a dead server ('still live without its endpoint'),
and a test that read /proc twice saw Z, then X, and failed although the server had ended.

Every server here is a private one this test starts, reached by its own -S socket; nothing else is signalled.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile

import pytest

from tests.testrunner import procs, servers, sessions
from tests.testrunner.layout import RESULTS, RUN_SOCKETS, Root

RUN = '20260101-010101-e5a7'


def fields_in(state, start='4242'):
    """A /proc stat row after the command, in ``state``, with ``start`` as the kernel start marker (field 19)."""
    fields = [state, '1', '77', '77'] + ['0'] * 16 + ['0'] * 10
    fields[19] = start
    return fields


@pytest.mark.parametrize('state', ['Z', 'X', 'x'])
def test_every_exited_state_reads_as_ended(monkeypatch, state):
    birth = {'pid': 4242, 'start': '4242', 'boot': procs.boot_id()}
    monkeypatch.setattr(procs, 'stat_fields', lambda pid: fields_in(state))
    assert servers.ended(birth)


@pytest.mark.parametrize('state', ['R', 'S', 'D', 'T', 't', 'I'])
def test_a_living_state_with_its_own_start_is_not_ended(monkeypatch, state):
    birth = {'pid': 4242, 'start': '4242', 'boot': procs.boot_id()}
    monkeypatch.setattr(procs, 'stat_fields', lambda pid: fields_in(state))
    assert not servers.ended(birth)


@pytest.mark.parametrize('state', ['Z', 'X', 'x'])
def test_an_exited_process_is_no_member_of_its_group_or_session(monkeypatch, state):
    monkeypatch.setattr(procs, '_ours', lambda: [11, 12])
    rows = {11: fields_in('S'), 12: fields_in(state)}
    monkeypatch.setattr(procs, 'stat_fields', rows.get)
    assert procs.group_members(77) == [11]
    rows[11] = fields_in(state)
    assert procs.group_members(77) == []
    assert procs.session_groups(77) == []


def test_a_server_caught_as_it_is_reaped_is_retired_not_held(monkeypatch):
    """The reproduction: the recorded server has ended and its endpoint is gone, but every read of its process lands
    in the reaping instant, X. The retire completes, as it does for a zombie or a vanished process."""
    from quarterdeck.tmux import Tmux

    with tempfile.TemporaryDirectory(prefix='qdx-', dir='/tmp') as temporary:
        project = Path(temporary)
        subprocess.run(['git', 'init', '-q', str(project)], check=True)
        (project / '.gitignore').write_text('.testruns/\n')
        root = Root(project)
        registry = root.path / RESULTS / RUN / 'sessions'
        for parts in ((RESULTS, RUN, 'sessions'), (RUN_SOCKETS,)):
            os.close(root.child(*parts))
        parent = root.path / RUN_SOCKETS
        folder = parent / 'u-reaped'
        folder.mkdir(mode=0o700)
        descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
        for key in ('TMUX', 'TMUX_PANE', 'QD_TMUX_SOCKET'):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv(sessions.RECORD_ENV, str(registry))
        monkeypatch.setenv('TESTRUNS_RUN', RUN)
        monkeypatch.setenv('TMUX_TMPDIR', str(folder))
        server = None
        try:
            sessions.register(parent, folder, descriptor)
            server = Tmux.private(folder)
            server.new_session('reaped')
            birth = next(iter(json.loads((registry / (folder.name + '.json')).read_text())['servers'].values()))
            subprocess.run(['tmux', '-S', server.socket, '-N', 'kill-server'], check=True, timeout=5)
            real = procs.stat_fields
            monkeypatch.setattr(procs, 'stat_fields',
                                lambda pid: fields_in('X', birth['start']) if pid == birth['pid'] else real(pid))
            outcome = sessions.retire(root, RUN)
            assert outcome['outcome'] == 'complete', outcome
            assert not folder.exists()
        finally:
            monkeypatch.undo()
            if server is not None:
                fields = procs.stat_fields(server._owned.pid) if server._owned.pid else None
                if fields is not None and fields[0] not in procs.EXITED:
                    subprocess.run(['tmux', '-S', server.socket, '-N', 'kill-server'],
                                   capture_output=True, timeout=5, check=False)
                server.close()
                if server._owned.socket_fd is not None:
                    os.close(server._owned.socket_fd)
                    server._owned.socket_fd = None
                if not server._owned.namespace.closed:
                    os.close(server._owned.namespace.directory)
            os.close(descriptor)
            root.close()
