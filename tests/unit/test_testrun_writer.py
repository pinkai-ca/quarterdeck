"""Ordinary filesystem writer races and their bounded cleanup reporting."""
from __future__ import annotations

import errno
import os
from pathlib import Path

from tests.testrunner import owned
from tests.testrunner.layout import identity


def directory(tmp_path):
    folder = tmp_path / 'scratch'
    folder.mkdir()
    parent = os.open(tmp_path, owned.DIRECTORY)
    descriptor = os.open(folder, owned.DIRECTORY)
    try:
        token = owned.mark(descriptor)
        expected = identity(os.fstat(descriptor))
    finally:
        os.close(descriptor)
    return parent, folder, expected, token


def test_file_written_between_walk_and_rmdir_is_retired(tmp_path, monkeypatch):
    parent, folder, expected, token = directory(tmp_path)
    real_rmdir = os.rmdir
    raced = False

    def rmdir(name, *, dir_fd=None):
        nonlocal raced
        if name == folder.name and not raced:
            raced = True
            (folder / 'late-file').write_text('writer output')
        return real_rmdir(name, dir_fd=dir_fd)

    monkeypatch.setattr(os, 'rmdir', rmdir)
    try:
        result = owned.retire(parent, folder.name, expected, folder, token)
        assert raced
        assert result['outcome'] == 'complete'
        assert result['removed']['files'] == 2
        assert not folder.exists()
    finally:
        os.close(parent)


def test_persistent_writer_is_bounded_and_retry_works(tmp_path, monkeypatch):
    parent, folder, expected, token = directory(tmp_path)
    real_rmdir = os.rmdir

    def rmdir(name, *, dir_fd=None):
        if name == folder.name:
            (folder / 'late-file').write_text('writer output')
        return real_rmdir(name, dir_fd=dir_fd)

    monkeypatch.setattr(os, 'rmdir', rmdir)
    try:
        result = owned.retire(parent, folder.name, expected, folder, token,
                              budget=owned.Budget(seconds=0.04))
        assert result['outcome'] == 'held'
        assert result['scratch'] == str(folder)
        assert 'budget' in result['reason']
        assert (folder / owned.MARKER).read_text() == token
        monkeypatch.setattr(os, 'rmdir', real_rmdir)
        assert owned.retire(parent, folder.name, expected, folder, token)['outcome'] == 'complete'
        assert owned.retire(parent, folder.name, expected, folder, token)['outcome'] == 'complete'
    finally:
        os.close(parent)


def test_nested_directory_writer_race_is_retried(tmp_path, monkeypatch):
    parent, folder, expected, token = directory(tmp_path)
    child = folder / 'output'
    child.mkdir()
    real_rmdir = os.rmdir
    raced = False

    def rmdir(name, *, dir_fd=None):
        nonlocal raced
        if name == child.name and not raced:
            raced = True
            (child / 'late-file').write_bytes(b'late')
        return real_rmdir(name, dir_fd=dir_fd)

    monkeypatch.setattr(os, 'rmdir', rmdir)
    try:
        result = owned.retire(parent, folder.name, expected, folder, token)
        assert result['outcome'] == 'complete'
        assert raced and not folder.exists()
    finally:
        os.close(parent)


def test_ordinary_io_failure_is_reported(tmp_path, monkeypatch):
    parent, folder, expected, token = directory(tmp_path)

    def unreadable(*args, **kwargs):
        raise OSError(errno.EIO, 'synthetic filesystem failure')

    monkeypatch.setattr(os, 'scandir', unreadable)
    try:
        result = owned.retire(parent, folder.name, expected, folder, token)
        assert result['outcome'] == 'held'
        assert 'synthetic filesystem failure' in result['reason']
        assert folder.exists()
    finally:
        os.close(parent)


def test_traversal_budget_is_shared_with_validation(tmp_path):
    parent, folder, expected, token = directory(tmp_path)
    (folder / 'output').write_text('one')
    try:
        result = owned.retire(parent, folder.name, expected, folder, token,
                              budget=owned.Budget(entries=0))
        assert result['outcome'] == 'held'
        assert (folder / 'output').read_text() == 'one'
    finally:
        os.close(parent)
