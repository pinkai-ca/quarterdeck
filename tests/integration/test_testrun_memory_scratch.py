"""Memory working storage under retire, clean, jail and bounds: real synthetic runs whose scratch lives in tmpfs.

A run is abandoned by killing its runner while its test still waits, as a crash leaves it. Each case then changes
the memory storage or its record as the hazard it names, and the tool must hold, remove nothing, or retire exactly
what it says. Results on disk are never lost."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
from types import SimpleNamespace

import pytest

from tests.integration.testrun_synthetic import (WAIT_TEST, admin_entries, make, result, results, scratch, start,
                                               started_marker, tool, wait_for)
from tests.testrunner import scratch as storage

HOLDING = '''
import os, pathlib, time
def test_waits():
    (pathlib.Path(os.environ["TMPDIR"]) / "started").write_text(str(os.getpid()))
    while pathlib.Path({hold!r}).exists():
        time.sleep(0.1)
'''


def memory_available():
    return storage.choose() is not None


pytestmark = pytest.mark.skipif(not memory_available(), reason='no tmpfs or ramfs with 4 GiB free for working storage')


def abandoned(tmp_path, *, change='feature', live=False):
    """One run of ``change`` whose runner died; its test is stopped too unless ``live``."""
    hold = tmp_path / f'hold-{change}'
    hold.write_text('hold')
    project, worktree = make(tmp_path, {'test_wait.py': HOLDING.format(hold=str(hold))}, branch=change)
    process = start('run', '--target', str(worktree), '-n', '0', cwd=project)
    marker = wait_for(lambda: started_marker(project))
    folder = marker.parent.parent
    run = folder.name
    record = json.loads((project / '.testruns' / 'results' / run / 'storage.json').read_text())
    assert record['kind'] in ('tmpfs', 'ramfs'), 'the run is in memory'
    root = Path(record['base']) / record['root']
    assert folder == root / 'runs' / run
    process.kill()
    process.wait()
    state = json.loads((folder / 'state.json').read_text())
    if not live:
        stop_groups(state)
        hold.unlink()
    return SimpleNamespace(project=project, worktree=worktree, run=run, folder=folder, root=root, record=record,
                           state=state, hold=hold)


def remove(path):
    """Remove a whole tree a test made, its read-only package folders included."""
    for folder, _, _ in os.walk(path):
        os.chmod(folder, 0o700)
    shutil.rmtree(path)


def stop_groups(state):
    for pgid, _ in state['groups']:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def first_result(case):
    folder = case.project / '.testruns' / 'results' / case.run
    return {name: (folder / name).read_bytes() for name in ('result.json',)}


def retire(case):
    return tool('retire', case.run, cwd=case.project)


@pytest.fixture
def cleanup_memory():
    """Whatever a case leaves in memory goes when it ends: only the roots it made, each named by its record."""
    made = []
    yield made.append
    for root in made:
        if root.is_symlink():
            root.unlink()
        elif root.exists():
            for folder, _, _ in os.walk(root):
                try:
                    os.chmod(folder, 0o700)
                except OSError:
                    pass
            shutil.rmtree(root, ignore_errors=True)


# -- retire -------------------------------------------------------------------------------------------------------


def test_a_same_boot_replaced_memory_root_is_held_and_nothing_is_removed(tmp_path, cleanup_memory):
    case = abandoned(tmp_path)
    cleanup_memory(case.root)
    aside = case.root.with_name(case.root.name + '-aside')
    cleanup_memory(aside)
    case.root.rename(aside)
    case.root.mkdir(mode=0o700)
    before = first_result(case)
    held = retire(case)
    assert held.returncode == 2 and 'identity changed' in held.stderr, held.stdout + held.stderr
    assert (aside / 'runs' / case.run / 'state.json').exists() and first_result(case) == before
    case.root.rmdir()
    aside.rename(case.root)
    done = retire(case)
    assert done.returncode == 0, done.stdout + done.stderr
    assert not case.folder.exists()
    ending = json.loads((case.project / '.testruns' / 'results' / case.run / 'result.json').read_text())
    assert ending['outcome']['status'] == 'abandoned', 'the run is given its abandoned ending, then retired'


@pytest.mark.parametrize('where', ['memory root', 'run folder'])
def test_a_symlink_at_the_memory_root_or_its_run_is_held(tmp_path, cleanup_memory, where):
    case = abandoned(tmp_path)
    cleanup_memory(case.root)
    target = case.root if where == 'memory root' else case.folder
    real = target.with_name(target.name + '-real')
    cleanup_memory(real)
    target.rename(real)
    target.symlink_to(real, target_is_directory=True)
    held = retire(case)
    assert held.returncode == 2 and ('link' in held.stderr or 'identity' in held.stderr), held.stdout + held.stderr
    assert target.is_symlink() and real.exists()
    assert (real / ('runs/' + case.run if where == 'memory root' else '') / 'state.json').exists()


@pytest.mark.parametrize('field', ['identity', 'root', 'base', 'run'])
def test_a_tampered_storage_record_removes_nothing(tmp_path, cleanup_memory, field):
    case = abandoned(tmp_path)
    cleanup_memory(case.root)
    path = case.project / '.testruns' / 'results' / case.run / 'storage.json'
    elsewhere = Path(case.record['base']) / 'synthetic-elsewhere'  # another folder of the same memory filesystem
    elsewhere.mkdir(mode=0o700)
    tampered = dict(case.record)
    tampered[field] = {'identity': [1, 2, 3], 'root': 'qd-elsewhere', 'base': str(elsewhere),
                       'run': '20200101-000000-abcd'}[field]
    assert tampered[field] != case.record[field]
    path.write_text(json.dumps(tampered))
    held = retire(case)
    assert held.returncode == 2, held.stdout + held.stderr
    assert case.folder.exists() and (case.folder / 'state.json').exists(), 'nothing of the run is removed'
    assert list(elsewhere.iterdir()) == [], 'nothing is made where the tampered record points'


def test_absent_memory_with_a_live_group_is_held(tmp_path, cleanup_memory):
    case = abandoned(tmp_path, live=True)
    try:
        remove(case.root)  # a cleaner removed the memory under a still-running test
        held = retire(case)
        assert held.returncode == 2 and 'held' in held.stderr, held.stdout + held.stderr
        assert (case.project / '.testruns' / 'results' / case.run / 'ownership.json').exists()
        assert case.run in admin_entries(case.project) or admin_entries(case.project), 'its admin entry stays'
    finally:
        stop_groups(case.state)
        case.hold.unlink(missing_ok=True)


def test_absent_memory_after_a_reboot_retires_only_admin_and_records(tmp_path, cleanup_memory):
    case = abandoned(tmp_path)
    remove(case.root)  # a reboot clears tmpfs
    folder = case.project / '.testruns' / 'results' / case.run
    for name in ('ownership.json', 'storage.json'):
        data = json.loads((folder / name).read_text())
        data['boot'] = 'synthetic-earlier-boot'
        (folder / name).write_text(json.dumps(data))
    before = first_result(case)
    admin_before = admin_entries(case.project)
    done = retire(case)
    assert done.returncode == 0, done.stdout + done.stderr
    left = set(os.listdir(folder))
    assert 'result.json' in left and left <= {'result.json', 'report.md', 'failures'}, left
    ending = json.loads((folder / 'result.json').read_text())
    assert ending['run'] == case.run
    assert ending['outcome']['status'] == 'abandoned', \
        'a retired run gets its abandoned ending, as recovery gives an absent memory run, never a lasting "running"'
    assert len(admin_entries(case.project)) == len(admin_before) - 1, 'its own admin entry, and only that'
    assert not case.root.exists(), 'nothing is recreated in memory'
    assert before['result.json'] != b''


# -- clean ---------------------------------------------------------------------------------------------------------


def test_another_changes_absent_memory_run_is_left_alone(tmp_path, cleanup_memory):
    case = abandoned(tmp_path, change='feature')
    remove(case.root)
    from tests.integration.testrun_synthetic import add_worktree
    other = add_worktree(case.project, 'other', {})
    elsewhere = tool('run', '--target', str(other), '-n', '0', cwd=case.project)
    assert elsewhere.returncode == 0, elsewhere.stdout + elsewhere.stderr
    assert f'runs/{case.run}: left alone' in elsewhere.stdout and 'missing in the same or unknown boot' in elsewhere.stdout
    folder = case.project / '.testruns' / 'results' / case.run
    assert (folder / 'ownership.json').exists() and (folder / 'storage.json').exists()
    newest = result(case.project)
    cleanup_memory(Path(newest['working_storage']['path']).parent.parent)


def test_a_kept_memory_run_sends_the_next_run_to_disk(tmp_path, cleanup_memory):
    hold = tmp_path / 'hold'
    hold.write_text('hold')
    project, worktree = make(tmp_path, {'test_wait.py': HOLDING.format(hold=str(hold))})
    process = start('run', '--target', str(worktree), '-n', '0', cwd=project)
    marker = wait_for(lambda: started_marker(project))
    run = marker.parent.parent.name
    assert tool('keep', run, '--reason', 'synthetic', cwd=project).returncode == 0
    hold.unlink()
    out, err = process.communicate(timeout=120)
    assert process.returncode == 0, out + err
    record = json.loads((project / '.testruns' / 'results' / run / 'storage.json').read_text())
    cleanup_memory(Path(record['base']) / record['root'])
    (worktree / 'tests' / 'test_wait.py').write_text('def test_done():\n    pass\n')
    from tests.integration.testrun_synthetic import commit
    commit(worktree, 'finish')
    nxt = tool('run', '--target', str(worktree), '-n', '0', cwd=project)
    assert nxt.returncode == 0, nxt.stdout + nxt.stderr
    assert 'working files on disk' in nxt.stdout and 'memory reservations of runs' in nxt.stdout
    assert result(project)['working_storage']['kind'] == 'disk'
    assert run in scratch(project), 'the kept run is untouched'


# -- jail and bounds -----------------------------------------------------------------------------------------------


JAILED = '''
import pathlib
def test_nothing_else_of_the_memory_root_or_the_project_temp_is_reachable():
    for path in {forbidden!r}:
        assert not pathlib.Path(path).exists(), path
'''


def test_the_jail_binds_exactly_the_runs_memory_storage_and_no_sibling_or_project_temp(tmp_path, cleanup_memory):
    from tests.testrunner import jail
    if shutil.which('bwrap', path=jail.PATH) is None:
        pytest.skip('bubblewrap is not installed')
    import subprocess
    if subprocess.run(['bwrap', '--ro-bind', '/', '/', 'true'], capture_output=True).returncode != 0:
        pytest.skip('cannot make a namespace here (inside a jail)')
    project, worktree = make(tmp_path, {})
    name = storage.project_name(project.resolve())
    base, _ = storage.choose()
    sibling = base / name / 'runs' / '20200101-000000-sibl'
    cleanup_memory(base / name)
    for folder in (base / name, base / name / 'runs', sibling):
        folder.mkdir(mode=0o700, exist_ok=True)  # private, as the tool requires of its memory root
    (sibling / 'canary').write_text('sibling run')
    (project / '.testruns').mkdir(mode=0o700, exist_ok=True)  # private, as the tool requires
    plain = project / '.testruns' / 't'
    plain.mkdir(mode=0o700, exist_ok=True)
    (plain / 'canary').write_text('project temp')
    (worktree / 'tests' / 'test_jailed.py').write_text(JAILED.format(
        forbidden=[str(sibling / 'canary'), str(plain / 'canary')]))
    from tests.integration.testrun_synthetic import commit
    commit(worktree, 'a jailed probe')
    done = tool('run', '--jail', '--target', str(worktree), '-n', '0', cwd=project)
    assert results(project), done.stdout + done.stderr
    found = result(project)
    assert done.returncode == 0, done.stdout + done.stderr
    assert found['working_storage']['kind'] in ('tmpfs', 'ramfs')
    assert found['jail']['binds'][0] == {'kind': 'rw', 'path': found['working_storage']['path']}
    assert found['evidence']['counts']['passed'] == 2


def test_a_memory_run_records_its_peak_and_its_storage_is_gone_after(tmp_path, cleanup_memory):
    project, worktree = make(tmp_path, {'test_ok.py': 'def test_ok(tmp_path):\n    (tmp_path / "x").write_text("x" * 4096)\n'})
    done = tool('run', '--target', str(worktree), '-n', '0', cwd=project)
    assert done.returncode == 0, done.stdout + done.stderr
    found = result(project)
    working = found['working_storage']
    assert working['kind'] in ('tmpfs', 'ramfs') and type(working['peak']['bytes']) is int
    assert working['peak']['bytes'] >= 4096 and working['peak']['bytes'] <= working['bound_bytes']
    assert not Path(working['path']).exists() and scratch(project) == []
    assert sorted(os.listdir(results(project)[-1])) == ['report.md', 'result.json']
    cleanup_memory(Path(working['path']).parent.parent)
