"""Synthetic projects for clean, keep and abandoned-run recovery. A retired worktree may have ignored
bytecode caches. Other unrecorded material, in-use worktrees and replaced identities hold cleanup."""
import json
import os
from pathlib import Path
import signal
import subprocess

import pytest

from tests.conftest import git
from tests.integration.testrun_synthetic import (WAIT_TEST, add_worktree, admin_entries, commit, make, result,
                                                 results, scratch, start, started_marker, tool, wait_for)
from tests.testrunner import clean, cli


def registration(project, change='feature'):
    return json.loads((project / '.testruns' / 'changes' / f'{change}.json').read_text())


def ran(project, worktree, failing=False):
    done = tool('run', '--target', str(worktree), '-n', '0', cwd=project)
    assert done.returncode == (1 if failing else 0), done.stdout + done.stderr
    return results(project)[-1]


def bytecode_in(parent):
    cache = parent / '__pycache__'
    cache.mkdir()
    bytecode = cache / 'a.cpython-314.pyc'
    bytecode.write_bytes(b'synthetic bytecode')
    return bytecode


def test_a_merged_change_is_retired_and_its_results_reports_and_failures_stay(tmp_path):
    project, worktree = make(tmp_path, {'test_fail.py': 'def test_fails():\n    assert False, "kept failure"\n'})
    run = ran(project, worktree, failing=True)
    git(project, 'merge', '-q', '--ff-only', 'feature')
    done = tool('clean', '--change', 'feature', cwd=project)
    assert done.returncode == 0, done.stdout + done.stderr
    assert not worktree.exists() and git(project, 'branch', '--list', 'feature') == ''
    assert admin_entries(project) == [] and scratch(project) == []
    assert sorted(os.listdir(run)) == ['failures', 'report.md', 'result.json']
    assert 'kept failure' in (run / 'failures' / '001.txt').read_text()
    assert registration(project)['retired']['merged'] is True
    assert f'removed worktree {worktree}' in done.stdout and "'files':" in done.stdout
    journal = json.loads((project / '.testruns' / 'changes' / 'feature.clean.json').read_text())
    assert [step['what'] for step in journal['done']] == [step['what'] for step in journal['plan']]
    again = tool('clean', '--change', 'feature', cwd=project)
    assert again.returncode == 0 and 'already retired' in again.stdout


def test_ignored_bytecode_caches_go_with_a_merged_worktree(tmp_path):
    project, worktree = make(tmp_path, {})
    run = ran(project, worktree)
    receipts = {name: (run / name).read_bytes() for name in ('result.json', 'report.md')}
    git(project, 'merge', '-q', '--ff-only', 'feature')
    root_bytecode = bytecode_in(worktree)
    nested_bytecode = bytecode_in(worktree / 'src' / 'synth')
    optimized = nested_bytecode.with_name('b.cpython-314.opt-1.pyc')
    optimized.write_bytes(b'more bytecode')

    done = tool('clean', '--change', 'feature', cwd=project)

    assert done.returncode == 0, done.stdout + done.stderr
    assert not worktree.exists() and git(project, 'branch', '--list', 'feature') == ''
    assert admin_entries(project) == []
    retired = registration(project)['retired']
    assert retired['merged'] and retired['removed'][0]['what'] == f'worktree {worktree}'
    journal = run.parent.parent / 'changes' / 'feature.clean.json'
    plan = json.loads(journal.read_text())['plan']
    recorded = (root_bytecode.parent, root_bytecode, nested_bytecode.parent, nested_bytecode, optimized)
    assert {str(path.relative_to(worktree)) for path in recorded} == plan[0]['bytecode'].keys()
    assert {name: (run / name).read_bytes() for name in receipts} == receipts


def test_an_unmerged_change_goes_only_by_a_recorded_drop_bound_to_its_tip(tmp_path):
    project, worktree = make(tmp_path, {})
    ran(project, worktree)
    refused = tool('clean', '--change', 'feature', cwd=project)
    assert refused.returncode == 2 and 'not merged' in refused.stderr and worktree.exists()
    no_reason = tool('clean', '--change', 'feature', '--drop', cwd=project)
    assert no_reason.returncode == 2 and '--reason' in no_reason.stderr
    tip = git(worktree, 'rev-parse', 'HEAD')
    done = tool('clean', '--change', 'feature', '--drop', '--reason', 'synthetic approach abandoned', cwd=project)
    assert done.returncode == 0, done.stdout + done.stderr
    assert not worktree.exists() and git(project, 'branch', '--list', 'feature') == ''
    drop = registration(project)['drop']
    assert drop['tip'] == tip and drop['reason'] == 'synthetic approach abandoned'
    assert f'git branch feature {tip}' in done.stdout


def test_new_work_after_a_recorded_drop_refuses(tmp_path):
    project, worktree = make(tmp_path, {})
    ran(project, worktree)
    (worktree / 'notes.txt').write_text('untracked')
    first = tool('clean', '--change', 'feature', '--drop', '--reason', 'synthetic', cwd=project)
    assert first.returncode == 2 and 'notes.txt' in first.stderr  # the drop is recorded, the dirty tree refuses
    commit(worktree, 'new work after the drop')
    for args in ((), ('--drop', '--reason', 'synthetic')):
        again = tool('clean', '--change', 'feature', *args, cwd=project)
        assert again.returncode == 2 and worktree.exists(), again.stderr
    assert git(project, 'branch', '--list', 'feature').strip().endswith('feature')


def test_unrecorded_ignored_material_refuses_by_name_and_nothing_goes(tmp_path):
    project, worktree = make(tmp_path, {})
    ran(project, worktree)
    git(project, 'merge', '-q', '--ff-only', 'feature')
    (worktree / '.venv').mkdir()
    (worktree / '.venv' / 'pyvenv.cfg').write_text('synthetic')
    bytecode = bytecode_in(worktree / 'src' / 'synth')
    done = tool('clean', '--change', 'feature', cwd=project)
    assert done.returncode == 2 and '.venv/' in done.stderr
    assert (worktree / '.venv' / 'pyvenv.cfg').read_text() == 'synthetic'
    assert bytecode.read_bytes() == b'synthetic bytecode' and git(project, 'branch', '--list', 'feature')


@pytest.mark.parametrize('material', ['notes.txt', 'directory.pyc', 'symlink.pyc', 'hardlink.pyc'])
def test_mixed_cache_contents_refuse_by_name_and_nothing_goes(tmp_path, material):
    project, worktree = make(tmp_path, {})
    ran(project, worktree)
    git(project, 'merge', '-q', '--ff-only', 'feature')
    bytecode = bytecode_in(worktree / 'src' / 'synth')
    unwanted = bytecode.with_name(material)
    target = tmp_path / 'keep.txt'
    target.write_text('outside the worktree')
    if material == 'directory.pyc':
        unwanted.mkdir()
    elif material == 'symlink.pyc':
        unwanted.symlink_to(target)
    elif material == 'hardlink.pyc':
        unwanted.hardlink_to(target)
    else:
        unwanted.write_text('mixed material')

    done = tool('clean', '--change', 'feature', cwd=project)

    assert done.returncode == 2 and str(unwanted.relative_to(worktree)) in done.stderr
    assert unwanted.exists() and bytecode.read_bytes() == b'synthetic bytecode'
    assert target.read_text() == 'outside the worktree'
    assert git(project, 'branch', '--list', 'feature') and registration(project)['retired'] is None


def test_tracked_and_untracked_changes_refuse_with_an_ignored_cache(tmp_path):
    project, worktree = make(tmp_path, {})
    ran(project, worktree)
    git(project, 'merge', '-q', '--ff-only', 'feature')
    bytecode = bytecode_in(worktree / 'tests')
    tracked = worktree / 'tests' / 'test_base.py'
    untracked = worktree / 'notes.txt'
    tracked.write_text('edited')
    untracked.write_text('untracked')

    done = tool('clean', '--change', 'feature', cwd=project)

    assert done.returncode == 2 and 'tests/test_base.py' in done.stderr and 'notes.txt' in done.stderr
    assert tracked.read_text() == 'edited' and untracked.read_text() == 'untracked'
    assert bytecode.read_bytes() == b'synthetic bytecode'


def test_cache_replaced_after_planning_refuses_before_git_removes_the_tree(tmp_path, monkeypatch, capsys):
    project, worktree = make(tmp_path, {})
    ran(project, worktree)
    git(project, 'merge', '-q', '--ff-only', 'feature')
    bytecode = bytecode_in(worktree / 'tests')
    replacement = tmp_path / 'replacement.pyc'
    replacement.write_bytes(bytecode.read_bytes())
    new_inode = replacement.stat().st_ino
    perform = clean.perform

    def replace_then_perform(project, step, drop):
        replacement.replace(bytecode)
        return perform(project, step, drop)

    monkeypatch.chdir(project)
    monkeypatch.setattr(clean, 'perform', replace_then_perform)

    assert cli.main(['clean', '--change', 'feature'], []) == 2
    assert 'tests/__pycache__' in capsys.readouterr().err
    assert bytecode.stat().st_ino == new_inode and git(project, 'branch', '--list', 'feature')
    assert registration(project)['retired'] is None


def test_a_worktree_in_use_or_replaced_refuses(tmp_path):
    project, worktree = make(tmp_path, {})
    ran(project, worktree)
    git(project, 'merge', '-q', '--ff-only', 'feature')
    bytecode = bytecode_in(worktree)
    user = subprocess.Popen(['sleep', '60'], cwd=worktree)
    try:
        done = tool('clean', '--change', 'feature', cwd=project)
        assert done.returncode == 2 and 'in use' in done.stderr and bytecode.exists()
    finally:
        user.kill()
        user.wait()
    moved = worktree.with_name('moved')
    worktree.rename(moved)
    worktree.mkdir()
    done = tool('clean', '--change', 'feature', cwd=project)
    assert done.returncode == 2 and 'not the registered worktree' in done.stderr
    assert moved.exists() and worktree.exists()


def test_main_and_unregistered_changes_are_never_cleaned(tmp_path):
    project, worktree = make(tmp_path, {})
    ran(project, worktree)
    for change in ('main', 'other'):
        done = tool('clean', '--change', change, cwd=project)
        assert done.returncode == 2 and 'not registered' in done.stderr
    assert worktree.exists()


def test_keep_holds_a_live_runs_scratch_past_its_end_and_past_clean(tmp_path):
    project, worktree = make(tmp_path, {'test_wait.py': WAIT_TEST})
    process = start('run', '--target', str(worktree), '-n', '0', cwd=project)
    marker = wait_for(lambda: started_marker(project))
    run = marker.parent.parent.name
    kept = tool('keep', run, '--reason', 'synthetic debugging', cwd=project)
    assert kept.returncode == 0, kept.stdout + kept.stderr
    marker.with_name('release').write_text('go')
    out, err = process.communicate(timeout=120)
    assert process.returncode == 0, out + err
    assert result(project)['cleanup']['outcome'] == 'kept' and scratch(project) == [run]
    git(project, 'merge', '-q', '--ff-only', 'feature')
    done = tool('clean', '--change', 'feature', cwd=project)
    assert done.returncode == 0 and f'kept scratch of runs [\'{run}\'] remains' in done.stdout
    assert scratch(project) == [run]
    finished = tool('keep', results(project)[-1].name, '--reason', 'too late', cwd=project)
    assert finished.returncode == 0  # the kept run's scratch is still there
    gone = tool('keep', '20200101-000000-abcd', '--reason', 'nothing', cwd=project)
    assert gone.returncode == 2 and 'no scratch left' in gone.stderr


def test_an_abandoned_run_is_recovered_by_its_own_change_only(tmp_path):
    hold = tmp_path / 'hold'
    hold.write_text('hold')
    project, worktree = make(tmp_path, {'test_wait.py': f'''
        import os, pathlib, time
        def test_waits():
            (pathlib.Path(os.environ["TMPDIR"]) / "started").write_text(str(os.getpid()))
            while pathlib.Path({str(hold)!r}).exists():
                time.sleep(0.1)
        '''})
    other = add_worktree(project, 'other', {})
    process = start('run', '--target', str(worktree), '-n', '0', cwd=project)
    marker = wait_for(lambda: started_marker(project))
    run = marker.parent.parent.name
    process.kill()  # the runner dies without its ending; its pytest session still holds the run lock
    process.wait()
    state = json.loads((marker.parent.parent / 'state.json').read_text())  # the run's own working storage
    # Only the base test: the change's own test would wait on the same hold until a time limit ended it.
    live = tool('run', '--target', str(worktree), '-n', '0', 'tests/test_base.py', cwd=project)
    assert live.returncode == 0 and f'run {run}: live' in live.stdout, live.stdout + live.stderr
    assert run in scratch(project) and marker.exists(), 'the live run is left alone'
    for pgid, _ in state['groups']:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    hold.unlink()
    elsewhere = tool('run', '--target', str(other), '-n', '0', cwd=project)
    assert elsewhere.returncode == 0, elsewhere.stdout + elsewhere.stderr
    assert f'run {run} of change feature has scratch left' in elsewhere.stdout and run in scratch(project)
    mine = tool('run', '--target', str(worktree), '-n', '0', cwd=project)
    assert mine.returncode == 0, mine.stdout + mine.stderr
    assert f'recovering abandoned run {run} of change feature' in mine.stdout
    assert run not in scratch(project)
    abandoned = json.loads((project / '.testruns' / 'results' / run / 'result.json').read_text())
    assert abandoned['outcome']['status'] == 'abandoned' and set(abandoned['evidence']['counts'].values()) == {None}
    assert (project / '.testruns' / 'results' / run / 'report.md').exists()
    assert sorted(admin_entries(project)) == ['feature', 'other']


def test_recovery_refuses_a_state_that_names_another_checkout_and_loses_nothing(tmp_path):
    hold = tmp_path / 'hold'
    hold.write_text('hold')
    project, worktree = make(tmp_path, {'test_wait.py': f'''
        import os, pathlib, time
        def test_waits():
            (pathlib.Path(os.environ["TMPDIR"]) / "started").write_text(str(os.getpid()))
            while pathlib.Path({str(hold)!r}).exists():
                time.sleep(0.1)
        '''})
    decoy = add_worktree(project, 'decoy', {})
    (decoy / 'uncommitted.txt').write_text('synthetic work in progress')
    process = start('run', '--target', str(worktree), '-n', '0', cwd=project)
    marker = wait_for(lambda: started_marker(project))
    run = marker.parent.parent.name
    process.kill()
    process.wait()
    state_file = marker.parent.parent / 'state.json'  # the run's own working storage, in memory or on disk
    state = json.loads(state_file.read_text())
    for pgid, _ in state['groups']:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    hold.unlink()
    own_admin = state['admin']
    tampered = [{**state, 'checkout': str(decoy)},
                {**state, 'admin': str(tmp_path / 'elsewhere' / 'admin')}]
    # The first through a run, the second through clean: both recover by the same clean.recover under a slot
    # before anything else; clean then refuses at the run's remaining scratch, with no test setup to pay for.
    for bad, verb in zip(tampered, (('run', '--target', str(worktree), '-n', '0'), ('clean', '--change', 'feature'))):
        state_file.write_text(json.dumps(bad))
        again = tool(*verb, cwd=project)
        if verb[0] == 'run':
            assert again.returncode == 0, again.stdout + again.stderr
        else:
            assert again.returncode == 2 and 'still have scratch' in again.stderr, again.stdout + again.stderr
        assert f'run {run}: abandoned · cleanup held' in again.stdout and 'nothing removed' in again.stdout
        assert (decoy / 'uncommitted.txt').read_text() == 'synthetic work in progress'
        assert str(decoy) in git_worktrees(project)
        assert run in scratch(project) and Path(state['checkout']).is_dir()
        assert os.path.isdir(own_admin) and json.loads(state_file.read_text()) == bad
    state_file.write_text(json.dumps(state))
    restored = tool('run', '--target', str(worktree), '-n', '0', cwd=project)
    assert restored.returncode == 0, restored.stdout + restored.stderr
    assert f'run {run}: abandoned · cleanup complete' in restored.stdout
    assert run not in scratch(project) and not os.path.exists(own_admin)
    assert sorted(admin_entries(project)) == ['decoy', 'feature']
    assert (decoy / 'uncommitted.txt').read_text() == 'synthetic work in progress'
    abandoned = json.loads((project / '.testruns' / 'results' / run / 'result.json').read_text())
    assert abandoned['outcome']['status'] == 'abandoned'


def git_worktrees(project):
    return [line[len('worktree '):] for line in git(project, 'worktree', 'list', '--porcelain').splitlines()
            if line.startswith('worktree ')]
