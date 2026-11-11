"""A synthetic project for tests of tests/testrun.py: its own Git repository with a tiny package, a main
branch and a feature worktree, so a tool run under test has its own .testruns, slot and Git admin, never
the real project's."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import time

from tests.conftest import ROOT, git

TOOL = ROOT / 'tests' / 'testrun.py'
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
    git(folder, 'commit', '-q', '-m', message)
    return git(folder, 'rev-parse', 'HEAD')


def make(tmp_path: Path, tests: dict[str, str], *, branch: str = 'feature') -> tuple[Path, Path]:
    project = tmp_path / 'p'
    (project / 'src' / 'synth').mkdir(parents=True)
    (project / 'tests').mkdir()
    git(project, 'init', '-q', '-b', 'main')
    (project / 'pyproject.toml').write_text(PYPROJECT)
    (project / '.gitignore').write_text('.testruns/\nwork/\n.venv/\n__pycache__/\n*.egg-info/\n')
    (project / 'src' / 'synth' / '__init__.py').write_text('VALUE = 1\n')
    for name in 'abcdefgh':
        (project / 'src' / 'synth' / f'{name}.py').write_text(f'NAME = {name!r}\n')
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


def tool(*args: str, cwd: Path, env: dict | None = None, timeout: float = 300) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, '-I', str(TOOL), *args], cwd=cwd, env=env or dict(os.environ),
                          capture_output=True, text=True, timeout=timeout)


def start(*args: str, cwd: Path) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, '-I', str(TOOL), *args], cwd=cwd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)


def results(project: Path) -> list[Path]:
    folder = project / '.testruns' / 'results'
    return sorted(folder.iterdir()) if folder.is_dir() else []


def result(project: Path, run: Path | None = None) -> dict:
    run = run or results(project)[-1]
    return json.loads((run / 'result.json').read_text())


def scratch(project: Path) -> list[str]:
    folder = project / '.testruns' / 'runs'
    return sorted(os.listdir(folder)) if folder.is_dir() else []


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
