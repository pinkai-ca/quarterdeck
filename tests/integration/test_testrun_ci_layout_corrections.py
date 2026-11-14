"""The two corrections to the CI-layout mode: the import proof accepts the project's code only from the run
checkout's own source, never from elsewhere in the checkout nor from the run's venv inside it; and an
independent repository is retired only when its ``.git`` is the real directory the run made."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys

import pytest

from tests.conftest import git
from tests.integration.testrun_synthetic import (admin_entries, commit, make, result, scratch, start, started_marker,
                                                 tool, wait_for)
from tests.testrunner import clean


def refused_before_pytest(project, name):
    found = result(project)
    note = found['outcome']['note'] or ''
    assert note.startswith('setup refused') and name in note, found['outcome']
    assert found['outcome']['pytest_exit'] is None and found['outcome']['status'] != 'passed'
    assert found['cleanup']['outcome'] == 'complete' and scratch(project) == []


def test_a_package_that_resolves_outside_the_checkouts_source_is_refused(tmp_path):
    project, worktree = make(tmp_path, {})
    pyproject = worktree / 'pyproject.toml'
    pyproject.write_text(pyproject.read_text().replace('where = ["src"]', 'where = ["lib"]'))
    (worktree / 'lib' / 'synth').mkdir(parents=True)
    (worktree / 'lib' / 'synth' / '__init__.py').write_text('VALUE = "outside src"\n')
    commit(worktree, 'synthetic package mapped outside src')
    done = tool('run', '--target', str(worktree), '-n', '0', cwd=project)
    assert done.returncode != 0, done.stdout + done.stderr
    refused_before_pytest(project, 'synth')


def test_a_package_that_resolves_from_the_ci_venv_inside_the_checkout_is_refused(tmp_path):
    project, worktree = make(tmp_path, {})
    version = f'python{sys.version_info.major}.{sys.version_info.minor}'
    shadow = worktree / '.venv' / 'lib' / version / 'site-packages' / 'synth'
    shadow.mkdir(parents=True)
    (shadow / '__init__.py').write_text('VALUE = "from the run venv"\n')
    git(worktree, 'add', '-f', '--', str(shadow / '__init__.py'))  # ignored path, tracked on purpose
    commit(worktree, 'synthetic package shadowed in the venv')
    done = tool('run', '--ci-layout', '--target', str(worktree), '-n', '0', cwd=project)
    assert done.returncode != 0, done.stdout + done.stderr
    refused_before_pytest(project, 'synth')


def test_a_linked_runs_state_relabelled_independent_is_held_and_its_admin_entry_kept(tmp_path):
    hold = tmp_path / 'hold'
    hold.write_text('hold')
    project, worktree = make(tmp_path, {'test_wait.py': f'''
        import os, pathlib, time
        def test_waits():
            (pathlib.Path(os.environ["TMPDIR"]) / "started").write_text(str(os.getpid()))
            while pathlib.Path({str(hold)!r}).exists():
                time.sleep(0.1)
        '''})
    process = start('run', '--target', str(worktree), '-n', '0', cwd=project)
    marker = wait_for(lambda: started_marker(project))
    run = marker.parent.parent.name
    process.kill()
    process.wait()
    state_file = project / '.testruns' / 'runs' / run / 'state.json'
    state = json.loads(state_file.read_text())
    for pgid, _ in state['groups']:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    hold.unlink()
    linked_admin = state['admin']
    assert Path(linked_admin).parent == project / '.git' / 'worktrees'
    relabelled = {**state, 'repository_kind': 'independent', 'admin': state['checkout'] + '/.git'}
    state_file.write_text(json.dumps(relabelled))
    again = tool('run', '--target', str(worktree), '-n', '0', cwd=project)
    assert again.returncode == 0, again.stdout + again.stderr
    assert f'run {run}: abandoned · cleanup held' in again.stdout
    assert run in scratch(project) and (Path(state['checkout']) / '.git').is_file()
    assert os.path.isdir(linked_admin) and sorted(admin_entries(project)) == sorted(['feature', Path(linked_admin).name])
    state_file.write_text(json.dumps(state))
    restored = tool('run', '--target', str(worktree), '-n', '0', cwd=project)
    assert restored.returncode == 0, restored.stdout + restored.stderr
    assert f'run {run}: abandoned · cleanup complete' in restored.stdout
    assert run not in scratch(project) and admin_entries(project) == ['feature']


@pytest.mark.parametrize('replacement', ['gitfile', 'link'])
def test_an_independent_checkout_whose_git_is_not_a_real_directory_is_held(tmp_path, replacement):
    from tests.unit.test_testrun_ci_layout_gates import Scratch
    made = Scratch(tmp_path)
    try:
        own = made.checkout / '.git'
        os.rename(own, made.scratch / 'moved-git')
        if replacement == 'gitfile':
            own.write_text(f'gitdir: {made.project / ".git" / "worktrees" / "synthetic-linked"}\n')
        else:
            own.symlink_to(made.scratch / 'moved-git', target_is_directory=True)
        cleanup = clean.retire_run(made.root, made.runs_fd, made.run, made.state)
        assert cleanup['outcome'] == 'held', cleanup
        assert os.path.lexists(own) and (made.checkout / 'tracked.txt').exists()
        assert (made.scratch / 'moved-git').is_dir() and made.scratch.is_dir()
    finally:
        made.close()
