"""Ordinary launch arguments and saved session directory compatibility; no native program or jail."""

from dataclasses import replace
import sqlite3

import pytest

from quarterdeck import endpoint
from quarterdeck.harness.opencode import OpenCodeHarness
from quarterdeck.ship import Pool, SeatSpec, ShipError


@pytest.fixture
def launch_spec(tmp_path):
    home = tmp_path / 'ship/runtime/seats/builder/home'
    home.mkdir(parents=True)
    workspace = tmp_path / 'work/hands/builder'
    workspace.mkdir(parents=True)
    pool = Pool('synthetic', 'local', endpoint='http://example.invalid/v1')
    seat = SeatSpec('builder', 'crew', 'hand', 'opencode', ('synthetic',),
                    model='synthetic-model', cwd=workspace)
    endpoint.Choice('synthetic', 'synthetic-model', 32768, 8192).write(home)
    return OpenCodeHarness(), seat, pool, home, workspace


def saved_session(home, directory):
    record = home / 'data-001/opencode/opencode.db'
    record.parent.mkdir(parents=True)
    with sqlite3.connect(record) as writer:
        writer.executescript('''
            CREATE TABLE session (id TEXT, time_created INTEGER, time_updated INTEGER,
                                  parent_id TEXT, directory TEXT);
            CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER,
                                  time_updated INTEGER, data TEXT);
            CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT,
                               time_created INTEGER, time_updated INTEGER, data TEXT);
        ''')
        writer.execute('INSERT INTO session VALUES (?, 1, 2, NULL, ?)',
                       ('synthetic-session', str(directory)))
    return record


@pytest.mark.parametrize('sandbox', [None, True])
def test_declared_hand_workspace_is_the_native_project(launch_spec, sandbox):
    harness, seat, pool, home, workspace = launch_spec
    launched = harness.launch(replace(seat, sandbox=sandbox), pool, home, home / 'brief', None, None)
    assert launched.argv[:2] == ('opencode', str(workspace))
    assert launched.env['QD_WORKSPACE'] == str(workspace)
    assert not (home.parent / 'work').exists()


def test_undeclared_hand_keeps_the_original_work_folder(launch_spec):
    harness, seat, pool, home, workspace = launch_spec
    launched = harness.launch(replace(seat, cwd=None), pool, home, home / 'brief', None, None)
    assert launched.argv[:2] == ('opencode', str(home.parent / 'work'))
    assert (home.parent / 'work').is_dir()
    assert 'QD_WORKSPACE' not in launched.env


@pytest.mark.parametrize('declared', [False, True])
def test_resume_uses_the_same_working_folder_as_launch(launch_spec, declared):
    harness, seat, pool, home, workspace = launch_spec
    seat = seat if declared else replace(seat, cwd=None)
    folder = workspace if declared else home.parent / 'work'
    record = saved_session(home, folder)
    harness.preflight_resume(seat, home, lambda: 'synthetic-session')
    launched = harness.launch(seat, pool, home, home / 'brief', 'synthetic-session', None)
    assert launched.argv[:2] == ('opencode', str(folder))
    assert launched.argv[-2:] == ('--session', 'synthetic-session')
    assert launched.env['XDG_DATA_HOME'] == str(record.parents[1])


def test_old_session_directory_is_explained_and_retained(launch_spec):
    harness, seat, pool, home, workspace = launch_spec
    previous = home.parent / 'work'
    record = saved_session(home, previous)
    before = record.read_bytes()
    for resume in (lambda: harness.preflight_resume(seat, home, lambda: 'synthetic-session'),
                   lambda: harness.launch(seat, pool, home, home / 'brief', 'synthetic-session', None)):
        with pytest.raises(ShipError) as caught:
            resume()
        reason = str(caught.value)
        assert f'recorded {str(previous)!r}' in reason
        assert f'expected {str(workspace)!r}' in reason
        assert 'qd fresh builder' in reason and 'retains the old record' in reason
        assert record.read_bytes() == before


@pytest.mark.parametrize('change', [dict(role='worker'), dict(human_session=True), dict(sandbox=False)])
def test_other_launches_keep_the_existing_native_project_default(launch_spec, change):
    harness, seat, pool, home, workspace = launch_spec
    launched = harness.launch(replace(seat, **change), pool, home, home / 'brief', None, None)
    assert launched.argv[:2] == ('opencode', '--auto')
