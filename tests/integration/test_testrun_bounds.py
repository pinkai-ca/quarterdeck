"""Resource exhaustion and timeout evidence on fresh synthetic projects only."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest

from tests.testrunner import resources

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
environment.tool_versions = lambda env: time.sleep(60)
files = [Path(sys.argv[1]) / "testrun.py", *sorted((Path(sys.argv[1]) / "testrunner").glob("*.py"))]
raise SystemExit(cli.main(["run", "--target", sys.argv[2], "-n", "0", "--timeout", "0.3"], files))
'''
    done = command(['timeout', '--kill-after=5s', '15s', 'prlimit', '--as=8589934592', sys.executable,
                    '-c', driver, str(ROOT / 'tests'), str(project)], cwd=ROOT, timeout=20)
    result, report, _ = run_result(project, done)
    assert done.returncode == 3 and result['outcome']['status'] == 'timeout'
    assert 'time limit of 0.3 s' in report
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
from testrunner import cli, run
run.FINALIZATION_SECONDS = 0.3
def stall(*args):
    Path(sys.argv[2], "cleanup-entered").touch()
    time.sleep(60)
run.clean.retire_run = stall
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
    assert (project / 'cleanup-entered').exists()
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


def test_wall_timeout_stops_its_own_running_pytest_group(synthetic_project):
    project, commit = synthetic_project
    marker = project / 'synthetic-child-pid'
    (project / 'tests/test_wait.py').write_text(f'''
import subprocess, sys, time
from pathlib import Path
def test_synthetic_wait():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    Path({str(marker)!r}).write_text(str(child.pid))
    time.sleep(60)
''')
    commit()
    done = command(['timeout', '--kill-after=10s', '75s', 'prlimit', '--as=8589934592', sys.executable,
                    '-I', str(ROOT / 'tests/testrun.py'), 'run', '--target', str(project), '-n', '0',
                    '--timeout', '8', 'tests/test_wait.py'], cwd=ROOT, timeout=80)
    result, report, _ = run_result(project, done)
    assert done.returncode == 3 and result['outcome']['status'] == 'timeout'
    assert result['outcome']['pytest_exit'] is not None
    assert 'time limit of 8.0 s' in report
    pid = int(marker.read_text())
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    except FileNotFoundError:
        pass
    else:
        assert fields[0] == 'Z', 'owned synthetic child survived timeout termination'
