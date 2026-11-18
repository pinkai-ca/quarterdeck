"""Outer git after a jailed step must not read a git file the jail could write.

A throwaway project. The marker lives outside the jail: only an outer git that honours a planted
``core.fsmonitor`` can create it. Each planted file holds the run, and the report names that file.
"""
from __future__ import annotations

from pathlib import Path
import stat

import pytest

from tests.conftest import git
from tests.integration.test_testrun_jail import needs_a_jail
from tests.integration.testrun_synthetic import (admin_entries, controller_environment, commit, make,
                                                 needs_ci_memory, result, results, tool)

HOOK = """#!/bin/sh
touch {marker} 2>/dev/null || true
exit 0
"""

PLANT_GITFILE = """
import stat
import subprocess
from pathlib import Path

MARKER = {marker!r}
HOOK = {hook!r}

def plant(root: Path) -> None:
    evil = root / "evil.git"
    subprocess.check_call(["git", "init", "-q", "-b", "main", str(evil)])
    hook = root / "hook.sh"
    hook.write_text(HOOK)
    hook.chmod(0o755)
    subprocess.check_call(["git", "-C", str(evil), "config", "core.fsmonitor", str(hook)])
    (root / ".git").write_text("gitdir: %s\\n" % (evil / ".git"))

def test_redirects_the_checkout_gitfile():
    plant(Path.cwd())
"""

PLANT_COMMONDIR = """
import subprocess
from pathlib import Path

MARKER = {marker!r}
HOOK = {hook!r}

def plant(root: Path) -> None:
    pointer = (root / ".git").read_text().split(":", 1)[1].strip()
    evil = root / "evil.git"
    subprocess.check_call(["git", "init", "-q", "-b", "main", str(evil)])
    hook = root / "hook.sh"
    hook.write_text(HOOK)
    hook.chmod(0o755)
    subprocess.check_call(["git", "-C", str(evil), "config", "core.fsmonitor", str(hook)])
    (Path(pointer) / "commondir").write_text(str(evil / ".git") + "\\n")

def test_redirects_the_admin_commondir():
    plant(Path.cwd())
"""

PLANT_CONFIG = """
import subprocess
from pathlib import Path

MARKER = {marker!r}
HOOK = {hook!r}

def plant(root: Path) -> None:
    hook = root / "hook.sh"
    hook.write_text(HOOK)
    hook.chmod(0o755)
    subprocess.check_call(["git", "-C", str(root), "config", "core.fsmonitor", str(hook)])

def test_sets_fsmonitor_in_the_checkout_config():
    plant(Path.cwd())
"""

SETUP = """
from pathlib import Path
from setuptools import setup
{plant}
setup()
plant(Path(__file__).resolve().parent)
"""

CI_CONFTEST = """
import os, pathlib, tempfile
ROOT = pathlib.Path(__file__).resolve().parent.parent
def pytest_configure(config):
    if os.environ.get("TESTRUNS_RUN"):
        return
    temp = ROOT / ".testruns" / "t"
    temp.mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = str(temp)
    tempfile.tempdir = None
"""

GIT_WORKS = """
import subprocess
def test_git_in_the_jail_still_works():
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    assert len(head) == 40
    subprocess.check_call(["git", "status", "--porcelain"])
"""


def names(changed: list[str], suffix: str) -> list[str]:
    return [path for path in changed if path.endswith(suffix)]


def real_admin(changed: list[str]) -> list[str]:
    return [path for path in changed if '/.git/worktrees/' in path]


def assert_held(done, project, marker: Path, suffix: str) -> dict:
    assert done.returncode == 6, done.stdout + done.stderr
    assert not marker.exists()
    found = result(project)
    assert found['outcome']['status'] == 'held', found['outcome']
    changed = found['git_guard']['changed']
    named = names(changed, suffix)
    assert named, changed
    text = (results(project)[-1] / 'report.md').read_text()
    assert all(path in text for path in named)
    assert 'git guard held' in text
    assert found['cleanup']['outcome'] == 'complete', found['cleanup']
    assert scratch_gone(project)
    return found


def scratch_gone(project: Path) -> bool:
    from tests.integration.testrun_synthetic import scratch
    return scratch(project) == []


def plant_body(template: str, marker: Path) -> str:
    hook = HOOK.format(marker=marker)
    return template.format(marker=str(marker), hook=hook)


@needs_a_jail
def test_a_plain_jailed_run_still_passes(tmp_path):
    project, worktree = make(tmp_path, {'test_git.py': GIT_WORKS})
    done = tool('run', '--jail', '--target', str(worktree), '-n', '0', cwd=project)
    assert done.returncode == 0, done.stdout + done.stderr
    found = result(project)
    assert found['outcome']['status'] == 'passed'
    assert found['evidence']['counts']['passed'] == 2
    assert found['git_guard']['changed'] == []
    mounted = [item for item in found['jail']['binds'] if 'from' in item]
    assert len(mounted) == 1 and mounted[0]['kind'] == 'rw'
    assert mounted[0]['from'].endswith('/admin-copy')
    assert '/.git/worktrees/' in mounted[0]['path'] and mounted[0]['from'] != mounted[0]['path']
    assert found['cleanup']['outcome'] == 'complete'
    assert admin_entries(project) == ['feature']
    text = (results(project)[-1] / 'report.md').read_text()
    assert '- jailed: yes, by the tool at' in text
    assert scratch_gone(project)


def test_an_unjailed_run_is_unchanged(tmp_path):
    project, worktree = make(tmp_path, {'test_ok.py': 'def test_ok():\n    assert True\n'})
    done = tool('run', '--target', str(worktree), '-n', '0', cwd=project)
    assert done.returncode == 0, done.stdout + done.stderr
    found = result(project)
    assert found['outcome']['status'] == 'passed'
    assert found['jail'] == {'jailed': False}
    assert 'git_guard' not in found
    text = (results(project)[-1] / 'report.md').read_text()
    assert '- jailed: no' in text
    assert 'git guard held' not in text
    assert admin_entries(project) == ['feature']


@needs_a_jail
def test_a_jailed_test_that_redirects_the_checkout_gitfile_is_held(tmp_path):
    marker = tmp_path / 'marker'
    project, worktree = make(tmp_path, {'test_plant.py': plant_body(PLANT_GITFILE, marker)})
    done = tool('run', '--jail', '--target', str(worktree), '-n', '0', cwd=project)
    found = assert_held(done, project, marker, '/.git')
    assert names(found['git_guard']['changed'], '/commondir') == []


@needs_a_jail
def test_a_jailed_test_that_redirects_the_admin_commondir_is_held(tmp_path):
    marker = tmp_path / 'marker'
    project, worktree = make(tmp_path, {'test_plant.py': plant_body(PLANT_COMMONDIR, marker)})
    done = tool('run', '--jail', '--target', str(worktree), '-n', '0', cwd=project)
    found = assert_held(done, project, marker, '/commondir')
    assert names(found['git_guard']['changed'], '/admin-copy/commondir')
    assert real_admin(found['git_guard']['changed']) == []


@needs_a_jail
@pytest.mark.parametrize('template, suffix, copy_only', [
    (PLANT_GITFILE, '/.git', False),
    (PLANT_COMMONDIR, '/commondir', True),
], ids=['gitfile', 'commondir'])
def test_a_setup_py_at_install_plants_the_same_git_writes(tmp_path, template, suffix, copy_only):
    marker = tmp_path / 'marker'
    project, worktree = make(tmp_path, {'test_ok.py': 'def test_ok():\n    assert True\n'})
    body = plant_body(template, marker)
    plant, _, _ = body.partition('\ndef test_')
    (worktree / 'setup.py').write_text(SETUP.format(plant=plant))
    commit(worktree, 'setup.py plants a git write at install')
    done = tool('run', '--jail', '--target', str(worktree), '-n', '0', cwd=project)
    found = assert_held(done, project, marker, suffix)
    if copy_only:
        assert names(found['git_guard']['changed'], '/admin-copy/commondir')
        assert real_admin(found['git_guard']['changed']) == []


@needs_ci_memory
def test_a_ci_layout_test_that_sets_fsmonitor_in_git_config_is_held(tmp_path):
    marker = tmp_path / 'marker'
    project, worktree = make(tmp_path, {'conftest.py': CI_CONFTEST,
                                        'test_plant.py': plant_body(PLANT_CONFIG, marker)},
                             socket_stub=True)
    done = tool('run', '--ci-layout', '--target', str(worktree), '-n', '0', cwd=project,
                env=controller_environment())
    assert_held(done, project, marker, '/.git/config')


@needs_ci_memory
def test_a_ci_layout_setup_py_that_sets_fsmonitor_in_git_config_is_held(tmp_path):
    marker = tmp_path / 'marker'
    project, worktree = make(tmp_path, {'conftest.py': CI_CONFTEST,
                                        'test_ok.py': 'def test_ok():\n    assert True\n'},
                             socket_stub=True)
    body = plant_body(PLANT_CONFIG, marker)
    plant, _, _ = body.partition('\ndef test_')
    (worktree / 'setup.py').write_text(SETUP.format(plant=plant))
    commit(worktree, 'setup.py sets core.fsmonitor at install')
    done = tool('run', '--ci-layout', '--target', str(worktree), '-n', '0', cwd=project,
                env=controller_environment())
    assert_held(done, project, marker, '/.git/config')


@needs_a_jail
def test_a_jailed_run_that_fails_before_its_checkout_exists_says_so(tmp_path):
    project, worktree = make(tmp_path, {'test_ok.py': 'def test_ok():\n    assert True\n'})
    worktrees = project / '.git' / 'worktrees'
    mode = stat.S_IMODE(worktrees.stat().st_mode)
    worktrees.chmod(0o555)
    try:
        done = tool('run', '--jail', '--target', str(worktree), '-n', '0', cwd=project)
    finally:
        worktrees.chmod(mode)
    assert done.returncode != 0, done.stdout + done.stderr
    found = result(project)
    assert found['jail']['jailed'] is True
    assert 'bubblewrap' not in found['jail']
    text = (results(project)[-1] / 'report.md').read_text()
    assert '- jailed: no' not in text
    assert '- jailed: yes, failed early: failed before the checkout existed' in text
    git(project, 'status', '--porcelain')
