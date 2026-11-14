"""Ordinary cleanup of inert project trees; no test-tool run or process is started."""

import os
import shutil

import pytest

from tests.conftest import SyntheticMemoryRoots
from tests.testrunner import layout, scratch


def test_all_temporary_projects_release_their_memory_roots(tmp_path, monkeypatch):
    projects = [tmp_path / 'p', tmp_path / 'second-project']
    base = tmp_path / 'memory-base'
    base.mkdir()
    roots = []
    for project in projects:
        (project / '.testruns').mkdir(parents=True)
        root = base / scratch.project_name(project)
        root.mkdir(mode=0o700)
        (root / 'runs').mkdir()
        (root / 'runs' / 'inert-kept-product.txt').write_text('synthetic product')
        roots.append(root)

    # Every filesystem reads as disk, as on a runner without a large tmpfs: a base inside the test's own
    # tmp_path is not asked its mount, and the real opening and removal take its roots.
    monkeypatch.setattr(scratch, 'filesystem', lambda fd: None)
    checks = set()
    cleanup = SyntheticMemoryRoots(tmp_path, checks)
    cleanup.finish()
    assert all(not root.exists() for root in roots)
    assert all(str(root) in checks for root in roots)
    assert all(project.is_dir() for project in projects)


@pytest.mark.parametrize('kind', [None, 'tmpfs'])
def test_a_shared_base_must_still_be_memory(tmp_path, tmp_path_factory, monkeypatch, kind):
    """A base outside the test's own tmp_path is held unless it is tmpfs or ramfs; never /dev/shm or /tmp here."""
    project = tmp_path / 'p'
    (project / '.testruns').mkdir(parents=True)
    shared = tmp_path_factory.mktemp('shared-base')
    assert not shared.is_relative_to(tmp_path)
    root = shared / scratch.project_name(project)
    root.mkdir(mode=0o700)
    (root / 'runs').mkdir()
    (root / 'runs' / 'inert-kept-product.txt').write_text('synthetic product')
    own = layout.identity(os.stat(shared))
    real = scratch.filesystem
    # Stand-in: the shared base reads as `kind`, every other base as it really is.
    monkeypatch.setattr(scratch, 'filesystem', lambda fd: kind if layout.identity(os.fstat(fd)) == own else real(fd))
    monkeypatch.setattr(scratch, 'candidates', lambda environ=None: [tmp_path, shared])
    checks = set()
    cleanup = SyntheticMemoryRoots(tmp_path, checks)
    try:
        if kind is None:
            with pytest.raises(pytest.fail.Exception) as failed:
                cleanup.finish()
            assert f'{shared}: recorded working storage is no longer tmpfs or ramfs' in str(failed.value)
            assert (root / 'runs' / 'inert-kept-product.txt').read_text() == 'synthetic product'
        else:
            cleanup.finish()
            assert not os.path.lexists(root)
        assert str(root) in checks and project.is_dir()
    finally:
        shutil.rmtree(shared)


@pytest.mark.parametrize('when', ['before the root is opened', 'after the root is opened'])
def test_a_root_replaced_on_a_disk_base_is_not_removed(tmp_path, monkeypatch, when):
    """On a base that reads as disk the root is opened without the mount, and the identities still hold:
    a root swapped for another directory of the same owner and mode is not removed, nor is the original."""
    project = tmp_path / 'p'
    (project / '.testruns').mkdir(parents=True)
    base = tmp_path / 'memory'
    base.mkdir()
    original = base / scratch.project_name(project)
    original.mkdir(mode=0o700)
    (original / 'runs').mkdir()
    (original / 'runs' / 'inert-kept-product.txt').write_text('synthetic product')
    aside = tmp_path / 'original-moved-aside'
    real = layout.open_child

    def swap():
        original.rename(aside)
        original.mkdir(mode=0o700)
        (original / 'replacement.txt').write_text('not the test root')

    def open_child(parent_fd, name, where, **options):
        if where == original and when == 'before the root is opened':
            swap()
        opened = real(parent_fd, name, where, **options)
        if where == original and when == 'after the root is opened':
            swap()
        return opened

    monkeypatch.setattr(scratch, 'filesystem', lambda fd: None)  # every base reads as disk
    monkeypatch.setattr(scratch, 'memory_root', None)  # a disk base inside tmp_path never asks it
    monkeypatch.setattr(layout, 'open_child', open_child)
    with pytest.raises(pytest.fail.Exception) as failed:
        SyntheticMemoryRoots(tmp_path, set()).finish()
    message = str(failed.value)
    assert str(original) in message and ('changed before teardown' in message or 'replaced' in message)
    assert (original / 'replacement.txt').read_text() == 'not the test root'
    assert (aside / 'runs' / 'inert-kept-product.txt').read_text() == 'synthetic product'
    shutil.rmtree(original)


def test_children_inherit_the_test_temporary_directory(tmp_path):
    import tempfile

    assert os.environ['TMPDIR'] == str(tmp_path)
    assert tempfile.gettempdir() == str(tmp_path)
