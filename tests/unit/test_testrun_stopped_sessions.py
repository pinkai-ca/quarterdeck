"""Ordinary process/server lifetime regressions for interrupted test runs."""
import json
import os
from pathlib import Path
import shutil
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
                if not server._owned.namespace.closed:
                    os.close(server._owned.namespace.directory)
            os.close(descriptor)
            root.close()


SOURCE = Path(__file__).resolve().parents[2]


def tool_project(project):
    """A synthetic project carrying this checkout's code, conftest and test tool, so its tests can open private
    tmux servers through the real fixture; the caller writes its tests and commits."""
    shutil.copytree(SOURCE / 'src', project / 'src', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copyfile(SOURCE / 'pyproject.toml', project / 'pyproject.toml')
    (project / '.gitignore').write_text('.testruns/\n__pycache__/\n*.egg-info/\nwork/\n')
    tests = project / 'tests'
    tests.mkdir()
    shutil.copyfile(SOURCE / 'tests/__init__.py', tests / '__init__.py')
    shutil.copyfile(SOURCE / 'tests/conftest.py', tests / 'conftest.py')
    shutil.copytree(SOURCE / 'tests/testrunner', tests / 'testrunner', ignore=shutil.ignore_patterns('__pycache__'))
    return tests


def tool_env(project):
    env = dict(os.environ, TMPDIR=str(project), OPENBLAS_NUM_THREADS='1')
    for key in ('TMUX', 'TMUX_PANE', 'TMUX_TMPDIR', 'QD_TMUX_SOCKET', 'QD_SHIP', 'QD_SEAT',
                'TESTRUNS_RUN', 'TESTRUN_SESSION_RECORDS'):
        env.pop(key, None)
    return env


def test_tool_stopped_during_test_cleans_server_and_next_run_uses_memory(monkeypatch):
    source = SOURCE
    with tempfile.TemporaryDirectory(prefix='qdi-', dir='/tmp') as temporary:
        project = Path(temporary)
        tests = tool_project(project)
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
        env = tool_env(project)
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


HOLD = '''import os, signal, time
from pathlib import Path
def test_hold(tmux):
    tmux.new_session("held")
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    Path({ready!r}).write_text(f"{{tmux._owned.pid}} {{os.getpid()}}")
    time.sleep(120)
'''


def test_server_ownership_holds_with_several_runs_in_memory_at_once():
    """Two runs of two changes of one project hold memory at once, each test with its private tmux server under
    the project's one socket anchor. The first run's runner dies, leaving its test and server alive. Stopping the
    second run stops only its own server and retires only its own scratch and reservation; the first run's server,
    test, scratch and reservation stand until the first run is retired by name, which then ends its server."""
    with tempfile.TemporaryDirectory(prefix='qdc-', dir='/tmp') as temporary:
        project = Path(temporary)
        tests = tool_project(project)
        ready = {name: project / f'ready-{name}' for name in 'ab'}
        (tests / 'test_hold.py').write_text(HOLD.format(ready=str(ready['a'])))
        git = ['-c', 'user.name=Test', '-c', 'user.email=test@example.invalid']
        for argv in (['init', '-q', '-b', 'main'], ['add', '.'], [*git, 'commit', '-qm', 'synthetic tests']):
            subprocess.run(['git', '-C', str(project), *argv], check=True, capture_output=True)
        other = project / 'work' / 'b'
        subprocess.run(['git', '-C', str(project), 'worktree', 'add', '-q', '-b', 'b', str(other)], check=True)
        (other / 'tests' / 'test_hold.py').write_text(HOLD.format(ready=str(ready['b'])))
        subprocess.run(['git', '-C', str(other), *git, 'commit', '-qam', 'the other change'], check=True,
                       capture_output=True)
        env = tool_env(project)
        roots = [base / scratch.project_name(named) for base in scratch.candidates(env)
                 for named in {project, project.resolve()}]
        processes, pids, records = {}, {}, {}

        def start(name, target):
            processes[name] = subprocess.Popen(
                [sys.executable, '-I', str(SOURCE / 'tests/testrun.py'), 'run', '--target', str(target),
                 '-n', '0', '--timeout', '180', 'tests/test_hold.py'],
                env=env, cwd=project, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            deadline = time.monotonic() + 60
            while not ready[name].exists() and processes[name].poll() is None and time.monotonic() < deadline:
                time.sleep(.05)
            assert ready[name].exists(), (processes[name].communicate(timeout=10)[0]
                                          if processes[name].poll() is not None else f'{name} not ready')
            while len(ready[name].read_text().split()) < 2 and time.monotonic() < deadline:
                time.sleep(.05)  # written, but perhaps not yet whole
            pids[name] = dict(zip(('server', 'test'), map(int, ready[name].read_text().split())))
            known = set(records.values())
            found = [p for p in (project / '.testruns/results').glob('*/storage.json') if p not in known]
            assert len(found) == 1, found
            records[name] = found[0]

        def alive(pid):
            fields = procs.stat_fields(pid)
            return fields is not None and fields[0] != 'Z'

        try:
            start('a', project)
            processes['a'].kill()  # its runner dies; its test and its server live on in memory
            processes['a'].communicate(timeout=30)
            first = json.loads(records['a'].read_text())
            first_bytes = records['a'].read_bytes()
            first_folder = Path(first['base']) / first['root'] / 'runs' / first['run']
            assert first['kind'] != 'disk' and first_folder.is_dir()

            start('b', other)
            second = json.loads(records['b'].read_text())
            assert second['kind'] != 'disk', 'the second run takes memory beside the first'
            assert second['root'] == first['root'] and second['run'] != first['run']
            assert all(alive(pid) for name in 'ab' for pid in pids[name].values()), 'both tests and servers run'

            processes['b'].send_signal(signal.SIGTERM)
            output = processes['b'].communicate(timeout=55)[0]
            assert processes['b'].returncode == 4, output
            result = json.loads((records['b'].parent / 'result.json').read_text())
            assert result['outcome']['status'] == 'interrupted'
            assert result['cleanup']['outcome'] == 'complete', result['cleanup']
            assert not alive(pids['b']['server']), 'the stopped run ended its own server'
            assert not records['b'].exists(), 'and released its own reservation'
            assert alive(pids['a']['server']) and alive(pids['a']['test']), "the peer's server and test are untouched"
            assert records['a'].read_bytes() == first_bytes and first_folder.is_dir(), "and its reservation and scratch"

            os.killpg(os.getpgid(pids['a']['test']), signal.SIGKILL)  # the first run's test ends; its server stays
            deadline = time.monotonic() + 10
            while alive(pids['a']['test']) and time.monotonic() < deadline:
                time.sleep(.05)
            assert alive(pids['a']['server']), 'a recorded server outlives its test'
            retired = subprocess.run([sys.executable, '-I', str(SOURCE / 'tests/testrun.py'), 'retire', first['run']],
                                     env=env, cwd=project, capture_output=True, text=True, timeout=120)
            assert retired.returncode == 0, retired.stdout + retired.stderr
            assert not alive(pids['a']['server']), 'retire by name ended the first run its own server'
            assert not first_folder.exists() and not records['a'].exists()
        finally:
            for name, process in processes.items():
                if process.poll() is None:
                    process.send_signal(signal.SIGTERM)
                    process.communicate(timeout=55)
            for name in pids:
                for pid in pids[name].values():
                    if alive(pid):
                        os.kill(pid, signal.SIGKILL)
            for root in roots:
                if root.is_dir() and not root.is_symlink():
                    shutil.rmtree(root, ignore_errors=True)
