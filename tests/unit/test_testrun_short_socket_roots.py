"""Ordinary positive layout, identity and retirement for the fixed-length root."""
import json
import os
from pathlib import Path
import subprocess

from tests.testrunner import sessions, socket_roots
from tests.testrunner.layout import RESULTS, RUN_SOCKETS, Root, identity

RUN = '20000101-000000-0000'


def test_long_projects_have_distinct_fixed_length_socket_roots():
    first = Path('/' + 'first-project-' * 100)
    second = Path('/' + 'second-project-' * 100)
    assert socket_roots.path(first) != socket_roots.path(second)
    assert len(os.fsencode(socket_roots.path(first))) == len(os.fsencode(socket_roots.path(second)))
    assert socket_roots.path(first).parent == Path('/tmp')


def test_the_same_project_reopens_the_same_private_root(tmp_path, monkeypatch):
    monkeypatch.setattr(socket_roots, 'SYSTEM_TMP', tmp_path)
    project = tmp_path / 'project'
    anchor = socket_roots.ensure(project, RUN_SOCKETS)
    before = identity(anchor.stat())
    assert socket_roots.ensure(project, RUN_SOCKETS) == anchor
    assert identity(anchor.stat()) == before
    assert anchor.stat().st_mode & 0o777 == 0o700
    assert anchor.parent.stat().st_mode & 0o777 == 0o700
    marker = anchor.parent / socket_roots.PROJECT
    assert marker.stat().st_mode & 0o777 == 0o600
    assert json.loads(marker.read_text())['project'] == str(project)


def test_short_session_is_registered_and_retired_with_project_records(tmp_path, monkeypatch):
    monkeypatch.setattr(socket_roots, 'SYSTEM_TMP', tmp_path)
    project = tmp_path / 'project'
    project.mkdir()
    subprocess.run(['git', '-c', 'init.templateDir=', 'init', '--quiet', str(project)], check=True)
    root = Root(project)
    try:
        os.close(root.child(RESULTS, RUN, 'sessions'))
        registry = root.path / RESULTS / RUN / 'sessions'
        monkeypatch.setenv(sessions.RECORD_ENV, str(registry))
        monkeypatch.setenv('TESTRUNS_RUN', RUN)
        parent = socket_roots.ensure(project, RUN_SOCKETS)
        assert sessions.automatic(parent)
        folder = parent / 'u-sample'
        folder.mkdir()
        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            token = sessions.register(parent, folder, fd)
            assert token
        finally:
            os.close(fd)
        record = json.loads((registry / 'u-sample.json').read_text())
        assert record['socket_layout'] == socket_roots.LAYOUT
        assert record['anchor_identity'] == identity(parent.stat())
        assert sessions.anchor_path(root, record) == parent
        assert sessions.retire(root, RUN)['outcome'] == 'complete'
        assert not folder.exists()
        sessions.discard_records(root, RUN, sessions.owned.Budget())
        assert not registry.exists()
        assert not any(parent.iterdir())
    finally:
        root.close()
