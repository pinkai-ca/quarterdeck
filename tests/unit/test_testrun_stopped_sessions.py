"""Ordinary process/server lifetime regressions for interrupted test runs."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

import pytest

from tests.testrunner import procs, run, sessions, servers, scratch
from tests.testrunner.layout import RESULTS, RUN_SOCKETS, Root


def test_stop_waits_for_child_after_shell_exit(monkeypatch):
    script = '''
import os, signal, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c',
    'import os, signal, time; os.setpgrp(); signal.signal(signal.SIGINT, signal.SIG_IGN); '
    'print(os.getpid(), flush=True); time.sleep(120)'])
time.sleep(120)
'''
    process = subprocess.Popen([sys.executable, '-c', script], start_new_session=True,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    child = int(process.stdout.readline())
    monkeypatch.setattr(run, 'GRACE', .15)
    runner = run.Runner(SimpleNamespace(), [])
    try:
        runner.stop_group(process)
        assert process.poll() is not None
        assert procs.session_groups(process.pid) == []
        fields = procs.stat_fields(child)
        assert fields is None or fields[0] == 'Z'
    finally:
        for group in procs.session_groups(process.pid):
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=5)
        process.stdout.close()


@pytest.mark.parametrize('phase', ['running', 'dead', 'legacy'])
def test_recorded_server_retires_and_memory_can_be_selected_again(monkeypatch, phase):
    from quarterdeck.tmux import Tmux

    with tempfile.TemporaryDirectory(prefix='qds-', dir='/tmp') as temporary:
        project = Path(temporary)
        subprocess.run(['git', 'init', '-q', str(project)], check=True)
        (project / '.gitignore').write_text('.testruns/\n')
        root = Root(project)
        run_id = '20260101-010101-abcd'
        registry = root.path / RESULTS / run_id / 'sessions'
        for parts in ((RESULTS, run_id, 'sessions'), (RUN_SOCKETS,)):
            os.close(root.child(*parts))
        parent = root.path / RUN_SOCKETS
        folder = parent / 'u-synthetic'
        folder.mkdir(mode=0o700)
        descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
        monkeypatch.setenv(sessions.RECORD_ENV, str(registry))
        monkeypatch.setenv('TESTRUNS_RUN', run_id)
        monkeypatch.setenv('TMUX_TMPDIR', str(folder))
        server = None
        try:
            sessions.register(parent, folder, descriptor)
            # The same birth hook used by conftest also covers a server orphaned before teardown.
            server = Tmux.private(folder)
            server.new_session('running')
            birth = json.loads((registry / (folder.name + '.json')).read_text())['servers']
            assert len(birth) == 1
            pid = next(iter(birth.values()))['pid']
            assert not servers.ended(next(iter(birth.values())))
            if phase == 'dead':
                subprocess.run(['tmux', '-S', server.socket, '-N', 'kill-server'], check=True)
            elif phase == 'legacy':
                filename = registry / (folder.name + '.json')
                old = json.loads(filename.read_text())
                old.pop('servers')
                filename.write_text(json.dumps(old))
            outcome = sessions.retire(root, run_id)
            assert outcome['outcome'] == 'complete', outcome
            assert not folder.exists()
            assert procs.stat_fields(pid) is None or procs.stat_fields(pid)[0] == 'Z'
            monkeypatch.setenv('TMPDIR', str(project))
            selected = scratch.prepare(root, '20260101-010102-abcd')
            assert selected['kind'] != 'disk'
            scratch.discard(root, '20260101-010102-abcd')
        finally:
            if server is not None:
                if folder.exists():
                    server.close()
                # Retirement already removed the endpoint; release only this test's held descriptors.
                if server._owned.socket_fd is not None:
                    os.close(server._owned.socket_fd)
                    server._owned.socket_fd = None
                os.close(server._owned.namespace.directory)
            os.close(descriptor)
            root.close()


def test_tool_stopped_during_test_cleans_server_and_next_run_uses_memory(monkeypatch):
    import shutil

    source = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix='qdi-', dir='/tmp') as temporary:
        project = Path(temporary)
        shutil.copytree(source / 'src', project / 'src', ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copyfile(source / 'pyproject.toml', project / 'pyproject.toml')
        (project / '.gitignore').write_text('.testruns/\n__pycache__/\n*.egg-info/\n')
        tests = project / 'tests'
        tests.mkdir()
        shutil.copyfile(source / 'tests/__init__.py', tests / '__init__.py')
        shutil.copyfile(source / 'tests/conftest.py', tests / 'conftest.py')
        shutil.copytree(source / 'tests/testrunner', tests / 'testrunner',
                        ignore=shutil.ignore_patterns('__pycache__'))
        ready = project / 'ready'
        (tests / 'test_pause.py').write_text(
            'import signal, time\nfrom pathlib import Path\n'
            'def test_pause(tmux):\n'
            '    tmux.new_session("running")\n'
            '    signal.signal(signal.SIGINT, signal.SIG_IGN)\n'
            f'    Path({str(ready)!r}).write_text(str(tmux._owned.pid))\n'
            '    time.sleep(120)\n')
        (tests / 'test_done.py').write_text('def test_done():\n    assert 2 + 2 == 4\n')
        for argv in (['init', '-q', '-b', 'main'], ['add', '.'], ['-c', 'user.name=Test',
                     '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'synthetic tests']):
            subprocess.run(['git', '-C', str(project), *argv], check=True, capture_output=True)
        env = dict(os.environ, TMPDIR=str(project), OPENBLAS_NUM_THREADS='1')
        for key in ('TMUX', 'TMUX_PANE', 'TMUX_TMPDIR', 'QD_TMUX_SOCKET', 'QD_SHIP', 'QD_SEAT',
                    'TESTRUNS_RUN', 'TESTRUN_SESSION_RECORDS'):
            env.pop(key, None)
        command = [sys.executable, '-I', str(source / 'tests/testrun.py'), 'run',
                   '--target', str(project), '-n', '0', '--timeout', '180']
        process = subprocess.Popen([*command, 'tests/test_pause.py'], env=env, cwd=project,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            deadline = time.monotonic() + 60
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.05)
            assert ready.exists(), process.communicate(timeout=10)[0] if process.poll() is not None else 'not ready'
            server_pid = int(ready.read_text())
            process.send_signal(signal.SIGTERM)
            output = process.communicate(timeout=55)[0]
            assert process.returncode == 4, output
            results = list((project / '.testruns/results').glob('*/result.json'))
            assert len(results) == 1
            result = json.loads(results[0].read_text())
            assert result['outcome']['status'] == 'interrupted'
            assert result['cleanup']['outcome'] == 'complete', result['cleanup']
            assert result['working_storage']['kind'] != 'disk'
            fields = procs.stat_fields(server_pid)
            assert fields is None or fields[0] == 'Z'
            ready.unlink()
            next_run = subprocess.run([*command, 'tests/test_done.py'], env=env, cwd=project,
                                      capture_output=True, text=True, timeout=60)
            assert next_run.returncode == 0, next_run.stdout + next_run.stderr
            results = list((project / '.testruns/results').glob('*/result.json'))
            assert len(results) == 2
            passed = next(json.loads(p.read_text()) for p in results
                          if json.loads(p.read_text())['outcome']['status'] == 'passed')
            assert passed['working_storage']['kind'] != 'disk'
            assert passed['cleanup']['outcome'] == 'complete'
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                process.communicate(timeout=55)
