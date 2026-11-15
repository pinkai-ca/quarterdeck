"""Measuring and removing a tree the tool owns: anchored at a verified parent descriptor, never following a
link, never crossing a filesystem, refusing anything not this user's, and checking the exact identity
recorded when the tool made it. A tree is fully measured and validated before the first unlink."""
from __future__ import annotations

import os
import errno
import secrets
import stat
import time

from .layout import DIRECTORY, Refused, identity
from . import resources


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
            marker = os.open(MARKER, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            try:
                marker_info = os.fstat(marker)
                if not stat.S_ISREG(marker_info.st_mode) or marker_info.st_uid != os.getuid():
                    raise OSError('owner marker is not this user\'s regular file')
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


class Budget:
    """One budget for validation, traversal and retries, rather than one per new file."""

    def __init__(self, seconds=45.0, entries=2_000_000):
        self.deadline = time.monotonic() + seconds
        self.remaining = entries

    def check(self, where):
        self.remaining -= 1
        if self.remaining < 0 or time.monotonic() >= self.deadline:
            raise Refused(f'{where}: retirement budget exhausted; exact remaining tree retained')


def _retire_walk(fd, device, where, tally, budget, *, remove, top=False):
    """Iterative, one held descriptor per level; racing writers still share one bounded budget."""
    def names(folder, path):
        budget.check(path)
        info = os.fstat(folder)
        if remove and not info.st_mode & stat.S_IWUSR:
            os.fchmod(folder, stat.S_IMODE(info.st_mode) | stat.S_IRWXU)
        found = []
        # Close the iterator before descending, rather than holding a second descriptor per level.
        with os.scandir(folder) as entries:
            for item in entries:
                budget.check(path)
                found.append(item.name)
        return iter(found)

    stack, depth = [], 0
    try:
        stack.append((fd, where, names(fd, where), None, None))
        while stack:
            parent, path, entries, own_name, expected = stack[-1]
            name = next(entries, None)
            if name is None:
                stack.pop()
                if own_name is not None:
                    try:
                        budget.check(path)
                        above = stack[-1][0]
                        if identity(os.stat(own_name, dir_fd=above, follow_symlinks=False)) != expected:
                            raise Refused(f'{path}: replaced during retirement; replacement retained')
                        if remove:
                            os.rmdir(own_name, dir_fd=above)
                        tally.dirs += 1
                    except FileNotFoundError:
                        pass
                    finally:
                        os.close(parent)
                continue
            child_path = path / name
            budget.check(child_path)
            if top and len(stack) == 1 and name == MARKER:
                continue  # partial retirement must keep its ownership proof
            try:
                entry = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if entry.st_uid != os.getuid() or entry.st_dev != device:
                    raise Refused(f'{child_path}: ownership or filesystem differs; remainder retained')
                if stat.S_ISDIR(entry.st_mode):
                    depth = len(stack)
                    child = os.open(name, DIRECTORY, dir_fd=parent)
                    try:
                        if identity(os.fstat(child)) != identity(entry):
                            raise Refused(f'{child_path}: replaced while being walked; remainder retained')
                        children = names(child, child_path)
                        stack.append((child, child_path, children, name, identity(entry)))
                    except BaseException:
                        os.close(child)
                        raise
                else:
                    if remove:
                        os.unlink(name, dir_fd=parent)
                    if stat.S_ISLNK(entry.st_mode):
                        tally.links += 1
                    else:
                        tally.files += 1
                        tally.bytes += entry.st_size
            except FileNotFoundError:
                continue  # an entry vanished inside the already verified tree
    except OSError as error:
        if error.errno in (errno.EMFILE, errno.ENFILE):
            raise Refused(resources.descriptor_refusal('retire working files', depth) + '; remainder retained') from None
        raise
    finally:
        for child, *_ in stack[1:]:  # the caller keeps ownership of the root descriptor
            os.close(child)


def retire(parent_fd, name, expected, where, token, *, budget=None):
    """Bounded retirement of a token-owned tree; return a report even when a writer wins.

    No process is signalled. A failed/partial traversal leaves the ownership marker for a later explicit
    retry. Finalization's outer alarm is a BaseException and deliberately escapes this ordinary handler.
    """
    tally, fd = Tally(), None
    budget = budget or Budget()
    try:
        if not token or expected is None:
            raise Refused(f'{where}: missing recorded identity/owner token; nothing removed')
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return {'outcome': 'complete', 'scratch': str(where), 'removed': tally.as_dict()}
        fd = _open_exact(parent_fd, name, expected, where, token)
        _retire_walk(fd, os.fstat(fd).st_dev, where, Tally(), budget, remove=False, top=True)
        while True:
            budget.check(where)
            try:
                _retire_walk(fd, os.fstat(fd).st_dev, where, tally, budget, remove=True, top=True)
                if identity(os.stat(name, dir_fd=parent_fd, follow_symlinks=False)) != list(expected):
                    raise Refused(f'{where}: replaced during retirement; replacement retained')
                os.unlink(MARKER, dir_fd=fd)
                try:
                    os.rmdir(name, dir_fd=parent_fd)
                except OSError:
                    # Keep the original proof through an ordinary ENOTEMPTY race. Exclusive creation
                    # refuses a replacement marker; the anchored fd cannot follow a replacement tree.
                    marker = os.open(MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                     0o600, dir_fd=fd)
                    try:
                        os.write(marker, token.encode())
                    finally:
                        os.close(marker)
                    raise
                tally.files += 1
                tally.bytes += len(token)
                tally.dirs += 1
                return {'outcome': 'complete', 'scratch': str(where), 'removed': tally.as_dict()}
            except OSError as error:
                if error.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                    raise
                budget.check(where)
                time.sleep(min(0.01, max(0, budget.deadline - time.monotonic())))
    except Exception as error:
        return {'outcome': 'held', 'scratch': str(where), 'removed': tally.as_dict(), 'reason': str(error)}
    finally:
        if fd is not None:
            os.close(fd)


def unlink_file(parent_fd: int, name: str, where) -> int:
    try:
        entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return 0
    if entry.st_uid != os.getuid() or not (stat.S_ISREG(entry.st_mode) or stat.S_ISLNK(entry.st_mode)):
        raise Refused(f'{where}: not this user\'s plain file; nothing removed')
    os.unlink(name, dir_fd=parent_fd)
    return entry.st_size if stat.S_ISREG(entry.st_mode) else 0
