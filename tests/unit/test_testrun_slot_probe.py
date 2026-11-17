"""The test tool's slot probe and a refused setup: throwaway slot and results folders under tmp_path only.

(4) Asking whether a run still holds its slot never takes a free slot: a run starting at that very moment gets it.
(3) A run refused or timed out before its work begins leaves no empty results/<run> folder behind; a folder with
anything in it is never removed. (2) A storage record that cannot be read names itself and how to clear it.
"""
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from tests.testrunner import cli, layout, run, scratch
from tests.testrunner.layout import RESULTS, ROOT_NAME, Refused
from .test_run_slots_ordinary import root_at
from .test_testrun_socket_paths import declare_suite

RUN = '20000101-000000-00a1'


def three_held(root):
    """Three slots held by live holders, each its own open file description."""
    held = []
    for index in range(3):
        pool = layout.Slot(root)
        pool.acquire({'verb': 'run', 'run': f'20000101-000000-01{index:02x}'})
        held.append(pool)
    return held


def crashed_note(folder, run_id):
    """The fourth slot's file as a crashed run leaves it: its note names the run, and no process holds its lock."""
    (folder / 'slot-4.lock').write_text(json.dumps({'verb': 'run', 'run': run_id}))


class _Os:
    """layout's own os, with close observed: the moment the probe has finished with a slot file."""

    def __init__(self, on_close):
        self.on_close = on_close

    def __getattr__(self, name):
        return getattr(os, name)

    def close(self, fd):
        self.on_close(fd)
        os.close(fd)


def test_a_probe_never_takes_a_free_slot_from_a_run_starting_at_that_moment(tmp_path, monkeypatch):
    root = root_at(tmp_path)
    held = three_held(root)
    crashed_note(tmp_path, RUN)
    fourth = os.stat(tmp_path / 'slot-4.lock').st_ino
    outcome = {}

    def starting(fd):
        # When the probe lets go of the fourth slot's file, a wait-0 run tries to start: it must find that slot free.
        if outcome or os.fstat(fd).st_ino != fourth:
            return
        outcome['tried'] = True
        competitor = layout.Slot(root)
        try:
            competitor.acquire({'verb': 'run', 'run': 'competitor'}, 0)
            outcome['slot'] = competitor.selected + 1
        except Refused as refused:
            outcome['refused'] = str(refused)
        finally:
            competitor.release()

    monkeypatch.setattr(layout, 'os', _Os(starting))
    try:
        assert layout.run_holds_slot(root, RUN) is False, 'a crashed run holds nothing'
        assert outcome.get('tried'), 'the probe read the fourth slot'
        assert outcome == {'tried': True, 'slot': 4}, outcome
    finally:
        monkeypatch.undo()
        for pool in held:
            pool.release()
        root.close()


@pytest.mark.parametrize('locks', ['proc', 'unreadable'])
def test_a_live_holder_is_seen_and_a_crashed_one_is_not(tmp_path, monkeypatch, locks):
    if locks == 'unreadable':
        def unreadable():
            raise OSError('no /proc/locks here')
        monkeypatch.setattr(layout, '_flock_inodes', unreadable)
    root = root_at(tmp_path)
    live = layout.Slot(root)
    try:
        live.acquire({'verb': 'run', 'run': RUN})
        assert layout.run_holds_slot(root, RUN) is True
        assert layout.run_holds_slot(root, '20000101-000000-00b2') is False, 'another run is not this one'
        live.held = []  # leave the note, as a crash does; the lock goes with the file description
        live.release()
        assert layout.run_holds_slot(root, RUN) is False
    finally:
        live.release()
        root.close()


@pytest.fixture
def setup_run(tmp_path, monkeypatch):
    """The run's setup on a throwaway project: a real root, slots, run id and result guard; only the target, the
    socket preflight and, per test, registration or storage are invented."""
    declare_suite(tmp_path)
    (tmp_path / 'tests/unit').mkdir(exist_ok=True)
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True, capture_output=True)
    (tmp_path / '.gitignore').write_text('.testruns/\n')  # the tool's own folder is ignored, as in any project
    target = SimpleNamespace(top=tmp_path, project=tmp_path, branch='change', main_checkout=False, head='0' * 40,
                             tree='0' * 40)
    monkeypatch.setattr(run.gitops, 'resolve', lambda path: target)
    monkeypatch.setattr(run.gitops, 'dirty', lambda path: [])
    monkeypatch.setattr(run, 'check_target', lambda top, item: item)
    monkeypatch.setattr(run.socket_paths, 'preflight', lambda *args, **kwargs: None)

    def results():
        folder = tmp_path / ROOT_NAME / RESULTS
        return sorted(path.name for path in folder.iterdir()) if folder.exists() else []

    return tmp_path, results


def test_a_refused_registration_leaves_no_empty_results_folder(setup_run, monkeypatch):
    tmp_path, results = setup_run
    def refuse(root, target, run_id):
        raise Refused('synthetic registration refusal')
    monkeypatch.setattr(run.clean, 'register', refuse)
    with pytest.raises(Refused, match='synthetic registration refusal'):
        run.Runner(cli.parser().parse_args(['run']), []).main()
    assert results() == []


def test_a_setup_timeout_leaves_no_empty_results_folder(setup_run, monkeypatch):
    tmp_path, results = setup_run
    def stop(root, target, run_id):
        raise run.Stop('synthetic setup time limit')
    monkeypatch.setattr(run.clean, 'register', stop)
    assert run.Runner(cli.parser().parse_args(['run']), []).main() == run.EXIT['timeout']
    assert results() == []


def test_a_refused_working_storage_leaves_no_empty_results_folder(setup_run, monkeypatch):
    tmp_path, results = setup_run
    monkeypatch.setattr(run.clean, 'register', lambda *args: None)
    def refuse(*args, **kwargs):
        raise Refused('synthetic storage refusal')
    monkeypatch.setattr(run.storage, 'prepare', refuse)
    with pytest.raises(Refused, match='synthetic storage refusal'):
        run.Runner(cli.parser().parse_args(['run']), []).main()
    assert results() == []


def test_a_results_folder_with_anything_in_it_is_never_removed(setup_run, monkeypatch):
    tmp_path, results = setup_run
    def refuse(root, target, run_id):
        (tmp_path / ROOT_NAME / RESULTS / run_id / 'evidence.txt').write_text('kept\n')
        raise Refused('synthetic refusal after a write')
    monkeypatch.setattr(run.clean, 'register', refuse)
    with pytest.raises(Refused):
        run.Runner(cli.parser().parse_args(['run']), []).main()
    kept, = results()
    assert (tmp_path / ROOT_NAME / RESULTS / kept / 'evidence.txt').read_text() == 'kept\n'


def test_an_unreadable_storage_record_names_itself_and_how_to_clear_it(tmp_path):
    root = root_at(tmp_path)
    try:
        folder = tmp_path / RESULTS / RUN
        folder.parent.mkdir(mode=0o700)
        folder.mkdir(mode=0o700)
        (folder / scratch.RECORD).write_text('{ not json')
        with pytest.raises(Refused) as refused:
            scratch.reserved(root, os.stat(tmp_path).st_dev)
        words = str(refused.value)
        assert str(folder / scratch.RECORD) in words
        assert 'every new run is refused' in words and 'move that one file' in words, words
    finally:
        root.close()
