"""Measuring and removing a tree the tool owns: anchored at a verified parent descriptor, never following a
link, never crossing a filesystem, refusing anything not this user's, and checking the exact identity
recorded when the tool made it. A tree is fully measured and validated before the first unlink."""
from __future__ import annotations

import os
import secrets
import stat

from .layout import DIRECTORY, Refused, identity


class Tally:
    def __init__(self):
        self.files = self.dirs = self.links = self.bytes = 0

    def as_dict(self) -> dict:
        return {'files': self.files, 'dirs': self.dirs, 'links': self.links, 'bytes': self.bytes}


MARKER = '.owner'  # a random token written when the tool makes a directory, recorded in its state


def mark(dir_fd: int) -> str:
    """Write a new directory's owner token: identity alone cannot tell a recreated directory that reused the
    original's inode number from the original."""
    token = secrets.token_hex(16)
    fd = os.open(MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
    try:
        os.write(fd, token.encode())
    finally:
        os.close(fd)
    return token


def _open_exact(parent_fd: int, name: str, expected, where, token=None) -> int:
    try:
        fd = os.open(name, DIRECTORY, dir_fd=parent_fd)
    except OSError as error:
        raise Refused(f'{where}: cannot open as a plain directory ({error.strerror}); nothing removed') from None
    info = os.fstat(fd)
    if expected is not None and identity(info) != list(expected):
        os.close(fd)
        raise Refused(f'{where}: identity changed since the tool made it (replaced); nothing removed')
    if info.st_uid != os.getuid():
        os.close(fd)
        raise Refused(f'{where}: not this user\'s; nothing removed')
    if token is not None:
        try:
            marker = os.open(MARKER, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                found = os.read(marker, 64).decode(errors='replace')
            finally:
                os.close(marker)
        except OSError:
            found = None
        if found != token:
            os.close(fd)
            raise Refused(f'{where}: its owner token is missing or differs (recreated); nothing removed')
    return fd


def _walk(fd: int, device: int, where, tally: Tally, remove: bool) -> None:
    info = os.fstat(fd)
    if remove and not info.st_mode & stat.S_IWUSR:
        os.fchmod(fd, stat.S_IMODE(info.st_mode) | stat.S_IRWXU)  # the run's own read-only package folders
    for name in os.listdir(fd):
        entry = os.stat(name, dir_fd=fd, follow_symlinks=False)
        path = where / name
        if entry.st_uid != os.getuid():
            raise Refused(f'{path}: not this user\'s; nothing more removed')
        if stat.S_ISDIR(entry.st_mode):
            if entry.st_dev != device:
                raise Refused(f'{path}: another filesystem is mounted inside; nothing more removed')
            child = os.open(name, DIRECTORY, dir_fd=fd)
            try:
                if identity(os.fstat(child)) != identity(entry):
                    raise Refused(f'{path}: replaced while being walked; nothing more removed')
                _walk(child, device, path, tally, remove)
            finally:
                os.close(child)
            tally.dirs += 1
            if remove:
                os.rmdir(name, dir_fd=fd)
        else:
            if stat.S_ISLNK(entry.st_mode):
                tally.links += 1
            else:
                tally.files += 1
                tally.bytes += entry.st_size
            if remove:
                os.unlink(name, dir_fd=fd)


def measure(parent_fd: int, name: str, expected, where, token=None) -> Tally:
    fd = _open_exact(parent_fd, name, expected, where, token)
    try:
        tally = Tally()
        _walk(fd, os.fstat(fd).st_dev, where, tally, remove=False)
        return tally
    finally:
        os.close(fd)


def remove(parent_fd: int, name: str, expected, where, token=None) -> Tally:
    """Validate the whole tree, then remove it; the counts are what was removed."""
    tally = measure(parent_fd, name, expected, where, token)
    fd = _open_exact(parent_fd, name, expected, where, token)
    try:
        _walk(fd, os.fstat(fd).st_dev, where, Tally(), remove=True)
    finally:
        os.close(fd)
    if expected is not None and identity(os.stat(name, dir_fd=parent_fd, follow_symlinks=False)) != list(expected):
        raise Refused(f'{where}: replaced during removal; its emptied original is gone, the replacement kept')
    os.rmdir(name, dir_fd=parent_fd)
    tally.dirs += 1
    return tally


def unlink_file(parent_fd: int, name: str, where) -> int:
    try:
        entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return 0
    if entry.st_uid != os.getuid() or not (stat.S_ISREG(entry.st_mode) or stat.S_ISLNK(entry.st_mode)):
        raise Refused(f'{where}: not this user\'s plain file; nothing removed')
    os.unlink(name, dir_fd=parent_fd)
    return entry.st_size if stat.S_ISREG(entry.st_mode) else 0
