"""tests/testrun.py clean and keep, and recovery of an abandoned run, on synthetic projects: only a registered
change that is merged or deliberately dropped goes; anything unrecorded, live or replaced refuses; results,
reports and failure output survive; recovery touches its own change's runs only."""
import json
import os
import signal
import subprocess

from tests.conftest import git
from tests.integration.testrun_synthetic import (WAIT_TEST, add_worktree, admin_entries, commit, make, result,
                                                 results, scratch, start, started_marker, tool, wait_for)


def registration(project, change='feature'):
    return json.loads((project / '.testruns' / 'changes' / f'{change}.json').read_text())


def ran(project, worktree, failing=False):
    done = tool('run', '--target', str(worktree), '-n', '0', cwd=project)
    assert done.returncode == (1 if failing else 0), done.stdout + done.stderr
    return results(project)[-1]


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
    (worktree / 'src' / 'synth' / '__pycache__').mkdir()
    (worktree / 'src' / 'synth' / '__pycache__' / 'a.cpython-314.pyc').write_bytes(b'not the tool\'s')
    done = tool('clean', '--change', 'feature', cwd=project)
    assert done.returncode == 2 and '.venv/' in done.stderr and '__pycache__' in done.stderr
    assert (worktree / '.venv' / 'pyvenv.cfg').exists() and git(project, 'branch', '--list', 'feature')


def test_a_worktree_in_use_or_replaced_refuses(tmp_path):
    project, worktree = make(tmp_path, {})
    ran(project, worktree)
    git(project, 'merge', '-q', '--ff-only', 'feature')
    user = subprocess.Popen(['sleep', '60'], cwd=worktree)
    try:
        done = tool('clean', '--change', 'feature', cwd=project)
        assert done.returncode == 2 and 'in use' in done.stderr and worktree.exists()
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
    state = json.loads((project / '.testruns' / 'runs' / run / 'state.json').read_text())
    live = tool('run', '--target', str(worktree), '-n', '0', '--timeout', '5', cwd=project)
    assert f'run {run}: live' in live.stdout
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
