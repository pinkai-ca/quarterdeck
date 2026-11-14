"""Ownership proofs for the test tool's stop and retire of a run's private tmux servers.

Stop and retire end only a server the run recorded, by its session record and owner token, or a legacy server
that passes every one of the three proofs: its socket lies inside the run's recorded session container whose
identity and owner token match, its own environment names the run, and its namespace header is qd's. Anything
else is held with its reason and left exactly as it was: a foreign server in a sibling folder, a recorded socket
another listener holds, a legacy server short of one proof, a recorded server that no longer answers.

Every server here is a private one this test starts, reached by its own -S socket; nothing else is signalled.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

import pytest

from tests.testrunner import procs, run, servers, sessions
from tests.testrunner.layout import RESULTS, RUN_SOCKETS, Root

RUN = '20260101-010101-a1c3'
OTHER_RUN = '20260101-010101-b2d4'


def alive(pid):
    fields = procs.stat_fields(pid)
    return fields is not None and fields[0] not in procs.EXITED


def held(outcome, reason):
    entries = [entry for entry in outcome.get('entries', []) if entry.get('outcome') == 'held']
    assert outcome['outcome'] != 'complete' and entries, outcome
    assert any(reason in entry.get('reason', '') for entry in entries), outcome


@contextmanager
def world(monkeypatch):
    """A synthetic project with one run, its recorded session container, and the servers this test starts."""
    from quarterdeck.tmux import Tmux

    with tempfile.TemporaryDirectory(prefix='qdo-', dir='/tmp') as temporary:
        project = Path(temporary)
        subprocess.run(['git', 'init', '-q', str(project)], check=True)
        (project / '.gitignore').write_text('.testruns/\n')
        root = Root(project)
        registry = root.path / RESULTS / RUN / 'sessions'
        for parts in ((RESULTS, RUN, 'sessions'), (RUN_SOCKETS,)):
            os.close(root.child(*parts))
        parent = root.path / RUN_SOCKETS
        folder = parent / 'u-larch'
        folder.mkdir(mode=0o700)
        descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
        started = []
        for key in ('TMUX', 'TMUX_PANE', 'QD_TMUX_SOCKET'):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv(sessions.RECORD_ENV, str(registry))
        monkeypatch.setenv('TESTRUNS_RUN', RUN)
        sessions.register(parent, folder, descriptor)

        def start(where=folder, *, recorded=True, run_env=RUN):
            """A private server in ``where``: recorded by the run's birth hook, or legacy (no birth record)."""
            with pytest.MonkeyPatch.context() as patch:
                patch.setenv('TMUX_TMPDIR', str(where))
                if not recorded:
                    patch.delenv(sessions.RECORD_ENV, raising=False)
                if run_env is None:
                    patch.delenv('TESTRUNS_RUN', raising=False)
                else:
                    patch.setenv('TESTRUNS_RUN', run_env)
                server = Tmux.private(where)
                started.append(server)
                server.new_session('larch')
            return server

        def record():
            return json.loads((registry / (folder.name + '.json')).read_text())

        def rewrite(**changes):
            data = record()
            data.update(changes)
            (registry / (folder.name + '.json')).write_text(json.dumps(data))

        try:
            yield SimpleNamespace(root=root, parent=parent, folder=folder, start=start, record=record,
                                  rewrite=rewrite, retire=lambda: sessions.retire(root, RUN))
        finally:
            for server in started:
                pid = server._owned.pid
                if pid is not None and alive(pid):
                    os.kill(pid, signal.SIGCONT)
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


def listing(path):
    return sorted((entry.name, entry.inode()) for entry in os.scandir(path))


# (1) A foreign server in a sibling folder, not recorded by the run.

def test_a_foreign_server_in_a_sibling_folder_is_never_touched_by_stop_or_retire(monkeypatch):
    with world(monkeypatch) as w:
        own = w.start()
        sibling = w.parent / 'u-sibling'
        sibling.mkdir(mode=0o700)
        # Even with the run's own name in its environment: it lies outside every recorded container.
        foreign = w.start(sibling, recorded=False)
        before = {path: listing(path) for path in (sibling, Path(foreign.socket).parent)}
        script = 'import time; time.sleep(120)'
        process = subprocess.Popen([sys.executable, '-c', script], start_new_session=True)
        try:
            run.Runner(SimpleNamespace(), []).stop_group(process)  # a stopped run's own process group
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        assert alive(foreign._owned.pid), 'stop never reaches a server outside the run'
        outcome = w.retire()
        assert outcome['outcome'] == 'complete', outcome
        assert not alive(own._owned.pid) and not w.folder.exists(), 'the run retired its own server'
        assert alive(foreign._owned.pid), 'retire never reaches an unrecorded server'
        assert {path: listing(path) for path in before} == before, 'its folder, socket and lock are untouched'
        assert foreign.has_session('larch')


# (2) A recorded socket with a live listener.

@pytest.mark.parametrize('form', ['endpoint-replaced', 'same-endpoint'])
def test_a_recorded_socket_with_a_live_listener_is_held_with_its_reason_and_never_removed(monkeypatch, form):
    with world(monkeypatch) as w:
        own = w.start()
        path = Path(own.socket)
        os.kill(own._owned.pid, signal.SIGKILL)  # the recorded server is gone; it left its socket behind
        deadline = time.monotonic() + 5
        while alive(own._owned.pid) and time.monotonic() < deadline:
            time.sleep(.01)  # its death is proved before retirement looks, so only the listener stands in the way
        assert not alive(own._owned.pid)
        assert path.exists() and not path.is_symlink()
        path.unlink()
        listener = socket.socket(socket.AF_UNIX)
        try:
            listener.bind(str(path))
            listener.listen(1)
            bound = servers.identity(path.lstat())
            if form == 'same-endpoint':
                # The birth names exactly this endpoint: only the live listener stands in the way.
                data = w.record()
                [key] = data['servers']
                data['servers'][key]['socket'] = list(servers.identity(path.lstat()))
                w.rewrite(servers=data['servers'])
            outcome = w.retire()
            reason = 'endpoint changed' if form == 'endpoint-replaced' else 'socket listener remains live'
            held(outcome, reason)
            assert servers.identity(path.lstat()) == bound, 'the listened socket is never removed'
            assert (path.parent / servers.LOCK).exists(), 'its namespace lock is kept'
        finally:
            listener.close()
        own._owned.socket_fd and os.close(own._owned.socket_fd)
        own._owned.socket_fd = None
        own._closed = True  # this test removed the endpoint; nothing of the server is left to close


# (3) A legacy server, from a run before births were recorded: held unless all three proofs hold.

@pytest.mark.parametrize('missing', ['run-env-absent', 'run-env-other', 'namespace-header',
                                     'container-token', 'container-identity'])
def test_a_legacy_server_missing_any_ownership_proof_is_held_and_left_running(monkeypatch, missing):
    with world(monkeypatch) as w:
        legacy = w.start(recorded=False, run_env={'run-env-absent': None, 'run-env-other': OTHER_RUN}.get(missing, RUN))
        assert 'servers' not in w.record() or not w.record()['servers'], 'legacy: no birth record'
        lock = Path(legacy.socket).parent / servers.LOCK
        if missing == 'namespace-header':
            with open(lock, 'r+b') as handle:  # same file, same mode: only its header is not qd's
                handle.write(b'another program namespace\n')
        elif missing == 'container-token':
            w.rewrite(owner='0123456789abcdef0123456789abcdef')
        elif missing == 'container-identity':
            data = w.record()
            w.rewrite(identity=[data['identity'][0], data['identity'][1] + 1])
        before = listing(Path(legacy.socket).parent)
        outcome = w.retire()
        reason = {'namespace-header': 'legacy namespace header unproved',
                  'run-env-absent': 'legacy server process/run ownership unproved',
                  'run-env-other': 'legacy server process/run ownership unproved'}.get(missing, '')
        held(outcome, reason)
        assert alive(legacy._owned.pid), 'a server short of one proof is never signalled'
        assert listing(Path(legacy.socket).parent) == before, 'its socket and lock are untouched'
        if missing == 'namespace-header':
            with open(lock, 'r+b') as handle:  # qd's own client refuses the foreign header too: restore it
                handle.write(servers.HEADER)
        assert legacy.has_session('larch'), 'the server still serves its session'


# (4) A recorded server that no longer answers its owner's query.

def test_a_recorded_server_that_does_not_answer_is_held_with_its_reason(monkeypatch):
    with world(monkeypatch) as w:
        own = w.start()
        os.kill(own._owned.pid, signal.SIGSTOP)
        try:
            outcome = w.retire()
        finally:
            os.kill(own._owned.pid, signal.SIGCONT)
        held(outcome, '')
        assert alive(own._owned.pid) and Path(own.socket).exists(), 'nothing it could not prove is removed'
