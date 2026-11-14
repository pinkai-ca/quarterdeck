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
from tests.testrunner.layout import RESULTS, RUNS, RUN_SOCKETS, Root

# Every proof of a stop or a retire runs twice: on the disk fallback, forced, so that it holds on a machine with no
# memory to give (GitHub's runner has no tmpfs with room); and in memory, where there is memory, skipped by name where
# there is none. What each proves of the stopped run, its server and its scratch is the same on both.
STORAGE = ('memory', 'disk')
NO_MEMORY = 'no writable executable tmpfs/ramfs has room for {}: the disk case proves the same on this machine'


def needs_memory(storage, runs=1):
    """Skip the memory case by name where the tool could not take memory for ``runs`` runs at once."""
    if storage == 'memory' and scratch.choose(runs * scratch.BOUND + scratch.MARGIN) is None:
        pytest.skip(NO_MEMORY.format(f'{runs} run(s) of 4 GiB plus the 8 GiB margin'))


def no_memory(bound=scratch.BOUND):
    """The tool's storage choice on a machine with no memory to give: every candidate refused."""
    return iter(())


def storage_kind(storage, kind):
    """The run took the storage its case asked for."""
    return kind == 'disk' if storage == 'disk' else kind in scratch.MEMORY_TYPES.values()


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


@pytest.mark.parametrize('storage', STORAGE)
@pytest.mark.parametrize('phase', ['running', 'dead', 'legacy'])
def test_recorded_server_retires_and_memory_can_be_selected_again(monkeypatch, phase, storage):
    """A recorded server is retired whatever its phase, and the next run's storage is selected afresh: memory where
    there is memory, the disk fallback where there is none."""
    from quarterdeck.tmux import Tmux

    needs_memory(storage)
    if storage == 'disk':
        monkeypatch.setattr(scratch, 'choices', no_memory)

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
            assert storage_kind(storage, selected['kind']), selected
            if storage == 'disk':  # the fallback reserves nothing, so there is nothing to release
                assert selected['path'] == str(root.path / RUNS / '20260101-010102-abcd'), selected
            else:
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


# The tool itself, this checkout's copy, run through a driver where a case asks the run something of its own: on a
# machine with no memory to give, its storage choice refuses every candidate, so the run falls back to disk exactly as
# on GitHub's runner; and where a stopped test ignores SIGINT on purpose, the tool's stop grace (production 15 s) is
# shortened for that run alone, since the escalation it then makes is the proof, not the grace's length. The runner's
# files are the same as the plain command's.
TOOL = '''import sys
sys.dont_write_bytecode = True
from pathlib import Path
here = Path(sys.argv[1])
sys.path.insert(0, str(here))
from testrunner import cli, run, scratch
if sys.argv[2] == 'disk':
    scratch.choices = lambda bound=scratch.BOUND: iter(())
if sys.argv[3] != '-':
    run.GRACE = float(sys.argv[3])
files = [here / "testrun.py", *sorted((here / "testrunner").glob("*.py"))]
sys.exit(cli.main(sys.argv[4:], files))
'''
STOP_GRACE = 1.0  # seconds: the stop grace for runs whose test ignores SIGINT, so the tool always escalates


def tool(storage, grace=None):
    """The command that starts this checkout's test tool, with its storage as the case asks, and its stop grace."""
    if storage == 'disk' or grace is not None:
        return [sys.executable, '-I', '-c', TOOL, str(SOURCE / 'tests'), storage, '-' if grace is None else str(grace)]
    return [sys.executable, '-I', str(SOURCE / 'tests/testrun.py')]


def tool_env(project):
    env = dict(os.environ, TMPDIR=str(project), OPENBLAS_NUM_THREADS='1')
    for key in ('TMUX', 'TMUX_PANE', 'TMUX_TMPDIR', 'QD_TMUX_SOCKET', 'QD_SHIP', 'QD_SEAT',
                'TESTRUNS_RUN', 'TESTRUN_SESSION_RECORDS'):
        env.pop(key, None)
    return env


@pytest.mark.parametrize('storage', STORAGE)
def test_tool_stopped_during_test_cleans_server_and_next_run_uses_memory(monkeypatch, storage):
    """A tool stopped while its test holds a private server cleans the server and its scratch; the next run takes
    its storage afresh: memory where there is memory, the disk fallback where there is none."""
    needs_memory(storage)
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
        command = [*tool(storage, STOP_GRACE), 'run', '--target', str(project), '-n', '0', '--timeout', '180']
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
            assert storage_kind(storage, result['working_storage']['kind']), result['working_storage']
            assert not Path(result['working_storage']['path']).exists(), 'the stopped run left no scratch'
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
            assert storage_kind(storage, passed['working_storage']['kind']), passed['working_storage']
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


@pytest.mark.parametrize('storage', STORAGE)
def test_server_ownership_holds_with_several_runs_in_memory_at_once(storage):
    """Two runs of two changes of one project run at once, each test with its private tmux server under the
    project's one socket anchor: in memory together where there is room for both, each on the disk fallback where
    there is none. The first run's runner dies, leaving its test and server alive. Stopping the second run stops only
    its own server and retires only its own scratch, and in memory its own reservation; the first run's server, test,
    scratch and reservation stand until the first run is retired by name, which then ends its server."""
    needs_memory(storage, runs=2)
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
        processes, pids, runs = {}, {}, {}

        def start(name, target):
            processes[name] = subprocess.Popen(
                [*tool(storage, STOP_GRACE), 'run', '--target', str(target), '-n', '0', '--timeout', '180',
                 'tests/test_hold.py'],
                env=env, cwd=project, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            deadline = time.monotonic() + 60
            while not ready[name].exists() and processes[name].poll() is None and time.monotonic() < deadline:
                time.sleep(.05)
            assert ready[name].exists(), (processes[name].communicate(timeout=10)[0]
                                          if processes[name].poll() is not None else f'{name} not ready')
            while len(ready[name].read_text().split()) < 2 and time.monotonic() < deadline:
                time.sleep(.05)  # written, but perhaps not yet whole
            pids[name] = dict(zip(('server', 'test'), map(int, ready[name].read_text().split())))
            known = set(runs.values())
            found = [p for p in (project / '.testruns/results').iterdir() if p.is_dir() and p not in known]
            assert len(found) == 1, found
            runs[name] = found[0]

        def scratch_of(name):
            """The run's scratch folder, and in memory its reservation record, which disk has none of."""
            record = runs[name] / scratch.RECORD
            if storage == 'disk':
                assert not record.exists(), 'a run on the disk fallback holds no memory reservation'
                return project / '.testruns' / RUNS / runs[name].name, None
            data = json.loads(record.read_text())
            assert storage_kind(storage, data['kind']), data
            return Path(data['base']) / data['root'] / RUNS / data['run'], data

        def alive(pid):
            fields = procs.stat_fields(pid)
            return fields is not None and fields[0] != 'Z'

        try:
            start('a', project)
            processes['a'].kill()  # its runner dies; its test and its server live on in memory
            processes['a'].communicate(timeout=30)
            first_folder, first = scratch_of('a')
            reservation = runs['a'] / scratch.RECORD
            first_bytes = reservation.read_bytes() if first else None
            assert first_folder.is_dir()

            start('b', other)
            second_folder, second = scratch_of('b')
            assert second_folder.is_dir() and second_folder != first_folder
            if first:
                assert second['root'] == first['root'] and second['run'] != first['run'], 'memory beside the first'
            assert all(alive(pid) for name in 'ab' for pid in pids[name].values()), 'both tests and servers run'

            processes['b'].send_signal(signal.SIGTERM)
            output = processes['b'].communicate(timeout=55)[0]
            assert processes['b'].returncode == 4, output
            result = json.loads((runs['b'] / 'result.json').read_text())
            assert result['outcome']['status'] == 'interrupted'
            assert storage_kind(storage, result['working_storage']['kind']), result['working_storage']
            assert result['cleanup']['outcome'] == 'complete', result['cleanup']
            assert not alive(pids['b']['server']), 'the stopped run ended its own server'
            assert not second_folder.exists(), 'and removed its own scratch'
            assert not (runs['b'] / scratch.RECORD).exists(), 'and holds no reservation'
            assert alive(pids['a']['server']) and alive(pids['a']['test']), "the peer's server and test are untouched"
            assert first_folder.is_dir(), "and its scratch"
            if first:
                assert reservation.read_bytes() == first_bytes, "and its reservation"

            os.killpg(os.getpgid(pids['a']['test']), signal.SIGKILL)  # the first run's test ends; its server stays
            deadline = time.monotonic() + 10
            while alive(pids['a']['test']) and time.monotonic() < deadline:
                time.sleep(.05)
            assert alive(pids['a']['server']), 'a recorded server outlives its test'
            retired = subprocess.run([*tool(storage), 'retire', runs['a'].name],
                                     env=env, cwd=project, capture_output=True, text=True, timeout=120)
            assert retired.returncode == 0, retired.stdout + retired.stderr
            assert not alive(pids['a']['server']), 'retire by name ended the first run its own server'
            assert not first_folder.exists() and not reservation.exists()
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
