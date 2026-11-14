"""Where a run's ``tests`` may come from. A checkout whose tests folder is a regular package must import it
from there; one whose tests folder is not a package may import ``tests`` only as a namespace lying wholly in
it. A regular package from anywhere else, or a namespace portion outside it, refuses before pytest starts.
The namespace form that accepts is every other synthetic project's ordinary case."""
from __future__ import annotations

from pathlib import Path
import sys

import pytest

from tests.conftest import git
from tests.integration.test_testrun_ci_layout_gates import CI_CONFTEST
from tests.integration.testrun_synthetic import commit, make, result, scratch, tool

SITE = Path('.venv') / 'lib' / f'python{sys.version_info.major}.{sys.version_info.minor}' / 'site-packages'


def track_in_venv(worktree, relative, text):
    """A file the run's in-checkout venv will hold from the start: ignored, tracked on purpose."""
    path = worktree / SITE / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    git(worktree, 'add', '-f', '--', str(path))


def test_a_regular_tests_package_imports_from_the_checkouts_own_tests_folder(tmp_path):
    project, worktree = make(tmp_path, {'__init__.py': '', 'test_regular.py': '''
        import tests
        def test_regular_package():
            assert tests.__file__.endswith("tests/__init__.py")
        '''})
    done = tool('run', '--target', str(worktree), '-n', '0', cwd=project)
    assert done.returncode == 0, done.stdout + done.stderr
    found = result(project)
    assert found['outcome']['status'] == 'passed' and found['evidence']['counts']['passed'] == 2
    origin = Path(found['tools']['run_venv']['imports']['tests'])
    assert origin == Path(found['checkout']['path']) / 'tests' / '__init__.py'


@pytest.mark.parametrize('shadow', [
    ('tests/__init__.py', 'regular package elsewhere'),
    ('tests/portion.py', 'namespace portion elsewhere'),
], ids=['regular-elsewhere', 'namespace-portion-elsewhere'])
def test_tests_from_outside_the_checkouts_tests_folder_refuses_before_pytest(tmp_path, shadow):
    relative, text = shadow
    project, worktree = make(tmp_path, {'conftest.py': CI_CONFTEST})
    # Control: the in-checkout venv may hold tracked content of its own without any refusal.
    track_in_venv(worktree, 'synthetic_unrelated/__init__.py', 'VALUE = "unrelated"\n')
    commit(worktree, 'synthetic unrelated venv content')
    control = tool('run', '--ci-layout', '--target', str(worktree), '-n', '0', cwd=project)
    assert control.returncode == 0, control.stdout + control.stderr
    assert result(project)['outcome']['status'] == 'passed'
    track_in_venv(worktree, relative, f'# synthetic {text}\n')
    commit(worktree, f'synthetic {text}')
    done = tool('run', '--ci-layout', '--target', str(worktree), '-n', '0', cwd=project)
    assert done.returncode != 0, done.stdout + done.stderr
    found = result(project)
    note = found['outcome']['note'] or ''
    assert note.startswith('setup refused') and 'tests' in note, found['outcome']
    assert found['outcome']['pytest_exit'] is None and found['outcome']['status'] != 'passed'
    assert found['cleanup']['outcome'] == 'complete' and scratch(project) == []
