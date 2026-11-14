"""Independent gates for ``tests/testrun.py run --ci-layout`` on synthetic projects, beside the unchanged
default: an independent repository with the target's own history, CI's temp and Git discovery measured in
every session, counts withheld when the measured layout differs, no Git action on the project, and recovery
of an abandoned CI-layout run that holds whatever its state cannot vouch for."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal

from tests.conftest import git
from tests.integration.testrun_synthetic import (admin_entries, controller_environment, make, needs_ci_memory,
                                                 result, results, scratch, start, tool, wait_for, working_paths)

# What tests/conftest.py does for a plain pytest run, reduced to the synthetic project.
CI_CONFTEST = '''
    import os, pathlib, tempfile
    ROOT = pathlib.Path(__file__).resolve().parent.parent
    def pytest_configure(config):
        if os.environ.get("TESTRUNS_RUN"):
            return
        temp = ROOT / ".testruns" / "t"
        temp.mkdir(parents=True, exist_ok=True)
        os.environ["TMPDIR"] = str(temp)
        tempfile.tempdir = None
    '''

CI_SESSION = '''
    import json, os, pathlib, shutil, subprocess, sys, tempfile
    import pytest
    ROOT = pathlib.Path(__file__).resolve().parent.parent
    def git(folder, *args):
        return subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True)
    @pytest.mark.parametrize("round", range(3))
    def test_ci_layout_seen_from_inside(tmp_path, round):
        temp = ROOT / ".testruns" / "t"
        assert "TESTRUNS_RUN" not in os.environ
        assert pathlib.Path(os.environ["TMPDIR"]) == pathlib.Path(tempfile.gettempdir()) == temp
        assert tmp_path.resolve().is_relative_to(temp)
        nested = tmp_path / "synthetic-uninitialised"
        nested.mkdir()
        assert git(nested, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip() == str(ROOT / ".git")
        (nested / "product.txt").write_text("synthetic")
        assert git(nested, "add", "--", "product.txt").returncode != 0  # ignored inside the disposable checkout
        assert git(ROOT, "worktree", "list", "--porcelain").stdout.count("worktree ") == 1
        assert pathlib.Path(sys.prefix) == ROOT / ".venv"
        assert pathlib.Path(shutil.which("python")).parent == ROOT / ".venv" / "bin"
        assert git(ROOT, "config", "--global", "user.email").stdout.strip() == "ci@example.invalid"
        child = subprocess.run([sys.executable, "-c", "import os, tempfile; print(os.environ.get('TESTRUNS_RUN'), tempfile.gettempdir())"],
                               capture_output=True, text=True, check=True)
        assert child.stdout.split() == ["None", str(temp)]
    '''

DEFAULT_SESSION = '''
    import os, pathlib, subprocess, tempfile
    ROOT = pathlib.Path(__file__).resolve().parent.parent
    def test_default_layout_seen_from_inside(tmp_path):
        assert os.environ["TESTRUNS_RUN"]
        assert not tmp_path.resolve().is_relative_to(ROOT)
        assert not pathlib.Path(tempfile.gettempdir()).is_relative_to(ROOT)
        common = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                capture_output=True, text=True, check=True).stdout.strip()
        assert pathlib.Path(common) != ROOT / ".git"  # a linked checkout of the project
    '''


def project_git_state(project):
    """What a CI-layout run must leave exactly as it found it in the source project's repository."""
    objects = git(project, 'count-objects', '-v')
    refs = git(project, 'for-each-ref', '--format=%(refname) %(objectname)')
    return {'objects': objects, 'refs': refs, 'worktrees': git(project, 'worktree', 'list', '--porcelain'),
            'admin': admin_entries(project), 'config': (project / '.git' / 'config').read_text()}


def only_the_fixed_ending(project):
    run, = results(project)
    assert sorted(os.listdir(run)) == ['report.md', 'result.json']
    assert scratch(project) == []
    return run


@needs_ci_memory
def test_a_ci_layout_run_tests_the_target_in_an_independent_repository_and_touches_no_project_git(tmp_path):
    project, worktree = make(tmp_path, {'conftest.py': CI_CONFTEST, 'test_ci_session.py': CI_SESSION},
                              socket_stub=True)
    before = project_git_state(project)
    done = tool('run', '--ci-layout', '--target', str(worktree), '-n', '2', cwd=project,
                env=controller_environment())
    assert done.returncode == 0, done.stdout + done.stderr
    run = only_the_fixed_ending(project)
    found = result(project)
    head, tree = git(worktree, 'rev-parse', 'HEAD'), git(worktree, 'rev-parse', 'HEAD^{tree}')
    assert found['outcome']['status'] == 'passed' and found['evidence']['complete']
    assert found['evidence']['counts']['passed'] == 4 and found['evidence']['expected'] == 4
    assert found['mode'] == 'ci-layout' and found['ci_full_invocation'] is False  # named targets aside, -n 2
    checkout = found['checkout']
    assert checkout['head'] == head and checkout['tree'] == tree and checkout['repository_kind'] == 'independent'
    assert checkout['common_dir'] == checkout['admin'] == str(Path(checkout['path']) / '.git')
    commits = len(git(worktree, 'rev-list', head).splitlines())
    assert checkout['history'] == checkout['target_history'] and checkout['history']['commits'] == commits == 2
    assert found['ci_layout']['session_verified'] is True and found['ci_layout']['testruns_run_present'] is False
    assert {item['role'] for item in found['evidence']['session_layouts']} == {'main', 'gw0', 'gw1'}
    assert not any(part.startswith('--basetemp') or part == 'no:cacheprovider' for part in found['argv'])
    fixed = found['environment']['fixed']
    assert 'TESTRUNS_RUN' not in fixed and fixed['PIP_CONFIG_FILE'] == '/dev/null'
    assert fixed['VIRTUAL_ENV'] == str(Path(checkout['path']) / '.venv')
    assert Path(fixed['GIT_CONFIG_GLOBAL']).parent == Path(found['environment']['owned']['HOME'])
    assert found['cleanup']['outcome'] == 'complete'
    assert found['cleanup']['git_admin'] == {'path': checkout['admin'], 'removed': True,
                                             'repository_kind': 'independent', 'external_admin': False}
    assert found['manifests']['target']['unchanged']
    assert project_git_state(project) == before
    assert 'mode: `ci-layout`' in (run / 'report.md').read_text()


@needs_ci_memory
def test_counts_are_withheld_when_the_measured_session_layout_is_not_cis(tmp_path):
    # Without the conftest's plain layout every session keeps the tool's own TMPDIR: an ordinary layout
    # cannot pass for a CI-layout run, however its tests end.
    project, worktree = make(tmp_path, {'test_plain.py': 'def test_plain():\n    pass\n'}, socket_stub=True)
    done = tool('run', '--ci-layout', '--target', str(worktree), '-n', '2', cwd=project,
                env=controller_environment())
    assert done.returncode == 5, done.stdout + done.stderr
    only_the_fixed_ending(project)
    found = result(project)
    assert found['outcome']['status'] == 'incomplete' and found['outcome']['pytest_exit'] == 0
    assert set(found['evidence']['counts'].values()) == {None}
    assert found['ci_layout']['session_verified'] is False
    named = [cause for cause in found['evidence']['causes'] if cause.startswith('CI layout session ')]
    assert sorted(named) == [f'CI layout session {role} differs from the requested checkout/temp/environment'
                             for role in ('gw0', 'gw1', 'main')]
    assert found['cleanup']['outcome'] == 'complete'


def test_the_default_run_keeps_its_linked_layout_and_says_it_is_not_ci(tmp_path):
    project, worktree = make(tmp_path, {'conftest.py': CI_CONFTEST, 'test_default_session.py': DEFAULT_SESSION})
    done = tool('run', '--target', str(worktree), '-n', '2', cwd=project, env=controller_environment())
    assert done.returncode == 0, done.stdout + done.stderr
    run = only_the_fixed_ending(project)
    assert admin_entries(project) == ['feature']
    found = result(project)
    assert found['mode'] == 'isolated' and found['ci_full_invocation'] is False and 'ci_layout' not in found
    assert found['evidence']['counts']['passed'] == 2
    assert found['environment']['fixed']['TESTRUNS_RUN'] == run.name
    for name in ('GIT_CONFIG_GLOBAL', 'PIP_CONFIG_FILE', 'VIRTUAL_ENV'):
        assert name not in found['environment']['fixed']
    assert Path(found['environment']['fixed']['TMUX_TMPDIR']) == project / '.testruns' / 'r'
    assert any(part.startswith('--basetemp=') for part in found['argv']) and 'no:cacheprovider' in found['argv']
    assert 'repository_kind' not in found['checkout'] and 'common_dir' not in found['checkout']
    assert not Path(found['tools']['run_venv']['prefix']).is_relative_to(found['checkout']['path'])
    assert all(item['testruns_run_present'] is True for item in found['evidence']['session_layouts'])
    assert 'git_admin' in found['cleanup'] and 'command_ok' in found['cleanup']['git_admin']
    assert 'mode: `isolated`; full CI-layout invocation: False' in (run / 'report.md').read_text()


def ci_marker(project):
    """The synthetic test's marker in the independent checkout, whose working files live in memory."""
    for folder in working_paths(project):
        found = next(folder.glob('checkout-*/.testruns/t/started'), None)
        if found is not None:
            return found
    return None


@needs_ci_memory
def test_an_abandoned_ci_layout_run_is_held_on_an_untrustworthy_state_and_recovered_on_its_own(tmp_path):
    hold = tmp_path / 'hold'
    hold.write_text('hold')
    project, worktree = make(tmp_path, {'conftest.py': CI_CONFTEST, 'test_wait.py': f'''
        import os, pathlib, time
        def test_waits():
            (pathlib.Path(os.environ["TMPDIR"]) / "started").write_text(str(os.getpid()))
            while pathlib.Path({str(hold)!r}).exists():
                time.sleep(0.1)
        '''}, socket_stub=True)
    before = project_git_state(project)
    process = start('run', '--ci-layout', '--target', str(worktree), '-n', '0', cwd=project,
                    env=controller_environment())
    marker = wait_for(lambda: ci_marker(project))
    run_dir = marker.parents[3]  # <memory>/runs/<run>/checkout-<run>/.testruns/t/started
    run = run_dir.name
    process.kill()  # the runner dies without its ending
    process.wait()
    state_file = run_dir / 'state.json'
    state = json.loads(state_file.read_text())
    assert state['repository_kind'] == 'independent' and state['admin'] == state['checkout'] + '/.git'
    for pgid, _ in state['groups']:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    hold.unlink()
    elsewhere = tmp_path / 'synthetic-elsewhere'
    elsewhere.mkdir()
    (elsewhere / '.git').mkdir()
    untrusted = [{**state, 'admin': str(elsewhere / '.git')},
                 {**state, 'checkout': str(elsewhere)},
                 {key: value for key, value in state.items() if key != 'repository_kind'}]
    for bad in untrusted:
        state_file.write_text(json.dumps(bad))
        again = tool('run', '--target', str(worktree), '-n', '0', cwd=project, env=controller_environment())
        assert again.returncode == 0, again.stdout + again.stderr
        assert f'run {run}: abandoned · cleanup held' in again.stdout and 'nothing removed' in again.stdout
        assert run in scratch(project) and Path(state['admin']).is_dir() and (elsewhere / '.git').is_dir()
        assert json.loads(state_file.read_text()) == bad
    state_file.write_text(json.dumps(state))
    restored = tool('run', '--target', str(worktree), '-n', '0', cwd=project, env=controller_environment())
    assert restored.returncode == 0, restored.stdout + restored.stderr
    assert f'run {run}: abandoned · cleanup complete' in restored.stdout
    assert run not in scratch(project) and scratch(project) == []
    abandoned = json.loads((project / '.testruns' / 'results' / run / 'result.json').read_text())
    assert abandoned['outcome']['status'] == 'abandoned' and abandoned['mode'] == 'ci-layout'
    assert set(abandoned['evidence']['counts'].values()) == {None}
    assert project_git_state(project) == before
