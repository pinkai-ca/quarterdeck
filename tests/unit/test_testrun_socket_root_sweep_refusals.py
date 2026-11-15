"""The orphan socket-root sweep refuses every root it cannot prove empty and its own.

Each case builds a real root on a stand-in /tmp, plants one fault, removes the
project, sweeps, and requires the root kept with its own reason and left exactly
as it was. A sound root beside the fault is still removed, so a refusal is not
just a sweep that removes nothing.
"""
import json
import os
from pathlib import Path
import stat
import threading

import pytest

from tests.testrunner import socket_roots
from tests.testrunner.layout import RUN_SOCKETS, SOCKETS


@pytest.fixture
def tmp(tmp_path, monkeypatch):
    base = tmp_path.resolve()
    monkeypatch.setattr(socket_roots, 'SYSTEM_TMP', base)
    return base


def make(base: Path, name: str) -> tuple[Path, Path]:
    project = base / f'project-{name}'
    project.mkdir()
    socket_roots.ensure(project, RUN_SOCKETS)
    socket_roots.ensure(project, SOCKETS)
    return project, socket_roots.path(project)


def snapshot(top: Path) -> list:
    """Every entry below top, without following a link: name, type and mode, inode, file bytes."""
    seen = []
    info = os.lstat(top)
    seen.append(('.', info.st_mode, info.st_ino, os.readlink(top) if stat.S_ISLNK(info.st_mode) else None))
    if not stat.S_ISDIR(info.st_mode):
        return seen
    for folder, dirs, files in os.walk(top, followlinks=False):
        for name in sorted(dirs + files):
            entry = Path(folder) / name
            info = os.lstat(entry)
            if stat.S_ISREG(info.st_mode):
                body = entry.read_bytes()
            elif stat.S_ISLNK(info.st_mode):
                body = os.readlink(entry)
            else:
                body = None
            seen.append((str(entry.relative_to(top)), info.st_mode, info.st_ino, body))
    return seen


def rewrite_identity(where: Path, change) -> None:
    record = json.loads((where / socket_roots.PROJECT).read_text())
    change(record)
    os.unlink(where / socket_roots.PROJECT)
    fd = os.open(where / socket_roots.PROJECT, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, json.dumps(record).encode())
    finally:
        os.close(fd)


def anchor_not_empty(base, where):
    (where / SOCKETS / 'container').mkdir(mode=0o700)


def anchor_holds_file(base, where):
    (where / RUN_SOCKETS / 'leftover.sock').write_text('')


def extra_entry(base, where):
    (where / 'extra').write_text('')


def root_mode_0755(base, where):
    os.chmod(where, 0o755)


def anchor_mode_0755(base, where):
    os.chmod(where / SOCKETS, 0o755)


def identity_mode_0644(base, where):
    os.chmod(where / socket_roots.PROJECT, 0o644)


def identity_is_link(base, where):
    real = base / 'real-identity.json'
    os.replace(where / socket_roots.PROJECT, real)
    os.symlink(real, where / socket_roots.PROJECT)


def identity_is_fifo(base, where):
    os.unlink(where / socket_roots.PROJECT)
    os.mkfifo(where / socket_roots.PROJECT, 0o600)


def identity_names_other_project(base, where):
    rewrite_identity(where, lambda record: record.update(project=str(base / 'elsewhere')))


def anchor_is_link(base, where):
    target = base / 'anchor-target'
    target.mkdir(mode=0o700)
    os.rmdir(where / SOCKETS)
    os.symlink(target, where / SOCKETS)


REFUSALS = {
    'anchor-not-empty': (anchor_not_empty, 'anchor is not private and empty'),
    'anchor-holds-a-file': (anchor_holds_file, 'anchor is not private and empty'),
    'extra-entry': (extra_entry, 'root has other entries'),
    'root-mode-0755': (root_mode_0755, 'root is not private mode 0700'),
    'anchor-mode-0755': (anchor_mode_0755, 'anchor is not private and empty'),
    'identity-mode-0644': (identity_mode_0644, 'not a bounded private project identity'),
    'identity-is-a-link': (identity_is_link, 'Too many levels of symbolic links'),
    'identity-names-another-project': (identity_names_other_project, 'identity does not derive this root'),
    'anchor-is-a-link': (anchor_is_link, 'is a link or not a directory'),
}


def sweep_beside_a_sound_root(base: Path, where: Path) -> dict:
    """Sweep the faulted root together with a sound orphan, which must still go."""
    project, sound = make(base, 'sound')
    project.rmdir()
    found = socket_roots.sweep([where, sound])
    assert found['removed'] == [str(sound)] and not sound.exists()
    return found


@pytest.mark.parametrize('case', sorted(REFUSALS))
def test_a_faulted_root_is_kept_as_it_was(tmp, case):
    plant, reason = REFUSALS[case]
    project, where = make(tmp, case)
    plant(tmp, where)
    project.rmdir()
    before = snapshot(where)
    found = sweep_beside_a_sound_root(tmp, where)
    assert [entry['path'] for entry in found['kept']] == [str(where)]
    assert reason in found['kept'][0]['reason']
    assert found['absent'] == []
    assert snapshot(where) == before


def test_a_root_whose_project_is_present_is_kept(tmp):
    project, where = make(tmp, 'present')
    before = snapshot(where)
    found = sweep_beside_a_sound_root(tmp, where)
    assert found['kept'] == [{'path': str(where), 'reason': 'project-present'}]
    assert snapshot(where) == before and project.is_dir()


def test_a_root_that_is_a_link_is_kept_and_its_target_untouched(tmp):
    project, where = make(tmp, 'root-link')
    project.rmdir()
    target = tmp / 'root-target'
    os.replace(where, target)  # a private folder holding a valid identity of a gone project
    os.symlink(target, where)
    before_link, before_target = snapshot(where), snapshot(target)
    found = sweep_beside_a_sound_root(tmp, where)
    assert [entry['path'] for entry in found['kept']] == [str(where)]
    assert 'is a link or not a directory' in found['kept'][0]['reason']
    assert snapshot(where) == before_link and snapshot(target) == before_target


def test_another_users_root_is_kept(tmp, monkeypatch):
    project, where = make(tmp, 'other-user')
    project.rmdir()
    real_uid = os.getuid()
    other = where.with_name(f'qd-0-{where.name.rsplit("-", 1)[1]}')
    os.rename(where, other)
    before = snapshot(other)
    monkeypatch.setattr(os, 'getuid', lambda: 0)  # the sweep now runs as uid 0: this folder is another's
    found = socket_roots.sweep([other])
    monkeypatch.setattr(os, 'getuid', lambda: real_uid)
    assert found['removed'] == [] and [entry['path'] for entry in found['kept']] == [str(other)]
    assert f'belongs to uid {real_uid}, not this user' in found['kept'][0]['reason']
    assert snapshot(other) == before


@pytest.mark.parametrize('spelling', ['outside-the-parent', 'dot-dot'])
def test_a_path_not_directly_under_the_parent_is_refused(tmp, spelling):
    project, where = make(tmp, spelling)
    project.rmdir()
    if spelling == 'outside-the-parent':
        named = tmp / 'sub' / where.name
    else:
        named = tmp / '..' / tmp.name / where.name
    before = snapshot(where)
    found = socket_roots.sweep([named])
    assert found['removed'] == [] and found['absent'] == []
    assert [entry['path'] for entry in found['kept']] == [str(named)]
    assert 'not a derived socket root for this user' in found['kept'][0]['reason']
    assert snapshot(where) == before  # the like-named root under the parent is not reached either


def test_an_identity_that_is_a_fifo_is_kept_without_waiting_on_it(tmp):
    project, where = make(tmp, 'identity-fifo')
    identity_is_fifo(tmp, where)
    project.rmdir()
    before = snapshot(where)
    found = {}
    sweeping = threading.Thread(target=lambda: found.update(sweep_beside_a_sound_root(tmp, where)), daemon=True)
    sweeping.start()
    sweeping.join(10)
    if sweeping.is_alive():
        # Release the blocked open by giving the FIFO a writer, then fail: the sweep waited on it.
        os.close(os.open(where / socket_roots.PROJECT, os.O_WRONLY | os.O_NONBLOCK))
        sweeping.join(10)
        pytest.fail('the sweep blocked opening an identity that is a FIFO')
    assert [entry['path'] for entry in found['kept']] == [str(where)]
    assert 'not a bounded private project identity' in found['kept'][0]['reason']
    assert snapshot(where) == before
