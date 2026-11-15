"""The scratch measure walks by descriptors: deep trees are measured, counts match the path walk, links are not followed."""
from __future__ import annotations

import os
import stat

import pytest

from tests.testrunner import scratch
from tests.testrunner.layout import Refused

DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def path_walk(path):
    """The measure as it was before the descriptor walk, by path, kept here as the reference for its counts."""
    total = {'bytes': 0, 'file_bytes': 0, 'allocated_bytes': 0, 'metadata_allowance_bytes': 0, 'files': 0, 'dirs': 0}
    seen = set()

    def walk(folder):
        with os.scandir(folder) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                total['metadata_allowance_bytes'] += scratch.METADATA_ALLOWANCE
                if stat.S_ISDIR(info.st_mode):
                    total['dirs'] += 1
                    walk(entry.path)
                elif stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) not in seen:
                    seen.add((info.st_dev, info.st_ino))
                    total['files'] += 1
                    total['file_bytes'] += max(info.st_size, info.st_blocks * 512)
                    total['allocated_bytes'] += info.st_blocks * 512
    walk(path)
    total['bytes'] = total['file_bytes'] + total['metadata_allowance_bytes']
    return total


def deep(root, levels, name, contents=b''):
    """A chain of ``levels`` folders named ``name`` under root, made by descriptors, with one file at the bottom."""
    held = [os.open(root, DIRECTORY)]
    try:
        for _ in range(levels):
            os.mkdir(name, 0o700, dir_fd=held[-1])
            held.append(os.open(name, DIRECTORY, dir_fd=held[-1]))
        fd = os.open('bottom', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600, dir_fd=held[-1])
        try:
            os.write(fd, contents)
        finally:
            os.close(fd)
    finally:
        for fd in held:
            os.close(fd)


def remove_deep(root, levels, name):
    """Remove what ``deep`` made, by descriptors, so no path past the limit is ever named."""
    held = [os.open(root, DIRECTORY)]
    try:
        for _ in range(levels):
            held.append(os.open(name, DIRECTORY, dir_fd=held[-1]))
        os.unlink('bottom', dir_fd=held[-1])
        for level in range(levels, 0, -1):
            os.close(held.pop())
            os.rmdir(name, dir_fd=held[level - 1])
    finally:
        for fd in held:
            os.close(fd)


def test_a_tree_past_the_path_limit_is_measured_not_refused(tmp_path):
    levels, name = 30, 'd' * 200  # about 6 KiB of path, past the 4 KiB limit
    deep(tmp_path, levels, name, b'x' * 4096)
    try:
        with pytest.raises(OSError):  # the path walk cannot even name the bottom of it
            path_walk(tmp_path)
        measured = scratch.usage(tmp_path)
    finally:
        remove_deep(tmp_path, levels, name)
    assert measured['dirs'] == levels
    assert measured['files'] == 1
    assert measured['file_bytes'] >= 4096
    assert measured['metadata_allowance_bytes'] == (levels + 1) * scratch.METADATA_ALLOWANCE
    assert measured['bytes'] == measured['file_bytes'] + measured['metadata_allowance_bytes']


def test_a_tree_past_the_interpreters_recursion_is_measured(tmp_path):
    levels = 1500  # short names, within the path limit, but deeper than a recursive walk can go
    deep(tmp_path, levels, 'd', b'y' * 100)
    try:
        measured = scratch.usage(tmp_path)
    finally:
        remove_deep(tmp_path, levels, 'd')
    assert (measured['dirs'], measured['files']) == (levels, 1)


def test_an_ordinary_tree_counts_the_same_as_the_path_walk(tmp_path):
    (tmp_path / 'a' / 'b' / 'c').mkdir(parents=True)
    (tmp_path / 'empty').mkdir()
    (tmp_path / 'a' / 'one').write_bytes(b'1' * 10)
    (tmp_path / 'a' / 'b' / 'two').write_bytes(b'2' * 5000)
    (tmp_path / 'a' / 'b' / 'c' / 'three').write_bytes(b'')
    os.link(tmp_path / 'a' / 'b' / 'two', tmp_path / 'a' / 'two-again')
    with open(tmp_path / 'sparse', 'wb') as sparse:
        sparse.truncate(1 << 20)
    (tmp_path / 'link-to-file').symlink_to(tmp_path / 'a' / 'one')
    (tmp_path / 'a' / 'link-to-folder').symlink_to(tmp_path / 'a' / 'b')
    os.mkfifo(tmp_path / 'fifo')
    assert scratch.usage(tmp_path) == path_walk(tmp_path)


def test_links_are_not_followed(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'large').write_bytes(b'z' * (1 << 20))
    (outside / 'inner').mkdir()
    measured_root = tmp_path / 'measured'
    measured_root.mkdir()
    (measured_root / 'to-folder').symlink_to(outside)
    (measured_root / 'to-file').symlink_to(outside / 'large')
    (measured_root / 'loop').symlink_to(measured_root)
    measured = scratch.usage(measured_root)
    assert (measured['files'], measured['dirs'], measured['file_bytes']) == (0, 0, 0)
    assert measured['metadata_allowance_bytes'] == 3 * scratch.METADATA_ALLOWANCE


def test_a_folder_replaced_by_a_link_after_it_was_read_is_not_followed(tmp_path, monkeypatch):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'large').write_bytes(b'z' * (1 << 20))
    measured_root = tmp_path / 'measured'
    (measured_root / 'swapped').mkdir(parents=True)
    real_open = os.open

    def swap_then_open(path, flags, *args, **kwargs):
        if path == 'swapped' and kwargs.get('dir_fd') is not None:
            os.rmdir(measured_root / 'swapped')
            os.symlink(outside, measured_root / 'swapped')
        return real_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'open', swap_then_open)
    measured = scratch.usage(measured_root)
    assert (measured['dirs'], measured['files'], measured['file_bytes']) == (1, 0, 0)


def test_a_folder_removed_during_the_walk_is_tolerated_and_a_missing_root_refused(tmp_path, monkeypatch):
    (tmp_path / 'gone').mkdir()
    (tmp_path / 'kept').write_bytes(b'k')
    real_open = os.open

    def remove_then_open(path, flags, *args, **kwargs):
        if path == 'gone' and kwargs.get('dir_fd') is not None:
            os.rmdir(tmp_path / 'gone')
        return real_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'open', remove_then_open)
    assert scratch.usage(tmp_path)['files'] == 1
    monkeypatch.undo()
    with pytest.raises(Refused, match='cannot measure'):
        scratch.usage(tmp_path / 'missing')


def test_an_unreadable_folder_still_refuses_and_no_descriptor_is_left(tmp_path):
    if os.geteuid() == 0:
        pytest.skip('root reads every folder')
    (tmp_path / 'a' / 'closed').mkdir(parents=True)
    (tmp_path / 'a' / 'closed').chmod(0)
    before = len(os.listdir('/proc/self/fd'))
    try:
        with pytest.raises(Refused, match='PermissionError'):
            scratch.usage(tmp_path)
    finally:
        (tmp_path / 'a' / 'closed').chmod(0o700)
    assert len(os.listdir('/proc/self/fd')) == before
