"""A synthetic project for tests of tests/testrun.py: its own Git repository with a tiny package, a main
branch and a feature worktree, so a tool run under test has its own .testruns, slot and Git admin, never
the real project's."""
from __future__ import annotations

import functools
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import textwrap
import time

import pytest

from tests.conftest import ROOT, git
from tests.testrunner import scratch

TOOL = ROOT / 'tests' / 'testrun.py'


def skip_without_ci_memory() -> None:
    """Skip when a fresh project's CI run would refuse for lack of a memory reservation.

    The measurement is ``prepare(..., memory_required=True)``: real tmpfs/ramfs with room
    for the reservation and the margin, ignoring the child disk model.
    """
    if scratch.eligible_memory(None, physical=True) is None:
        pytest.skip(scratch.NO_CI_MEMORY)


def needs_ci_memory(fn):
    """Mark one nested CI-layout gate. The skip carries the refusal's own reason."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        skip_without_ci_memory()
        return fn(*args, **kwargs)
    wrapper.__ci_memory_skip__ = skip_without_ci_memory
    return wrapper
PYPROJECT = '''[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "synth"
version = "0"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
'''


def commit(folder: Path, message: str) -> str:
    git(folder, 'add', '-A')
    git(folder, 'commit', '-q', '--allow-empty', '-m', message)
    return git(folder, 'rev-parse', 'HEAD')


def write_socket_stub(project: Path) -> None:
    """The CI session instrumentation patches ``OwnedSocket`` before it records a layout.

    A synthetic target is not the Quarterdeck checkout, so that import has to exist in the target itself.
    """
    folder = project / 'src' / 'quarterdeck'
    folder.mkdir()
    (folder / '__init__.py').write_text('')
    (folder / 'tmux_socket.py').write_text('class OwnedSocket:\n'
                                           '    def record(self, held, query):\n'
                                           '        return None\n')


def make(tmp_path: Path, tests: dict[str, str], *, branch: str = 'feature',
         socket_stub: bool = False) -> tuple[Path, Path]:
    project = tmp_path / 'p'
    (project / 'src' / 'synth').mkdir(parents=True)
    (project / 'tests').mkdir()
    git(project, 'init', '-q', '-b', 'main')
    (project / 'pyproject.toml').write_text(PYPROJECT)
    (project / '.gitignore').write_text('.testruns/\nwork/\n.venv/\n__pycache__/\n*.egg-info/\n')
    (project / 'src' / 'synth' / '__init__.py').write_text('VALUE = 1\n')
    for name in 'abcdefgh':
        (project / 'src' / 'synth' / f'{name}.py').write_text(f'NAME = {name!r}\n')
    if socket_stub:
        write_socket_stub(project)
    (project / 'tests' / 'test_base.py').write_text('def test_base():\n    assert True\n')
    commit(project, 'synthetic base')
    worktree = add_worktree(project, branch, tests)
    return project, worktree


def add_worktree(project: Path, branch: str, tests: dict[str, str]) -> Path:
    worktree = project / 'work' / branch
    git(project, 'worktree', 'add', '-q', '-b', branch, str(worktree))
    for name, body in tests.items():
        (worktree / 'tests' / name).write_text(textwrap.dedent(body))
    commit(worktree, f'synthetic tests on {branch}')
    return worktree


def controller_environment() -> dict:
    """The environment an operator-started controller has.

    A suite already running under ``--ci-layout`` carries the child disk model and the CI session context.
    Those belong to pytest. The controller under test sets them for its own children and must not inherit them.
    The suite's HOME is the outer run's scratch, so the controller is given the user base that run carried.
    A real shell resolves its tools from that same base, including pytest-xdist.
    """
    env = dict(os.environ)
    env.pop('TESTRUNS_CI_DISK', None)
    env.pop('TESTRUN_CI_SESSION_CONTEXT', None)
    base = env.get('TESTRUNS_USERBASE')
    if base and 'PYTHONUSERBASE' not in env:
        env['PYTHONUSERBASE'] = base
    return env


def tool(*args: str, cwd: Path, env: dict | None = None, timeout: float = 300) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, '-I', str(TOOL), *args], cwd=cwd, env=env or dict(os.environ),
                          capture_output=True, text=True, timeout=timeout)


LEAP_SECONDS = 86400.0
LEAP = r'''
import json, pathlib, sys, time, types
sys.dont_write_bytecode = True
tool, gate, leap = pathlib.Path(sys.argv[1]), sys.argv[2], float(sys.argv[3])
sys.path.insert(0, str(tool.parent))
from testrunner import cli, run
project, real, leapt = pathlib.Path.cwd(), time.monotonic, []
def opened():
    """The gate inside any run's recorded working storage, in memory or on disk."""
    runs = list((project / ".testruns" / "runs").glob("*"))
    for found in (project / ".testruns" / "results").glob("*/result.json"):
        try:
            runs.append(pathlib.Path(json.loads(found.read_text())["working_storage"]["path"]))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return any(next(folder.glob(gate), None) is not None for folder in runs)
def monotonic():
    if not leapt and opened():
        leapt.append(real())
        print(f"leapt at {leapt[0]!r}", file=sys.stderr, flush=True)
    return real() + (leap if leapt else 0.0)
run.time = types.SimpleNamespace(monotonic=monotonic, time=time.time, sleep=time.sleep, strftime=time.strftime)
sys.exit(cli.main(sys.argv[4:], [tool, *sorted((tool.parent / "testrunner").glob("*.py"))]))
'''


def tool_at_phase(gate: str, *args: str, cwd: Path, timeout: float = 300,
                  driver: str = LEAP) -> tuple[subprocess.CompletedProcess, float]:
    """The tool, its time-limit clock alone leaping a day ahead when ``gate`` (a glob inside the run's working
    storage, such as ``tmp/started``, wherever the run recorded it) first exists, so the limit ends the run in the
    phase the gate marks, however fast setup is. The real time of the leap is returned, on the system-wide
    monotonic clock."""
    done = subprocess.run([sys.executable, '-I', '-c', driver, str(TOOL), gate, str(LEAP_SECONDS), *args], cwd=cwd,
                          env=dict(os.environ), capture_output=True, text=True, timeout=timeout)
    leapt = re.search(r'^leapt at (\S+)$', done.stderr, re.M)
    assert leapt, 'the gate never opened, so no limit was forced:\n' + done.stdout + done.stderr
    return done, float(leapt.group(1))


def start(*args: str, cwd: Path, env: dict | None = None) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, '-I', str(TOOL), *args], cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)


def results(project: Path) -> list[Path]:
    folder = project / '.testruns' / 'results'
    return sorted(folder.iterdir()) if folder.is_dir() else []


def result(project: Path, run: Path | None = None) -> dict:
    run = run or results(project)[-1]
    return json.loads((run / 'result.json').read_text())


def working_paths(project: Path) -> list[Path]:
    """Every run's recorded working storage: memory or disk, from its own result, which stays on disk."""
    found = []
    for run in results(project):
        try:
            found.append(Path(json.loads((run / 'result.json').read_text())['working_storage']['path']))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return found


def scratch(project: Path) -> list[str]:
    """What is left of every run's working storage, wherever it lives."""
    folder = project / '.testruns' / 'runs'
    left = set(os.listdir(folder)) if folder.is_dir() else set()
    left.update(path.name for path in working_paths(project) if path.exists() or path.is_symlink())
    return sorted(left)


def admin_entries(project: Path) -> list[str]:
    folder = project / '.git' / 'worktrees'
    return sorted(os.listdir(folder)) if folder.is_dir() else []


def wait_for(predicate, seconds: float = 120.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError('condition not met in time')


def started_marker(project: Path) -> Path | None:
    """The marker a synthetic test writes into its run's own TMPDIR."""
    for path in (project / '.testruns' / 'runs').glob('*/tmp/started'):
        return path
    for folder in working_paths(project):
        if (folder / 'tmp' / 'started').exists():
            return folder / 'tmp' / 'started'
    return None


WAIT_TEST = '''
import os, pathlib, time
def test_waits():
    marker = pathlib.Path(os.environ["TMPDIR"]) / "started"
    marker.write_text(str(os.getpid()))
    release = marker.with_name("release")
    for _ in range(1200):
        if release.exists():
            return
        time.sleep(0.1)
'''


COLLECTION_WAIT = '''
import os, pathlib, time
marker = pathlib.Path(os.environ["TMPDIR"]) / "collecting"
marker.write_text(str(os.getpid()))
for _ in range(1200):
    if marker.with_name("release").exists():
        break
    time.sleep(0.1)
def test_collected_late():
    pass
'''
