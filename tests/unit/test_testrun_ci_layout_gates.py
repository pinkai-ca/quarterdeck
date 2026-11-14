"""Independent gates for the test tool's opt-in CI layout: the measured-layout check that alone lets a run's
counts stand, the two environments, complete-history identity, old results and evidence read unchanged, and
retirement of an independent repository that refuses whatever its recorded state cannot vouch for."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from tests.conftest import git
from tests.testrunner import clean, environment, evidence, gitops, owned, report, run as runmod
from tests.testrunner.layout import RESULTS, RUNS, Refused, Root, identity

CHECKOUT = Path('/owned/synthetic-run/checkout')
TEMP = str(CHECKOUT / '.testruns' / 't')


def session(role, **changes):
    layout = {'role': role, 'cwd': str(CHECKOUT), 'TMPDIR': TEMP, 'tempdir': TEMP, 'testruns_run_present': False,
              'disk_only': True, 'registry_present': False,
              'basetemp': None if role == 'main' else f'{TEMP}/pytest-of-synthetic/pytest-0/popen-{role}'}
    layout.update(changes)
    return layout


def problems(layouts, workers):
    return runmod.ci_layout_problems({'session_layouts': layouts}, CHECKOUT, workers)


# -- the measured session layout --------------------------------------------------------------------------------
def test_every_session_in_the_requested_layout_raises_nothing():
    assert problems([session('main'), session('gw0'), session('gw1')], 2) == []
    assert problems([session('main')], 0) == []


def test_a_session_that_never_reported_its_layout_makes_the_evidence_incomplete():
    assert problems([session('main'), session('gw0')], 2) == ['CI layout session observations missing']
    assert problems([], 0) == ['CI layout session observations missing']
    # an old plugin's start event carries no layout: an ordinary run cannot pass for a CI-layout one
    assert problems([session('gw0'), session('gw1')], 2) == ['CI layout session observations missing']


@pytest.mark.parametrize('role', ['main', 'gw1'])
@pytest.mark.parametrize('field,value', [
    ('cwd', '/owned/synthetic-run/elsewhere'),
    ('TMPDIR', '/owned/synthetic-run/tmp'),
    ('tempdir', '/owned/synthetic-run/tmp'),
    ('testruns_run_present', True),
    ('testruns_run_present', None),
    ('disk_only', False),
    ('disk_only', None),
    ('registry_present', True),
    ('registry_present', None),
])
def test_each_departure_from_the_checkout_layout_is_named_for_its_session(role, field, value):
    layouts = [session('main'), session('gw0'), session('gw1')]
    layouts[[item['role'] for item in layouts].index(role)] = session(role, **{field: value})
    assert problems(layouts, 2) == [f'CI layout session {role} differs from the requested checkout/temp/environment']


def test_a_forced_basetemp_on_the_controller_is_refused_and_a_workers_derived_one_is_not():
    assert problems([session('main', basetemp='/owned/forced'), session('gw0')], 1) == [
        'CI layout session main differs from the requested checkout/temp/environment']
    assert problems([session('main'), session('gw0', basetemp=f'{TEMP}/pytest-of-synthetic/x')], 1) == []


def test_an_extra_session_is_checked_like_the_expected_ones():
    layouts = [session('main'), session('gw0'), session('gw7', cwd='/owned/elsewhere')]
    assert problems(layouts, 1) == ['CI layout session gw7 differs from the requested checkout/temp/environment']


# -- the two environments ---------------------------------------------------------------------------------------
OPERATOR = {'TESTRUNS_RUN': 'synthetic-outer-run', 'GIT_CONFIG_GLOBAL': '/synthetic/operator.gitconfig',
            'PIP_CONFIG_FILE': '/synthetic/operator-pip.conf', 'HOME': '/synthetic/operator-home',
            'GIT_DIR': '/synthetic/elsewhere.git', 'TMUX_TMPDIR': '/synthetic/operator-sockets'}


def built(tmp_path, monkeypatch, ci_layout):
    for name, value in OPERATOR.items():
        monkeypatch.setenv(name, value)
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    env, record = environment.build(scratch, 'synthetic-run', ci_layout=ci_layout)
    return scratch, env, record


def test_the_default_environment_is_exactly_what_it_was(tmp_path, monkeypatch):
    monkeypatch.delenv('TESTRUNS_CI_DISK', raising=False)
    scratch, env, record = built(tmp_path, monkeypatch, ci_layout=False)
    assert list(record['fixed'].items()) == [('PIP_NO_INDEX', '1'), ('PIP_NO_INPUT', '1'),
                                             ('PIP_DISABLE_PIP_VERSION_CHECK', '1'), ('PYTHONNOUSERSITE', '1'),
                                             ('OPENBLAS_NUM_THREADS', '1'), ('TESTRUNS_RUN', 'synthetic-run')]
    assert env['TESTRUNS_RUN'] == 'synthetic-run' and env['HOME'] == str(scratch / 'home')
    for name in ('GIT_CONFIG_GLOBAL', 'PIP_CONFIG_FILE', 'GIT_DIR', 'TMUX_TMPDIR'):
        assert name not in env and name in record['removed_names']
    assert not (scratch / 'home' / '.gitconfig').exists()


def test_the_ci_environment_carries_no_run_marker_and_only_its_own_git_identity(tmp_path, monkeypatch):
    scratch, env, record = built(tmp_path, monkeypatch, ci_layout=True)
    assert 'TESTRUNS_RUN' not in env and 'TESTRUNS_RUN' not in record['fixed']
    assert 'TESTRUNS_RUN' in record['removed_names']
    config = scratch / 'home' / '.gitconfig'
    assert env['GIT_CONFIG_GLOBAL'] == str(config) and env['GIT_CONFIG_NOSYSTEM'] == '1'
    assert env['PIP_CONFIG_FILE'] == '/dev/null' and env['HOME'] == str(scratch / 'home')
    assert env['TESTRUNS_CI_DISK'] == '1' and record['fixed']['TESTRUNS_CI_DISK'] == '1'
    assert 'GIT_DIR' not in env and 'TMUX_TMPDIR' not in env
    read = subprocess.run(['git', 'config', '--global', '--list'], env=env, cwd=tmp_path, capture_output=True,
                          text=True, check=True).stdout.splitlines()
    assert sorted(read) == ['user.email=ci@example.invalid', 'user.name=Quarterdeck CI']
    recorded = json.dumps(record)
    for value in OPERATOR.values():
        assert value not in recorded  # removed names are recorded, never their values


def test_a_nested_default_environment_carries_an_inherited_disk_model(tmp_path, monkeypatch):
    monkeypatch.setenv('TESTRUNS_CI_DISK', '1')
    scratch, env, record = built(tmp_path, monkeypatch, ci_layout=False)
    assert env['TESTRUNS_CI_DISK'] == '1' and record['fixed']['TESTRUNS_CI_DISK'] == '1'
    assert record['fixed']['TESTRUNS_RUN'] == 'synthetic-run'
    assert not (scratch / 'home' / '.gitconfig').exists()


# -- complete history -------------------------------------------------------------------------------------------
def repository(tmp_path, commits=3):
    folder = tmp_path / 'source'
    folder.mkdir()
    git(folder, 'init', '-q', '-b', 'main')
    for number in range(commits):
        (folder / 'file.txt').write_text(f'synthetic {number}\n')
        git(folder, 'add', '-A')
        git(folder, 'commit', '-q', '-m', f'synthetic {number}')
    return folder


def test_history_is_the_exact_ancestry_of_one_commit_whatever_the_refs(tmp_path):
    folder = repository(tmp_path)
    head = git(folder, 'rev-parse', 'HEAD')
    env = dict(os.environ)
    first = gitops.history(folder, head, env=env)
    assert first['commits'] == 3 and len(first['sha256']) == 64
    git(folder, 'branch', 'synthetic-unrelated', 'HEAD~2')
    git(folder, 'tag', 'synthetic-tag', 'HEAD~1')
    assert gitops.history(folder, head, env=env) == first
    parent = gitops.history(folder, git(folder, 'rev-parse', 'HEAD~1'), env=env)
    assert parent['commits'] == 2 and parent['sha256'] != first['sha256']


def test_a_shallow_source_is_refused(tmp_path):
    folder = repository(tmp_path)
    shallow = tmp_path / 'shallow'
    subprocess.run(['git', 'clone', '-q', '--depth', '1', f'file://{folder}', str(shallow)], check=True,
                   capture_output=True)
    with pytest.raises(Refused, match='complete target history'):
        gitops.history(shallow, git(shallow, 'rev-parse', 'HEAD'), env=dict(os.environ))


# -- old results and evidence -----------------------------------------------------------------------------------
def first_version_evidence(folder):
    events = [{'kind': 'start', 'role': 'main', 'plugin': 1, 'pytest': '9.0.2', 'xdist': None, 'python': '3.14.4'},
              {'kind': 'collected', 'role': 'main', 'nodeids': []},
              {'kind': 'finish', 'role': 'main', 'exitstatus': 5}]
    (folder / 'main.jsonl').write_text(''.join(json.dumps(event) + '\n' for event in events))
    return evidence.analyse(folder, 5)


def test_a_first_version_start_event_still_analyses_with_no_layout(tmp_path):
    found = first_version_evidence(tmp_path)
    assert found['complete'] is True and found['session_layouts'] == [] and found['plugin'] == [1]


def test_an_old_result_renders_as_an_isolated_run_and_never_as_ci_proof(tmp_path):
    old = {'run': 'synthetic-run', 'target': {'top': '/synthetic', 'head': 'synthetic-tip', 'tree': 'synthetic-tree'},
           'runner': {}, 'outcome': {'status': 'no-tests', 'wrapper_exit': 5},
           'evidence': first_version_evidence(tmp_path)}
    rendered = report.render(old, '', [])
    assert 'mode: `isolated`; full CI-layout invocation: False' in rendered
    assert '- ci_layout:' not in rendered and 'pytest session layouts: `[]`' in rendered


# -- retiring an independent repository ---------------------------------------------------------------------------
class Scratch:
    """One owned run folder in a synthetic project, holding an independent checkout, as run.py makes it."""

    def __init__(self, tmp_path):
        self.project = tmp_path / 'project'
        self.project.mkdir()
        subprocess.run(['git', '-c', 'init.templateDir=', 'init', '--quiet', str(self.project)], check=True)
        self.root = Root(self.project)
        self.run = 'synthetic-run'
        self.runs_fd = self.root.child(RUNS)
        self.scratch_fd = self.root.child(RUNS, self.run)
        self.results_fd = self.root.child(RESULTS, self.run)
        self.scratch = self.root.path / RUNS / self.run
        self.checkout = self.scratch / 'checkout'
        token = owned.mark(self.scratch_fd)
        subprocess.run(['git', '-c', 'init.templateDir=', 'init', '--quiet', str(self.checkout)], check=True)
        (self.checkout / 'tracked.txt').write_text('synthetic\n')
        self.state = {'scratch': identity(os.fstat(self.scratch_fd)), 'owner': token,
                      'repository_kind': 'independent', 'checkout': str(self.checkout),
                      'checkout_identity': identity(self.checkout.lstat()), 'admin': str(self.checkout / '.git')}

    def retire(self, **changes):
        return clean.retire_run(self.root, self.runs_fd, self.run, {**self.state, **changes})

    def close(self):
        for fd in (self.results_fd, self.scratch_fd, self.runs_fd):
            os.close(fd)
        self.root.close()


@pytest.fixture
def made(tmp_path):
    made = Scratch(tmp_path)
    try:
        yield made
    finally:
        made.close()


@pytest.mark.parametrize('changes', [
    {'admin': 'ELSEWHERE'},
    {'checkout': 'ELSEWHERE'},
    {'admin': 'PROJECT_WORKTREES'},
    {'repository_kind': None},
])
def test_a_state_that_cannot_vouch_for_its_own_repository_is_held_and_nothing_goes(made, tmp_path, changes):
    elsewhere = tmp_path / 'synthetic-elsewhere'
    elsewhere.mkdir()
    (elsewhere / 'keep.txt').write_text('not the run\'s\n')
    places = {'ELSEWHERE': str(elsewhere), 'PROJECT_WORKTREES': str(made.project / '.git' / 'worktrees' / 'x')}
    changes = {key: places.get(value, value) for key, value in changes.items()}
    cleanup = made.retire(**changes)
    assert cleanup['outcome'] == 'held' and 'nothing removed' in cleanup['reason']
    assert (made.checkout / '.git').is_dir() and (made.checkout / 'tracked.txt').exists()
    assert (elsewhere / 'keep.txt').exists()


def test_a_replaced_independent_checkout_is_held_and_its_replacement_kept(made):
    os.rename(made.checkout, made.scratch / 'original')
    made.checkout.mkdir()
    (made.checkout / 'replacement.txt').write_text('synthetic replacement\n')
    cleanup = made.retire()
    assert cleanup['outcome'] == 'held' and 'identity changed' in cleanup['reason']
    assert (made.checkout / 'replacement.txt').exists() and (made.scratch / 'original' / '.git').is_dir()


def test_retiring_an_independent_repository_issues_no_project_git_action(made):
    before = sorted(os.listdir(made.project / '.git'))
    cleanup = made.retire()
    assert cleanup['outcome'] == 'complete' and not made.scratch.exists()
    assert cleanup['git_admin'] == {'path': str(made.checkout / '.git'), 'removed': True,
                                    'repository_kind': 'independent', 'external_admin': False}
    assert cleanup['removed']['files'] >= 1
    assert sorted(os.listdir(made.project / '.git')) == before and not (made.project / '.git' / 'worktrees').exists()
