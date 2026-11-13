"""One recorded crashed run, its temporary recovery records, and independent session containers."""
from __future__ import annotations

import fcntl
import json
import os
import signal

import pytest

from tests.conftest import socket_container
from tests.integration.testrun_synthetic import (WAIT_TEST, commit, make, result, results, start,
                                               started_marker, tool, wait_for)
from tests.testrunner import clean, owned, sessions
from tests.testrunner.layout import (KEEPS, RESULTS, RUNS, SOCKETS, Root, identity, new_run_id, write_json)


def crashed(tmp_path):
    project, worktree = make(tmp_path, {})
    root = Root(project)
    run = new_run_id()
    scratch = root.child(RUNS, run)
    state = {'run': run, 'change': 'feature', 'target': str(worktree), 'head': 'synthetic-head',
             'scratch': identity(os.fstat(scratch)), 'owner': owned.mark(scratch),
             'boot': 'synthetic-earlier-boot', 'groups': []}
    write_json(scratch, 'state.json', state)
    os.close(scratch)
    clean.record(root, run, state)
    fd = root.child(RESULTS, run)
    write_json(fd, 'result.json', {'run': run, 'outcome': {'status': 'failed', 'pytest_exit': 1,
                                                         'wrapper_exit': 1}, 'cleanup': {'outcome': 'pending'}})
    os.close(fd)
    os.close(root.child(RESULTS, run, 'sessions'))
    os.close(root.child(RESULTS, run, 'failures'))
    folder = root.path / RESULTS / run
    (folder / 'report.md').write_text('original test report\n')
    (folder / 'failures' / '001.txt').write_text('original test failure\n')
    root.close()
    original = {name: (folder / name).read_bytes() for name in ('result.json', 'report.md', 'failures/001.txt')}
    return project, run, state, original


def container(project, run, name='owned-container'):
    root = Root(project)
    try:
        fd = root.child(SOCKETS, name)
        record = {'name': name, 'identity': identity(os.fstat(fd)), 'owner': owned.mark(fd)}
        os.close(fd)
        records = root.child(RESULTS, run, 'sessions')
        write_json(records, name + '.json', record)
        os.close(records)
        return root.path / SOCKETS / name
    finally:
        root.close()


def assert_original(project, run, original):
    folder = project / '.testruns' / RESULTS / run
    assert {name: (folder / name).read_bytes() for name in original} == original


def test_fixture_records_its_container_before_use_then_retirement_forgets_it(tmp_path, monkeypatch):
    project, run, state, original = crashed(tmp_path)
    parent = project / '.testruns' / SOCKETS
    parent.mkdir(mode=0o700)
    registry = project / '.testruns' / RESULTS / run / 'sessions'
    with monkeypatch.context() as patch:
        patch.setenv(sessions.RECORD_ENV, str(registry))
        patch.setenv('TESTRUNS_RUN', run)
        with socket_container(parent, 'registered-', record=True) as folder:
            record = json.loads((registry / (folder.name + '.json')).read_text())
            assert record['name'] == folder.name
            assert record['identity'] == identity(folder.stat())
            assert record['owner'] == (folder / owned.MARKER).read_text()
    assert not folder.exists() and registry.exists()
    done = tool('retire', run, cwd=project)
    assert done.returncode == 0, done.stdout + done.stderr
    assert not registry.exists()
    assert_original(project, run, original)


def test_recording_failure_closes_the_unused_container(tmp_path, monkeypatch):
    project, run, state, original = crashed(tmp_path)
    parent = project / '.testruns' / SOCKETS
    parent.mkdir(mode=0o700)
    registry = project / '.testruns' / RESULTS / run / 'sessions'

    def failed(*args, **kwargs):
        raise OSError('synthetic recording failure')

    with monkeypatch.context() as patch:
        patch.setenv(sessions.RECORD_ENV, str(registry))
        patch.setenv('TESTRUNS_RUN', run)
        patch.setattr(sessions, 'write_json', failed)
        with pytest.raises(OSError, match='synthetic recording failure'):
            with socket_container(parent, 'registered-', record=True):
                pytest.fail('an unrecorded container was used')
    assert list(parent.iterdir()) == []
    assert_original(project, run, original)


def test_retire_preserves_first_evidence_and_peers_then_repeats(tmp_path):
    project, run, state, original = crashed(tmp_path)
    own = container(project, run)
    (own / f'tmux-{os.getuid()}').mkdir()
    peer = own.with_name('peer-container')
    peer.mkdir()
    (peer / 'peer-output').write_text('independent session')
    other = project / '.testruns' / RUNS / 'unrelated-run'
    other.mkdir()
    (other / 'output').write_text('independent run')
    done = tool('retire', run, cwd=project)
    assert done.returncode == 0, done.stdout + done.stderr
    assert not own.exists() and not (project / '.testruns' / RUNS / run).exists()
    assert (peer / 'peer-output').read_text() == 'independent session'
    assert (other / 'output').read_text() == 'independent run'
    assert_original(project, run, original)
    folder = project / '.testruns' / RESULTS / run
    assert sorted(item.name for item in folder.iterdir()) == ['failures', 'report.md', 'result.json']
    retirement = project / '.testruns' / 'retirements' / (run + '.json')
    before = retirement.read_bytes()
    assert json.loads(before)['cleanup']['outcome'] == 'complete'
    again = tool('retire', run, cwd=project)
    assert again.returncode == 0 and 'already retired' in again.stdout
    assert retirement.read_bytes() == before
    assert_original(project, run, original)


def test_held_session_keeps_recovery_records_until_retry(tmp_path):
    project, run, state, original = crashed(tmp_path)
    own = container(project, run)
    output = own / 'remaining-output'
    output.write_text('unfinished namespace')
    first = tool('retire', run, cwd=project)
    assert first.returncode == 2, first.stdout + first.stderr
    folder = project / '.testruns' / RESULTS / run
    assert (folder / 'ownership.json').exists() and (folder / 'sessions').exists()
    assert output.read_text() == 'unfinished namespace'
    assert_original(project, run, original)
    output.unlink()  # this test's own ordinary output, after its writer has ended
    again = tool('retire', run, cwd=project)
    assert again.returncode == 0, again.stdout + again.stderr
    assert not own.exists()
    assert sorted(item.name for item in folder.iterdir()) == ['failures', 'report.md', 'result.json']
    assert_original(project, run, original)


def test_retire_uses_external_record_after_partial_scratch_removal(tmp_path):
    project, run, state, original = crashed(tmp_path)
    scratch = project / '.testruns' / RUNS / run
    (scratch / 'state.json').unlink()
    done = tool('retire', run, cwd=project)
    assert done.returncode == 0, done.stdout + done.stderr
    assert not scratch.exists()
    assert_original(project, run, original)


def test_live_run_lock_and_keep_note_refuse_without_changing_evidence(tmp_path):
    project, run, state, original = crashed(tmp_path)
    scratch = project / '.testruns' / RUNS / run
    lock = os.open(scratch / 'lock', os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        live = tool('retire', run, cwd=project)
        assert live.returncode == 2 and 'lock is held' in live.stderr
        assert scratch.exists()
        assert_original(project, run, original)
    finally:
        os.close(lock)
    root = Root(project)
    fd = root.child(KEEPS)
    write_json(fd, run + '.json', {'run': run, 'reason': 'synthetic debugging'})
    os.close(fd)
    root.close()
    kept = tool('retire', run, cwd=project)
    assert kept.returncode == 2 and 'kept by note' in kept.stderr
    assert scratch.exists()
    assert_original(project, run, original)


def test_missing_ownership_and_replaced_sessions_are_held(tmp_path):
    project, run, state, original = crashed(tmp_path)
    scratch = project / '.testruns' / RUNS / run
    marker = scratch / owned.MARKER
    token = marker.read_text()
    marker.unlink()
    missing = tool('retire', run, cwd=project)
    assert missing.returncode == 2 and scratch.exists()
    assert_original(project, run, original)
    marker.write_text(token)
    own = container(project, run)
    moved = own.with_name('moved-original')
    own.rename(moved)
    own.mkdir()  # distinct inode: the original still exists, so it cannot be reused here
    replaced = tool('retire', run, cwd=project)
    assert replaced.returncode == 2 and own.exists() and moved.exists()
    assert_original(project, run, original)
    own.rmdir()
    moved.rename(own)
    done = tool('retire', run, cwd=project)
    assert done.returncode == 0, done.stdout + done.stderr
    assert_original(project, run, original)


def test_new_direct_session_does_not_hold_two_successive_tool_runs(tmp_path):
    project, worktree = make(tmp_path, {'test_wait.py': WAIT_TEST})
    process = start('run', '--target', str(worktree), '-n', '0', cwd=project)
    try:
        marker = wait_for(lambda: started_marker(project))
        root = Root(project)
        os.close(root.child(SOCKETS))
        parent = root.path / SOCKETS
        root.close()
        # This author pytest's direct container starts after the tool's pytest did, on the same anchor.
        with socket_container(parent, 'peer-') as peer:
            output = peer / 'direct-output'
            output.write_text('direct pytest still running')
            marker.with_name('release').write_text('finish')
            out, err = process.communicate(timeout=120)
            assert process.returncode == 0, out + err
            assert result(project)['cleanup']['outcome'] == 'complete'
            assert output.read_text() == 'direct pytest still running'
            (worktree / 'tests' / 'test_wait.py').write_text('def test_done():\n    pass\n')
            commit(worktree, 'finish the synthetic wait')
            second = tool('run', '--target', str(worktree), '-n', '0', cwd=project)
            assert second.returncode == 0, second.stdout + second.stderr
            assert result(project)['cleanup']['outcome'] == 'complete'
            assert output.read_text() == 'direct pytest still running'
            for folder in results(project):
                assert sorted(item.name for item in folder.iterdir()) == ['report.md', 'result.json']
            output.unlink()
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            process.communicate(timeout=90)


def test_running_crashed_run_gets_an_abandoned_ending_before_retirement(tmp_path):
    project, run, state, original = crashed(tmp_path)
    root = Root(project)
    fd = root.child(RESULTS, run)
    write_json(fd, 'result.json', {'run': run, 'outcome': {'status': 'running'}})
    os.close(fd)
    root.close()
    folder = project / '.testruns' / RESULTS / run
    (folder / 'failures' / '001.txt').unlink()
    (folder / 'failures').rmdir()
    (project / '.testruns' / RUNS / run / 'console.txt').write_text('synthetic interrupted console\n')
    done = tool('retire', run, cwd=project)
    assert done.returncode == 0, done.stdout + done.stderr
    ended = json.loads((folder / 'result.json').read_text())
    assert ended['outcome']['status'] == 'abandoned'
    assert set(ended['evidence']['counts'].values()) == {None}
    assert 'synthetic interrupted console' in (folder / 'report.md').read_text()
    assert sorted(item.name for item in folder.iterdir()) == ['report.md', 'result.json']


def test_retire_never_follows_a_replaced_scratch_link(tmp_path):
    project, run, state, original = crashed(tmp_path)
    scratch = project / '.testruns' / RUNS / run
    moved = scratch.with_name('moved-scratch')
    scratch.rename(moved)
    peer = tmp_path / 'independent-data'
    peer.mkdir()
    (peer / 'output').write_text('independent output')
    scratch.symlink_to(peer, target_is_directory=True)
    done = tool('retire', run, cwd=project)
    assert done.returncode == 2
    assert (peer / 'output').read_text() == 'independent output'
    assert scratch.is_symlink() and moved.exists()
    assert_original(project, run, original)
