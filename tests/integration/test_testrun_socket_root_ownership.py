"""The short socket root under /tmp is this user's, private and this project's, or the session does not start.

/tmp is shared by every user of the machine, so the fixed root `qd-<uid>-<hash>` may already stand there, planted.
A root that is a link, that is not mode 0700, that another user owns, that names another project or none, or whose
anchor is a link, is refused before anything is made or written in it; what stands there is left as it was.

Each case runs the suite's own fallback (conftest.socket_parent) on a synthetic project, with the root's parent a
private stand-in for /tmp, except where another user's folder is needed: there the machine's /usr stands in for a
root someone else owns, which nothing here can write.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests import conftest
from tests.testrunner import socket_roots
from tests.testrunner.layout import FORMAT, SOCKETS

REFUSED = 'cannot verify the project-owned socket anchor'


@pytest.fixture
def project(tmp_path, monkeypatch):
    folder = tmp_path / 'synthetic-project'
    folder.mkdir()
    conftest.git(folder, 'init', '--quiet')
    system_tmp = tmp_path / 'synthetic-tmp'
    system_tmp.mkdir()
    monkeypatch.setattr(socket_roots, 'SYSTEM_TMP', system_tmp.resolve())
    monkeypatch.delenv('TESTRUNS_JAILED', raising=False)  # the project rule, which holds inside a jail as well
    monkeypatch.delenv('TMUX_TMPDIR', raising=False)
    monkeypatch.setattr(conftest, 'ROOT', folder)
    return folder


def contents(folder: Path) -> dict[str, tuple[int, int]]:
    """Every name below a folder, with its mode and size: what must stay exactly as it was."""
    found = {}
    for path in sorted(folder.rglob('*')):
        info = path.lstat()
        found[str(path.relative_to(folder))] = (info.st_mode, info.st_size)
    return found


def refused(match: str = '') -> None:
    with pytest.raises(pytest.UsageError, match=REFUSED) as error:
        conftest.socket_parent()
    assert match in str(error.value), error.value


def test_a_root_that_is_a_link_is_refused_and_its_target_left_untouched(project, tmp_path):
    target = tmp_path / 'synthetic-elsewhere'
    target.mkdir(mode=0o700)
    root = socket_roots.path(project)
    root.symlink_to(target, target_is_directory=True)
    refused('link')
    assert root.is_symlink() and os.readlink(root) == str(target)
    assert list(target.iterdir()) == [], 'nothing is made through the link'


@pytest.mark.parametrize('mode', [0o755, 0o750, 0o711, 0o500])
def test_a_root_that_is_not_mode_0700_is_refused_and_left_as_it_was(project, mode):
    root = socket_roots.path(project)
    root.mkdir()
    root.chmod(mode)
    try:
        refused('0700' if mode & 0o022 == 0 else '')
        assert (root.stat().st_mode & 0o777) == mode, 'its mode is not changed'
        root.chmod(0o700)
        assert list(root.iterdir()) == [], 'nothing is written in a refused root'
    finally:
        root.chmod(0o700)


def test_a_root_writable_by_others_is_refused(project):
    root = socket_roots.path(project)
    root.mkdir()
    root.chmod(0o777)
    try:
        refused('writable by others')
        assert list(root.iterdir()) == []
    finally:
        root.chmod(0o700)


@pytest.mark.skipif(os.getuid() == 0, reason='root owns every stand-in for another user\'s folder')
def test_a_root_another_user_owns_is_refused(project, monkeypatch):
    foreign = Path('/usr')  # always there, never this user's, never writable here
    assert foreign.stat().st_uid != os.getuid()
    monkeypatch.setattr(socket_roots, 'SYSTEM_TMP', foreign.parent)
    monkeypatch.setattr(socket_roots, 'path', lambda project: foreign)
    before = sorted(os.listdir(foreign))
    refused('belongs to uid')
    assert sorted(os.listdir(foreign)) == before


def test_a_root_that_names_another_project_is_refused_and_left_alone(project, tmp_path):
    root = socket_roots.path(project)
    root.mkdir(mode=0o700)
    marker = root / socket_roots.PROJECT
    marker.write_text(json.dumps({'format': FORMAT, 'project': str(tmp_path / 'synthetic-other-project'),
                                  'layout': socket_roots.LAYOUT}))  # a well-formed identity, of another project
    marker.chmod(0o600)
    before = contents(root)
    refused('does not belong to this project')
    assert contents(root) == before


def test_a_root_holding_files_but_no_project_identity_is_refused_and_left_alone(project):
    root = socket_roots.path(project)
    root.mkdir(mode=0o700)
    (root / 'planted').write_text('not ours to adopt')
    before = contents(root)
    refused('no project identity')
    assert contents(root) == before


def test_a_project_identity_others_can_read_is_refused(project):
    conftest.socket_parent()  # made once, properly
    marker = socket_roots.path(project) / socket_roots.PROJECT
    marker.chmod(0o644)
    refused('0600')


def test_an_anchor_that_is_a_link_is_refused_and_its_target_left_untouched(project, tmp_path):
    conftest.socket_parent()  # the root, made properly, with its anchor
    root = socket_roots.path(project)
    (root / SOCKETS).rmdir()
    target = tmp_path / 'synthetic-anchor-elsewhere'
    target.mkdir(mode=0o700)
    (root / SOCKETS).symlink_to(target, target_is_directory=True)
    refused('link')
    assert (root / SOCKETS).is_symlink() and list(target.iterdir()) == []


def test_a_proper_root_is_made_private_and_reopened(project):
    anchor = conftest.socket_parent()
    root = socket_roots.path(project)
    assert anchor == root / SOCKETS
    assert (root.stat().st_mode & 0o777) == 0o700 and (anchor.stat().st_mode & 0o777) == 0o700
    assert root.stat().st_uid == os.getuid()
    assert conftest.socket_parent() == anchor, 'the same project reopens its own root'
