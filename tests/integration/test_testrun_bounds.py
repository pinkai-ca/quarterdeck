"""Resource exhaustion and timeout evidence on fresh synthetic projects only."""
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import tempfile

import pytest

from tests.testrunner import procs, resources

ROOT = Path(__file__).resolve().parents[2]


def command(argv, *, cwd, env=None, timeout=90):
    return subprocess.run(argv, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout)


@pytest.fixture
def synthetic_project(tmp_path):
    project = tmp_path / 'synthetic-bounds-zzzz'
    project.mkdir()
    (project / 'src/synthetic_bounds_zzzz').mkdir(parents=True)
    (project / 'src/synthetic_bounds_zzzz/__init__.py').write_text('')
    (project / 'tests').mkdir()
    (project / 'pyproject.toml').write_text('''[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"
[project]
name = "synthetic-bounds-zzzz"
version = "0.0.0"
[tool.setuptools.packages.find]
where = ["src"]
''')
    def commit():
        for args in [('init', '-q'), ('add', '.'), ('-c', 'user.name=synthetic', '-c',
                     'user.email=synthetic@example.invalid', 'commit', '-qm', 'Synthetic resource proof')]:
            done = command(['git', *args], cwd=project, timeout=30)
            assert done.returncode == 0, done.stderr
    return project, commit


def run_result(project, done):
    records = list((project / '.testruns/results').glob('*/result.json'))
    assert len(records) == 1, done.stdout + done.stderr
    result = json.loads(records[0].read_text())
    report = records[0].with_name('report.md').read_text()
    assert result['cleanup']['outcome'] == 'complete', result['cleanup']
    assert not list((project / '.testruns/runs').iterdir())
    working = Path(result['working_storage']['path'])
    assert not working.exists() and not working.is_symlink(), 'its working storage is gone, in memory as on disk'
    return result, report, records[0].parent


def test_tool_raises_its_own_open_file_limit_and_retires_deep_scratch(synthetic_project):
    project, commit = synthetic_project
    (project / 'tests/test_descriptors.py').write_text('''
import os, resource
def test_deep_working_tree():
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    assert soft >= 8192
    root = os.path.join(os.environ['TMPDIR'], 'deep-working-tree')
    os.mkdir(root)
    held = [os.open(root, os.O_RDONLY | os.O_DIRECTORY)]
    try:
        for _ in range(1500):
            os.mkdir('d', dir_fd=held[-1])
            held.append(os.open('d', os.O_RDONLY | os.O_DIRECTORY, dir_fd=held[-1]))
        leaf = os.open('bottom', os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=held[-1])
        os.close(leaf)
    finally:
        for fd in held:
            os.close(fd)
''')
    commit()
    driver = '''
import resource, runpy, sys
soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (1024, hard))
assert resource.getrlimit(resource.RLIMIT_NOFILE)[0] == 1024
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
'''
    done = command([sys.executable, '-B', '-I', '-c', driver, str(ROOT / 'tests/testrun.py'),
                    'run', '--target', str(project), '-n', '0', '--timeout', '60'], cwd=ROOT)
    result, report, _ = run_result(project, done)
    assert done.returncode == 0, done.stdout + done.stderr
    assert result['outcome']['status'] == 'passed' and result['evidence']['complete']
    assert result['evidence']['counts']['passed'] == 1
    limits = result['environment']['open_files']
    assert limits == dict(resource='RLIMIT_NOFILE', requested_soft=8192, soft=8192,
                          hard=resource.getrlimit(resource.RLIMIT_NOFILE)[1])
    assert '- open_files:' in report and '"soft": 8192' in report


def test_explicit_retirement_raises_its_own_limit_and_removes_deep_scratch(tmp_path):
    from tests.integration.test_testrun_retire import assert_original, crashed
    from tests.unit.test_testrun_scratch_walk import deep
    project, run, _, original = crashed(tmp_path)
    scratch = project / '.testruns/runs' / run
    deep(scratch, 1500, 'd', b'synthetic deep output')
    driver = '''
import resource, runpy, sys
resource.setrlimit(resource.RLIMIT_NOFILE, (1024, resource.getrlimit(resource.RLIMIT_NOFILE)[1]))
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
'''
    done = command([sys.executable, '-B', '-I', '-c', driver, str(ROOT / 'tests/testrun.py'),
                    'retire', run], cwd=project)
    assert done.returncode == 0, done.stdout + done.stderr
    assert not scratch.exists()
    retirement = json.loads((project / '.testruns/retirements' / (run + '.json')).read_text())
    assert retirement['cleanup']['outcome'] == 'complete'
    assert retirement['cleanup']['removed']['dirs'] == 1501
    assert_original(project, run, original)


def test_tool_caps_an_older_target_and_retains_the_first_memory_failure(synthetic_project):
    project, commit = synthetic_project
    # No conftest in this old/synthetic target: the executing runner must bound it itself.
    (project / 'tests/test_memory.py').write_text('''
import os, resource
def test_synthetic_allocation():
    assert os.environ['OPENBLAS_NUM_THREADS'] == '1'
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    assert soft == hard == 6144 * 1024 * 1024
    bytearray(12288 * 1024 * 1024)
''')
    commit()
    done = command(['timeout', '--kill-after=10s', '80s', 'prlimit', '--as=8589934592', sys.executable,
                    '-I', str(ROOT / 'tests/testrun.py'), 'run', '--target', str(project), '-n', '0',
                    '--memory-mib', '6144', '--timeout', '60', 'tests/test_memory.py'], cwd=ROOT)
    result, report, folder = run_result(project, done)
    assert done.returncode == 1 and result['outcome']['status'] == 'failed'
    assert result['evidence']['counts']['failed'] == 1
    assert result['environment']['memory_mib'] == 6144
    assert result['environment']['fixed']['OPENBLAS_NUM_THREADS'] == '1'
    failures = list((folder / 'failures').glob('*.txt'))
    assert len(failures) == 1
    assert 'MemoryError' in failures[0].read_text() and 'MemoryError' in report


def test_wall_alarm_interrupts_synchronous_setup_and_retains_timeout(synthetic_project):
    project, commit = synthetic_project
    (project / 'tests/test_never_reached.py').write_text('def test_synthetic(): assert True\n')
    commit()
    driver = '''
import sys, time
from pathlib import Path
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])
from testrunner import cli, environment
from testrunner import run
run.SETUP_SECONDS = 0.3
environment.tool_versions = lambda env: time.sleep(60)
files = [Path(sys.argv[1]) / "testrun.py", *sorted((Path(sys.argv[1]) / "testrunner").glob("*.py"))]
raise SystemExit(cli.main(["run", "--target", sys.argv[2], "-n", "0", "--timeout", "60"], files))
'''
    done = command(['timeout', '--kill-after=5s', '15s', 'prlimit', '--as=8589934592', sys.executable,
                    '-c', driver, str(ROOT / 'tests'), str(project)], cwd=ROOT, timeout=20)
    result, report, _ = run_result(project, done)
    assert done.returncode == 3 and result['outcome']['status'] == 'timeout'
    assert 'time limit of setup, 0.3 s' in report, 'setup is ended by its own bound, never by --timeout'
    assert result['seconds'] < 10
    assert result['outcome']['pytest_exit'] is None


def test_finalization_timeout_keeps_first_result_and_remaining_scratch(synthetic_project):
    project, commit = synthetic_project
    (project / 'tests/test_pass.py').write_text('def test_synthetic(): assert True\n')
    commit()
    driver = '''
import sys, time
from pathlib import Path
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])
import signal
from testrunner import cli, run
# The tool's own finalisation budget, unchanged: no runner's work before cleanup comes near it. Inside cleanup the
# stall reads the alarm the tool armed (armed, not yet fired), then leaps that alarm's clock so it fires now, while
# the stall holds: the tool's own handler takes the emergency path inside cleanup, and no real budget is waited out.
def stall(*args):
    remaining, _ = signal.getitimer(signal.ITIMER_REAL)
    Path(sys.argv[2], "cleanup-entered").write_text(repr([time.monotonic(), remaining, run.FINALIZATION_SECONDS]))
    signal.setitimer(signal.ITIMER_REAL, 0.001)
    time.sleep(60)  # held until the alarm interrupts it; never outlasted
run.clean.retire_run = stall
timeout = run.Runner.on_finalization_timeout
def fired(self, number, frame):
    Path(sys.argv[2], "timeout-fired").write_text(repr(time.monotonic()))
    return timeout(self, number, frame)
run.Runner.on_finalization_timeout = fired
files = [Path(sys.argv[1]) / "testrun.py", *sorted((Path(sys.argv[1]) / "testrunner").glob("*.py"))]
raise SystemExit(cli.main(["run", "--target", sys.argv[2], "-n", "0", "--timeout", "60"], files))
'''
    done = command(['timeout', '--kill-after=10s', '80s', 'prlimit', '--as=8589934592', sys.executable,
                    '-c', driver, str(ROOT / 'tests'), str(project)], cwd=ROOT)
    assert done.returncode == 7, done.stdout + done.stderr
    records = list((project / '.testruns/results').glob('*/result.json'))
    assert len(records) == 1
    first = json.loads(records[0].read_text())
    emergency = json.loads(records[0].with_name('emergency.json').read_text())
    assert first['outcome']['status'] == 'passed' and first['cleanup']['outcome'] == 'pending'
    assert '1 passed' in records[0].with_name('report.md').read_text()
    assert emergency['outcome'] == 'finalization-timeout' and emergency['scratch_retained']
    assert Path(emergency['scratch']).is_dir()
    entered, remaining, budget = json.loads((project / 'cleanup-entered').read_text())
    fired = float((project / 'timeout-fired').read_text())
    assert 0 < remaining <= budget, 'cleanup began under the armed finalisation budget, not yet fired'
    assert entered < fired, 'the budget fired inside cleanup, after the first result was written'
    assert 'first result and remaining scratch retained' in done.stderr


@pytest.mark.parametrize('workers', ['0', '2'])
def test_direct_pytest_and_xdist_apply_the_shared_cap_before_collection(tmp_path, workers):
    folder = tmp_path / 'synthetic-pytest-zzzz'
    folder.mkdir()
    # Load the actual shared conftest as a plugin, without any inherited test body.
    (folder / 'test_limits.py').write_text('''
import json, resource, subprocess, sys
import pytest
@pytest.mark.parametrize("case", ["synthetic-one-zzzz", "synthetic-two-zzzz"])
def test_actual_limits(case, pytestconfig):
    assert pytestconfig._qd_memory_limit == 6144 * 1024 * 1024
    assert resource.getrlimit(resource.RLIMIT_AS) == (6144 * 1024 * 1024,) * 2
    done = subprocess.run([sys.executable, "-c", "import json,resource; print(json.dumps(resource.getrlimit(resource.RLIMIT_AS)))"],
        env={}, check=True, capture_output=True, text=True, timeout=10)
    assert json.loads(done.stdout) == [6144 * 1024 * 1024] * 2
    if case == "synthetic-two-zzzz":
        bytearray(12288 * 1024 * 1024)
''')
    env = {key: value for key, value in os.environ.items() if not key.startswith('PYTEST_') and key != 'TMUX'}
    env[resources.MEMORY_ENV] = '6144'
    anchor = Path(tempfile.mkdtemp(prefix='qdb-', dir='/tmp'))
    original = anchor.stat()
    env.update(TMUX_TMPDIR=str(anchor), QD_TMUX_SOCKET=str(anchor / 'socket'))
    try:
        done = command(['timeout', '--kill-after=10s', '45s', 'prlimit', '--as=8589934592', sys.executable,
                        '-m', 'pytest', '-q', '-p', 'tests.conftest', '--confcutdir=' + str(folder),
                        '-n', workers, str(folder)], cwd=ROOT, env=env, timeout=60)
    finally:
        assert anchor.stat().st_ino == original.st_ino and anchor.stat().st_dev == original.st_dev
        anchor.rmdir()
    assert done.returncode == 1, done.stdout + done.stderr
    assert '1 failed, 1 passed' in done.stdout
    assert 'allocation refused within this test process' in done.stdout


# --timeout bounds pytest alone, from its start; setup has its own bound. So a short wall still reaches the synthetic
# test however long the checkout and the environment take (all of 8 s on a loaded GitHub runner), and stops it within
# its sleep. Collection is inside the wall: 3 s leaves it room.
WALL = 3
SLEEP = 300


def test_wall_timeout_stops_its_own_running_pytest_group(synthetic_project):
    project, commit = synthetic_project
    marker = project / 'synthetic-child-pid'
    (project / 'tests/test_wait.py').write_text(f'''
import subprocess, sys, time
from pathlib import Path
def test_synthetic_wait():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep({SLEEP})"])
    Path({str(marker)!r}).write_text(str(child.pid))
    time.sleep({SLEEP})
''')
    commit()
    done = command(['timeout', '--kill-after=10s', f'{WALL + 70}s', 'prlimit', '--as=8589934592', sys.executable,
                    '-I', str(ROOT / 'tests/testrun.py'), 'run', '--target', str(project), '-n', '0',
                    '--timeout', str(WALL), 'tests/test_wait.py'], cwd=ROOT, timeout=WALL + 85)
    result, report, _ = run_result(project, done)
    assert done.returncode == 3 and result['outcome']['status'] == 'timeout'
    assert result['outcome']['pytest_exit'] is not None, f'setup took the whole {WALL} s wall: pytest never started'
    assert f'time limit of {WALL}.0 s' in report
    assert marker.exists(), f'the synthetic test never started within the {WALL} s wall'
    pid = int(marker.read_text())
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    except FileNotFoundError:
        pass
    else:
        assert fields[0] in procs.EXITED, 'owned synthetic child survived timeout termination'


# A clock of the run's own that leaps once, at a chosen moment: the bounds read it, real time does not pass.
LEAPING = '''
import sys, time, types
from pathlib import Path
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])
from testrunner import cli, environment, run
offset = [0.0]
run.time = types.SimpleNamespace(monotonic=lambda: time.monotonic() + offset[0], time=time.time, sleep=time.sleep,
                                 strftime=time.strftime)
def leap(seconds):
    offset[0] += seconds
{patch}
files = [Path(sys.argv[1]) / "testrun.py", *sorted((Path(sys.argv[1]) / "testrunner").glob("*.py"))]
raise SystemExit(cli.main(["run", "--target", sys.argv[2], "-n", "0", {args}], files))
'''


def leaping_run(project, patch, *args):
    driver = LEAPING.format(patch=patch, args=', '.join(repr(arg) for arg in args))
    return command(['timeout', '--kill-after=10s', '200s', 'prlimit', '--as=8589934592', sys.executable,
                    '-c', driver, str(ROOT / 'tests'), str(project)], cwd=ROOT, timeout=210)


def test_a_long_setup_is_not_counted_in_the_pytest_timeout(synthetic_project):
    """Setup that takes 500 s on the run's clock, inside its own 600 s, leaves pytest its whole --timeout."""
    project, commit = synthetic_project
    (project / 'tests/test_pass.py').write_text('def test_synthetic(): assert True\n')
    commit()
    versions = 'measured = environment.tool_versions\nenvironment.tool_versions = lambda env: (leap(500), measured(env))[1]'
    done = leaping_run(project, versions, '--timeout', '60')
    result, report, _ = run_result(project, done)
    assert done.returncode == 0 and result['outcome']['status'] == 'passed', done.stdout + done.stderr + report


def test_setup_past_its_own_bound_is_a_setup_timeout_whatever_the_pytest_timeout(synthetic_project):
    project, commit = synthetic_project
    (project / 'tests/test_never_reached.py').write_text('def test_synthetic(): assert True\n')
    commit()
    versions = 'measured = environment.tool_versions\nenvironment.tool_versions = lambda env: (leap(601), measured(env))[1]'
    done = leaping_run(project, versions, '--timeout', '86400')
    result, report, _ = run_result(project, done)
    assert done.returncode == 3 and result['outcome']['status'] == 'timeout'
    assert 'time limit of setup, 600 s' in report and result['outcome']['pytest_exit'] is None


def test_the_wait_for_a_slot_is_counted_in_neither_bound(synthetic_project):
    """A wait for a slot of 10 000 s on the run's clock, longer than setup's bound and --timeout together, ends
    neither: --wait alone governs it."""
    project, commit = synthetic_project
    (project / 'tests/test_pass.py').write_text('def test_synthetic(): assert True\n')
    commit()
    acquire = ('acquire = run.Slot.acquire\n'
               'def waited(self, *args, **kwargs):\n'
               '    leap(10000)\n'
               '    return acquire(self, *args, **kwargs)\n'
               'run.Slot.acquire = waited')
    done = leaping_run(project, acquire, '--timeout', '60')
    result, report, _ = run_result(project, done)
    assert done.returncode == 0 and result['outcome']['status'] == 'passed', done.stdout + done.stderr + report
