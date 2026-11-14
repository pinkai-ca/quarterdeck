"""Ownership with peers running: four slots let real runs of one project overlap, so the one slot no longer keeps a
live run from a recovering or retiring peer. Each run's own result-directory guard must: a peer of the same change
leaves a live run alone, retire refuses a live run, each run retires only its own, and the all-slots hold of a
non-sharing proof part excludes every other run while it holds, waits for a live run, and holds nothing partial.

Real synthetic projects and the real tool; a run is abandoned by killing its runner, as a crash leaves it.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
from types import SimpleNamespace

import pytest

from tests.integration.testrun_synthetic import (add_worktree, make, result, results, scratch, start, tool, wait_for,
                                               working_paths)
from tests.testrunner import layout

HOLDING = '''
import os, pathlib, time
def test_waits():
    (pathlib.Path(os.environ["TMPDIR"]) / "started").write_text(str(os.getpid()))
    while pathlib.Path({hold!r}).exists():
        time.sleep(0.1)
'''


def markers(project):
    found = [path / 'tmp' / 'started' for path in working_paths(project) if (path / 'tmp' / 'started').exists()]
    folder = project / '.testruns' / 'runs'
    found += list(folder.glob('*/tmp/started')) if folder.is_dir() else []
    return set(found)


def holding(project, worktree, hold):
    """A live run of ``worktree`` whose test waits on ``hold``: its runner and its slot are held."""
    hold.write_text('hold')
    known = markers(project)
    process = start('run', '--target', str(worktree), '-n', '0', cwd=project)
    marker = wait_for(lambda: next(iter(markers(project) - known), None))
    folder = marker.parent.parent
    return SimpleNamespace(run=folder.name, folder=folder, marker=marker, process=process, hold=hold,
                           state=json.loads((folder / 'state.json').read_text()))


def holding_at_once(project, worktrees):
    """Live runs started together, one per ``(change, worktree, hold)``, each told by the change its state names."""
    known = markers(project)
    started = []
    for change, worktree, hold in worktrees:
        hold.write_text('hold')
        started.append((change, hold, start('run', '--target', str(worktree), '-n', '0', cwd=project)))

    def all_started():
        new = markers(project) - known
        return new if len(new) == len(worktrees) else None

    found = wait_for(all_started)
    by_change = {}
    for marker in found:
        state = json.loads((marker.parent.parent / 'state.json').read_text())
        by_change[state['change']] = (marker, state)
    cases = []
    for change, hold, process in started:
        marker, state = by_change[change]
        cases.append(SimpleNamespace(run=marker.parent.parent.name, folder=marker.parent.parent, marker=marker,
                                     process=process, hold=hold, state=state))
    return cases


def finish(case):
    case.hold.unlink()
    out, err = case.process.communicate(timeout=180)
    assert case.process.returncode == 0, out + err
    return out


def abandon(case):
    case.process.kill()
    case.process.wait()
    for pgid, _ in case.state['groups']:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    case.hold.unlink()


@pytest.fixture
def peers(tmp_path):
    project, feature = make(tmp_path, {'test_wait.py': HOLDING.format(hold=str(tmp_path / 'hold-feature'))})
    other = add_worktree(project, 'other', {'test_wait.py': HOLDING.format(hold=str(tmp_path / 'hold-other'))})
    quick = add_worktree(project, 'quick', {'test_quick.py': 'def test_quick():\n    pass\n'})
    live = []
    yield SimpleNamespace(project=project, feature=feature, other=other, quick=quick, tmp=tmp_path, live=live)
    for case in live:
        if case.process.poll() is None:
            case.process.kill()
            case.process.wait()
        for pgid, _ in case.state['groups']:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_two_runs_at_once_each_recovering_and_retiring_only_its_own(peers):
    p = peers
    # Both started together, so their setups overlap as well as their tests.
    first, second = holding_at_once(p.project, [('feature', p.feature, p.tmp / 'hold-feature'),
                                                ('other', p.other, p.tmp / 'hold-other')])
    p.live.extend([first, second])
    assert first.run != second.run and first.state['change'] == 'feature' and second.state['change'] == 'other'
    assert first.process.poll() is None and second.process.poll() is None, 'two live runs at once'
    # A run of the same change as a live run recovers nothing of it: its guard is held.
    # (only the base test: the change's own test would wait on the same hold)
    done = tool('run', '--target', str(p.feature), '-n', '0', 'tests/test_base.py', cwd=p.project)
    assert done.returncode == 0 and f'run {first.run}: left alone' in done.stdout, done.stdout + done.stderr
    assert first.marker.exists() and first.process.poll() is None, 'the live run is untouched'
    # Retire refuses a live run.
    refused = tool('retire', second.run, cwd=p.project)
    assert refused.returncode != 0 and second.marker.exists() and second.process.poll() is None
    # Each run retires only its own.
    finish(second)
    assert not second.folder.exists(), 'the second retired its own scratch'
    assert first.marker.exists() and first.process.poll() is None, 'and nothing of the first'
    # The first abandoned: a later run of its change recovers exactly it, beside nothing else.
    abandon(first)
    later = tool('run', '--target', str(p.feature), '-n', '0', 'tests/test_base.py', cwd=p.project)
    assert later.returncode == 0 and f'run {first.run}: abandoned' in later.stdout, later.stdout + later.stderr
    assert not first.folder.exists()
    assert result(p.project, p.project / '.testruns' / 'results' / second.run)['outcome']['status'] == 'passed'


def test_all_slots_excludes_every_other_run_while_it_holds(peers):
    p = peers
    root = layout.Root(p.project)
    try:
        with layout.all_slots(root):
            for slot in ('slot.lock', 'slot-2.lock', 'slot-3.lock', 'slot-4.lock'):
                assert json.loads((p.project / '.testruns' / slot).read_text())['all_slots'] is True
            done = tool('run', '--target', str(p.quick), '-n', '0', cwd=p.project)
            assert done.returncode == 2 and 'all 4 test slots are busy' in done.stderr
            done = tool('retire', '20261111-120000-abcd', cwd=p.project)
            assert done.returncode != 0, 'retire takes a slot too, and waits'
            assert results(p.project) == []
        done = tool('run', '--target', str(p.quick), '-n', '0', cwd=p.project)
        assert done.returncode == 0, done.stdout + done.stderr
    finally:
        root.close()


def test_all_slots_waits_for_a_live_run_and_holds_nothing_partial_meanwhile(peers):
    p = peers
    first = holding(p.project, p.feature, p.tmp / 'hold-feature')
    p.live.append(first)
    root = layout.Root(p.project)
    try:
        with pytest.raises(layout.Refused, match='all four test slots are needed'):
            with layout.all_slots(root, wait=1):
                pytest.fail('all slots taken while a run lives')
        done = tool('run', '--target', str(p.quick), '-n', '0', cwd=p.project)
        assert done.returncode == 0, 'the refused wait held no partial slot'
        finish(first)
        with layout.all_slots(root, wait=5):
            done = tool('run', '--target', str(p.quick), '-n', '0', cwd=p.project)
            assert done.returncode == 2
    finally:
        root.close()
    assert scratch(p.project) == []
