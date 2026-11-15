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

from tests.integration.testrun_synthetic import (WAIT_TEST, add_worktree, admin_entries, make, result, results,
                                               scratch, skip_without_ci_memory, start, started_marker, tool, wait_for,
                                               working_paths)
from tests.testrunner import procs as storage_procs, scratch as storage

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


@pytest.fixture(autouse=True)
def memory_reservation():
    """The nested runs need the full reservation and margin, just like the CI-layout tests."""
    skip_without_ci_memory()


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
    elsewhere = tmp_path / 'synthetic-elsewhere'  # this case's own folder, never one shared across cases
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


def test_a_kept_memory_run_keeps_its_reservation_and_the_next_run_takes_memory_beside_it(tmp_path, cleanup_memory):
    hold = tmp_path / 'hold'
    hold.write_text('hold')
    project, worktree = make(tmp_path, {'test_wait.py': HOLDING.format(hold=str(hold))})
    own_roots(project, cleanup_memory)
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
    assert 'working files on disk' not in nxt.stdout
    assert result(project)['working_storage']['kind'] in ('tmpfs', 'ramfs')
    assert run in scratch(project), 'the kept run is untouched'
    assert (project / '.testruns' / 'results' / run / 'storage.json').exists(), 'and keeps its reservation'


# -- several runs in memory at once ---------------------------------------------------------------------------------


WRITES = 'def test_writes(tmp_path):\n    (tmp_path / "x").write_text("x" * 4096)\n'
NO_ROOM = 'no writable executable tmpfs/ramfs has room for a 4 GiB reservation plus the 8 GiB margin'


def own_roots(project, cleanup_memory):
    """This synthetic project's memory root under every candidate base: admission may make one in each it tries."""
    for base in storage.candidates():
        for named in {project, project.resolve()}:
            cleanup_memory(base / storage.project_name(named))


def markers(project):
    return [path / 'tmp' / 'started' for path in working_paths(project) if (path / 'tmp' / 'started').exists()]


def holding(project, worktree, hold, *, kill=True, live=False):
    """One run of ``worktree`` holding its test in memory; its runner is killed unless not ``kill``, and its
    test is stopped too unless ``live``."""
    hold.write_text('hold')
    known = set(markers(project))
    process = start('run', '--target', str(worktree), '-n', '0', cwd=project)
    marker = wait_for(lambda: next((path for path in markers(project) if path not in known), None))
    folder = marker.parent.parent
    run = folder.name
    record = json.loads((project / '.testruns' / 'results' / run / 'storage.json').read_text())
    assert record['kind'] in ('tmpfs', 'ramfs'), 'the run is in memory'
    root = Path(record['base']) / record['root']
    assert folder == root / 'runs' / run
    state = json.loads((folder / 'state.json').read_text())
    if kill:
        process.kill()
        process.wait()
        if not live:
            stop_groups(state)
            hold.unlink()
    return SimpleNamespace(project=project, run=run, folder=folder, root=root, record=record, state=state,
                           hold=hold, process=process, marker=marker,
                           storage=(project / '.testruns' / 'results' / run / 'storage.json').read_bytes())


def untouched(case):
    assert case.folder.is_dir() and case.marker.exists(), f'{case.run}: its memory scratch stands'
    assert (case.project / '.testruns' / 'results' / case.run / 'storage.json').read_bytes() == case.storage, \
        f'{case.run}: its reservation stands'


def alive(state):
    return any(storage_procs.group_members(pgid) for pgid, _ in state['groups'])


def memory_devices():
    """Each memory filesystem among the candidates a tool run of this test sees, by device: a base and its free bytes."""
    found = {}
    for base in storage.candidates():
        try:
            fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
        except OSError:
            continue
        try:
            if storage.filesystem(fd):
                space = os.fstatvfs(fd)
                found.setdefault(os.fstat(fd).st_dev, (base, space.f_bavail * space.f_frsize))
        finally:
            os.close(fd)
    return found


def promise(project, device, base, bound):
    """A standing reservation of the project on ``device``: admission charges its recorded bound in full."""
    from tests.testrunner.layout import RESULTS, Root, reserve_run_id, write_json
    root = Root(project)
    try:
        run = reserve_run_id(root)
        fd = root.child(RESULTS, run, create=False)
        try:
            write_json(fd, storage.RECORD, {'run': run, 'base': str(base), 'root': 'qd-standing-promise',
                                            'identity': [device, 1, os.getuid()], 'kind': 'tmpfs',
                                            'boot': storage_procs.boot_id(), 'bound_bytes': bound})
        finally:
            os.close(fd)
    finally:
        root.close()
    return project / '.testruns' / 'results' / run / storage.RECORD


def test_standing_promises_keep_both_devices_when_a_candidate_id_collides(tmp_path, monkeypatch):
    from tests.testrunner import layout
    project, _ = make(tmp_path, {})
    candidates = iter(('abcd', 'abcd', 'bcde'))
    real_token_hex = layout.secrets.token_hex
    monkeypatch.setattr(layout.secrets, 'token_hex',
                        lambda count: next(candidates) if count == 2 else real_token_hex(count))
    monkeypatch.setattr(layout, 'time', SimpleNamespace(strftime=lambda fmt: '20200101-000000'))
    first = promise(project, 11, tmp_path, storage.BOUND)
    original = first.read_bytes()
    second = promise(project, 22, tmp_path, storage.BOUND * 2)
    assert first != second and first.read_bytes() == original
    assert json.loads(second.read_text())['identity'][0] == 22
    root = layout.Root(project)
    try:
        assert storage.reserved(root, 11) == storage.BOUND
        assert storage.reserved(root, 22) == storage.BOUND * 2
        assert len(storage.pending(root)) == 2
    finally:
        root.close()


def no_room(project):
    """Every memory device the tool sees is promised beyond its free space."""
    return [promise(project, device, base, free + storage.BOUND) for device, (base, free) in memory_devices().items()]


def run_beside_promises(project, worktree):
    """One tool run of ``worktree`` and its own result. The stand-in promises have results folders of their own,
    named like runs and possibly in the same second, so the newest folder by name need not be this run's."""
    before = set(results(project))
    done = tool('run', '--target', str(worktree), '-n', '0', cwd=project)
    made = [path for path in results(project) if path not in before]
    assert len(made) == 1, (made, done.stdout + done.stderr)
    return done, result(project, made[0])


def test_two_runs_hold_memory_at_once_and_each_retires_only_its_own(tmp_path, cleanup_memory):
    first_hold, second_hold = tmp_path / 'hold-first', tmp_path / 'hold-second'
    project, worktree = make(tmp_path, {'test_wait.py': HOLDING.format(hold=str(first_hold))})
    own_roots(project, cleanup_memory)
    first = holding(project, worktree, first_hold, live=True)  # its runner died; its test still runs in memory
    cleanup_memory(first.root)
    other = add_worktree(project, 'other', {'test_wait.py': HOLDING.format(hold=str(second_hold))})
    second = holding(project, other, second_hold, kill=False)  # a live run beside it
    assert second.folder != first.folder and second.root == first.root, 'each its own run folder in one root'
    assert alive(first.state) and alive(second.state), 'both tests run in memory at once'
    second_hold.unlink()
    out, err = second.process.communicate(timeout=120)
    assert second.process.returncode == 0, out + err
    assert 'working files on disk' not in out, 'the second run took memory beside the first'
    assert not second.folder.exists(), 'the second run retired its own scratch'
    assert not (project / '.testruns' / 'results' / second.run / 'storage.json').exists(), 'and its reservation'
    untouched(first)
    assert alive(first.state), "the first run's test was never touched"
    stop_groups(first.state)
    first_hold.unlink()
    retired = tool('retire', first.run, cwd=project)
    assert retired.returncode == 0, retired.stdout + retired.stderr
    assert not first.folder.exists() and not (project / '.testruns' / 'results' / first.run / 'storage.json').exists()
    assert result(project, project / '.testruns' / 'results' / second.run)['outcome']['status'] == 'passed'


def test_a_dead_runs_reservation_is_retired_alone_while_its_peers_stays(tmp_path, cleanup_memory):
    first_hold, second_hold = tmp_path / 'hold-first', tmp_path / 'hold-second'
    project, worktree = make(tmp_path, {'test_wait.py': HOLDING.format(hold=str(first_hold))})
    own_roots(project, cleanup_memory)
    first = holding(project, worktree, first_hold)
    cleanup_memory(first.root)
    other = add_worktree(project, 'other', {'test_wait.py': HOLDING.format(hold=str(second_hold))})
    second = holding(project, other, second_hold)
    untouched(first)
    retired = tool('retire', first.run, cwd=project)
    assert retired.returncode == 0, retired.stdout + retired.stderr
    assert not first.folder.exists() and not (project / '.testruns' / 'results' / first.run / 'storage.json').exists()
    untouched(second)
    assert tool('retire', second.run, cwd=project).returncode == 0
    assert not second.folder.exists()


def test_a_third_run_falls_back_to_disk_with_its_one_line_when_the_room_is_gone(tmp_path, cleanup_memory):
    first_hold, second_hold = tmp_path / 'hold-first', tmp_path / 'hold-second'
    project, worktree = make(tmp_path, {'test_wait.py': HOLDING.format(hold=str(first_hold))})
    own_roots(project, cleanup_memory)
    first = holding(project, worktree, first_hold)
    cleanup_memory(first.root)
    second = holding(project, add_worktree(project, 'other', {'test_wait.py': HOLDING.format(hold=str(second_hold))}),
                     second_hold)
    standing = no_room(project)
    third = add_worktree(project, 'third', {'test_writes.py': WRITES})
    done, ran = run_beside_promises(project, third)
    assert done.returncode == 0, done.stdout + done.stderr
    lines = [line for line in done.stdout.splitlines() if 'working files' in line]
    assert len(lines) == 1 and 'working files on disk' in lines[0] and NO_ROOM in lines[0], done.stdout
    assert ran['working_storage']['kind'] == 'disk'
    untouched(first)
    untouched(second)
    for path in standing:
        path.unlink()  # the stand-in promises are this test's own records
    for case in (first, second):
        assert tool('retire', case.run, cwd=case.project).returncode == 0
        assert not case.folder.exists()


def test_a_disk_fallback_leaves_no_memory_root_under_any_candidate_base(tmp_path, cleanup_memory):
    """A real tool run on real tmpfs that finds no room makes no qd-<uid>-* root of its project in any base it
    weighed. Other seats' runs make roots of their own in the same bases meanwhile, so the gate looks for this
    synthetic project's own root by name, as admission would name it."""
    project, worktree = make(tmp_path, {'test_writes.py': WRITES})
    own_roots(project, cleanup_memory)  # a regression's empty roots are still removed
    names = {storage.project_name(named) for named in (project, project.resolve())}
    bases = storage.candidates()
    devices = memory_devices()
    if not devices:
        pytest.skip('no memory filesystem among the candidates: nothing to fall back from')

    def roots():
        return sorted(str(base / name) for base in bases for name in names if os.path.lexists(base / name))
    assert roots() == [], 'a fresh synthetic project has no memory root yet'
    standing = no_room(project)
    done, ran = run_beside_promises(project, worktree)
    assert done.returncode == 0, done.stdout + done.stderr
    lines = [line for line in done.stdout.splitlines() if 'working files' in line]
    assert len(lines) == 1 and 'working files on disk' in lines[0] and NO_ROOM in lines[0], done.stdout
    assert ran['working_storage']['kind'] == 'disk'
    assert roots() == [], 'the fallback left an empty memory root in a base it passed over'
    for path in standing:
        path.unlink()


RACE = """
import json, os, sys, time
from pathlib import Path
from tests.testrunner import scratch
from tests.testrunner.layout import Root
project, run, go = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
root = Root(project)
while not go.exists():
    time.sleep(0.001)
print(json.dumps(scratch.prepare(root, run)), flush=True)
"""


def test_admission_under_the_lock_never_promises_the_last_room_twice(tmp_path, cleanup_memory):
    import subprocess
    import sys
    from tests.conftest import ROOT
    from tests.testrunner.layout import Root, reserve_run_id
    project, _ = make(tmp_path, {'test_writes.py': WRITES})
    own_roots(project, cleanup_memory)
    devices = memory_devices()
    slack = 2 * 1024 ** 3  # room for this one admission and half a bound more: never for two
    rounds = 3
    device, (base, free) = max(devices.items(), key=lambda item: item[1][1])
    if free < storage.BOUND + storage.MARGIN + slack + rounds * storage.BOUND + storage.BOUND:
        pytest.skip('the largest memory filesystem has too little room to stage the race')
    for other, (other_base, other_free) in devices.items():
        if other != device:
            promise(project, other, other_base, other_free + storage.BOUND)
    winners = []
    for number in range(rounds):
        free = memory_devices()[device][1]
        last = promise(project, device, base, free - storage.BOUND - storage.MARGIN - slack - len(winners) * storage.BOUND)
        go = tmp_path / f'go-{number}'
        root = Root(project)
        try:
            runs = [reserve_run_id(root), reserve_run_id(root)]
        finally:
            root.close()
        racers = [subprocess.Popen([sys.executable, '-I', '-c', f'import sys; sys.path.insert(0, {str(ROOT)!r}); exec({RACE!r})',
                                    str(project), run, str(go)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                  for run in runs]
        go.write_text('go')
        outcomes = []
        for racer in racers:
            out, err = racer.communicate(timeout=60)
            assert racer.returncode == 0, out + err
            outcomes.append(json.loads(out.strip().splitlines()[-1]))
        kinds = sorted(outcome['kind'] for outcome in outcomes)
        assert kinds == ['disk', 'tmpfs'], f'round {number}: the last room went to exactly one: {outcomes}'
        won = next(outcome for outcome in outcomes if outcome['kind'] == 'tmpfs')
        assert NO_ROOM in next(outcome for outcome in outcomes if outcome['kind'] == 'disk')['reason']
        cleanup_memory(Path(won['base']) / won['root'])
        winners.append(won['run'])
        last.unlink()
    assert sorted(winners) == sorted(set(winners))
    for run in winners:
        assert (project / '.testruns' / 'results' / run / 'storage.json').exists(), 'each winner holds its promise'


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
