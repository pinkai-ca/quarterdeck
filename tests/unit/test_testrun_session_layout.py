"""Ordinary fixture layout and cleanup capacity for a run and child pytest."""
import json
import os
from pathlib import Path
import subprocess

import pytest

from tests.testrunner import owned, sessions
from tests.testrunner.layout import RESULTS, RUN_SOCKETS, SOCKETS, Refused, Root, git_environment, identity, write_json


RUN = '20000101-000000-0000'  # invented schema-valid run, never a captured runtime identity


@pytest.fixture
def root(tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    subprocess.run(['git', '-C', str(project), 'init', '--quiet'], env=git_environment(), check=True)
    result = Root(project)
    try:
        yield result
    finally:
        result.close()


def register(root, anchor, monkeypatch):
    os.close(root.child(RESULTS, RUN, 'sessions'))
    os.close(root.child(anchor))
    parent = root.path / anchor
    folder = parent / 'u-sample'
    folder.mkdir()
    descriptor = os.open(folder, owned.DIRECTORY)
    monkeypatch.setenv('TESTRUNS_RUN', RUN)
    monkeypatch.setenv(sessions.RECORD_ENV, str(root.path / RESULTS / RUN / 'sessions'))
    try:
        if anchor == SOCKETS:
            # Construct an old record rather than asking a new runner to use
            # direct pytest's anchor for its own fixture.
            token = owned.mark(descriptor)
            records = root.child(RESULTS, RUN, 'sessions', create=False)
            try:
                write_json(records, 'u-sample.json', {'name': folder.name,
                    'identity': identity(os.fstat(descriptor)), 'owner': token})
            finally:
                os.close(records)
        else:
            assert sessions.register(parent, folder, descriptor)
    finally:
        os.close(descriptor)
    return folder


def test_run_container_is_separate_from_the_direct_pytest_anchor(root, monkeypatch):
    os.close(root.child(SOCKETS))
    folder = register(root, RUN_SOCKETS, monkeypatch)
    record = json.loads((root.path / RESULTS / RUN / 'sessions/u-sample.json').read_text())

    assert record['anchor'] == RUN_SOCKETS
    assert not any((root.path / SOCKETS).iterdir())
    assert sessions.retire(root, RUN)['outcome'] == 'complete'
    assert not folder.exists()
    sessions.discard_records(root, RUN, owned.Budget())
    assert not (root.path / RESULTS / RUN / 'sessions').exists()


def test_legacy_session_record_keeps_its_original_anchor(root, monkeypatch):
    folder = register(root, SOCKETS, monkeypatch)

    assert sessions.retire(root, RUN)['outcome'] == 'complete'
    assert not folder.exists()
    sessions.discard_records(root, RUN, owned.Budget())


def test_nested_child_pytest_uses_its_ordinary_fixture_cleanup(root, monkeypatch):
    outer = register(root, RUN_SOCKETS, monkeypatch)
    child = outer / 'u-child'
    child.mkdir()
    descriptor = os.open(child, owned.DIRECTORY)
    try:
        assert sessions.register(outer, child, descriptor) is None
        assert not any(child.iterdir())
    finally:
        os.close(descriptor)
        child.rmdir()

    assert sessions.retire(root, RUN)['outcome'] == 'complete'


def test_child_pytest_using_the_direct_anchor_keeps_local_cleanup(root, monkeypatch):
    register(root, RUN_SOCKETS, monkeypatch)
    os.close(root.child(SOCKETS))
    parent = root.path / SOCKETS
    child = parent / 'u-child'
    child.mkdir()
    descriptor = os.open(child, owned.DIRECTORY)
    try:
        assert sessions.register(parent, child, descriptor) is None
        assert not any(child.iterdir())
    finally:
        os.close(descriptor)
        child.rmdir()
    assert not any(parent.iterdir())
    assert sessions.retire(root, RUN)['outcome'] == 'complete'


def test_default_budget_can_validate_and_remove_a_large_run(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(owned.time, 'monotonic', lambda: clock[0])
    budget = owned.Budget()
    clock[0] = 6.0
    where = Path('sample-run')
    for _ in range(500_000):
        budget.check(where)

    clock[0] = 61.0
    with pytest.raises(Refused, match='retirement budget exhausted'):
        budget.check(where)
