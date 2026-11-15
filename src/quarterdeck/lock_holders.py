"""Who holds a file's exclusive flock, read without taking the lock: one reader for every lock the framework reads.

A reader that takes a lock for an instant to learn whether it is held makes a holder starting that instant refuse it.
So the holder is read from the kernel's lock table (/proc/locks), by device and inode. That table alone cannot say a
lock is free: the kernel serves it a page per read and starts each page again at its position, so a row removed above
shifts a held lock's row past the reader. Measured here with eight processes each taking and releasing fifty locks, a
lock held throughout was missing from 1,948 of 77,138 readings, in runs of up to three readings back to back. A
reading with no row is therefore confirmed from the descriptors that hold the file open, whose fdinfo the kernel
prints for that one file under its own lock.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat


def table() -> str:
    """The kernel's table of file locks."""
    return Path("/proc/locks").read_text()


def nested_pid_namespace() -> bool:
    """Whether this process lies in a pid namespace below another, or cannot tell. The kernel's table leaves out, or
    shows with pid 0, a lock held outside the reader's namespace: there an empty table does not say a lock is free."""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("NSpid:"):
                return len(line.split()) != 2
    except OSError:
        pass
    return True


def descriptor_locks(path: Path, info: os.stat_result) -> set[int] | None:
    """The pids of the exclusive flocks held on ``path``'s file, read from the descriptors that have it open: each
    one's fdinfo, which the kernel prints for that one file under its own lock. The table cannot say that a lock is
    free: the kernel serves it a page per read and starts each page again at its position, so a row removed above
    shifts a held lock's row past the reader. Never takes the lock. None when the processes cannot be listed."""
    try:
        processes = [name for name in os.listdir("/proc") if name.isdigit()]
    except OSError:
        return None
    pids = set()
    for process in processes:
        folder = f"/proc/{process}/fd"
        try:
            descriptors = os.listdir(folder)
        except OSError:
            continue  # gone, or another user's
        for descriptor in descriptors:
            try:
                if os.path.basename(os.readlink(f"{folder}/{descriptor}")) != path.name:
                    continue
                opened = os.stat(f"{folder}/{descriptor}")
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    continue
                lines = Path(f"/proc/{process}/fdinfo/{descriptor}").read_text().splitlines()
            except (OSError, UnicodeError):
                continue  # closed meanwhile
            for line in lines:
                fields = line.split()
                if fields[:1] == ["lock:"] and fields[2:5] == ["FLOCK", "ADVISORY", "WRITE"]:
                    try:
                        pids.add(int(fields[5]))
                    except (IndexError, ValueError):
                        return None
    return pids


def reading(path: Path) -> tuple[bool | None, int | None] | None:
    """What the kernel's lock table (/proc/locks) says of ``path``'s exclusive flock, by device and inode, without
    taking the lock: (None, None) when there is no such file, (False, None) when it is free, (True, pid) when it is
    held, where pid is the one holder the table names, or None when it names none or several. A table with no row for
    it is confirmed from the descriptors that have the file open (``descriptor_locks``) before it reads as free. A
    row whose pid is 0 or less names no holder: a lock held outside this namespace, or a freed owner's, as older
    kernels show it, and a signal to pid 0 would reach the caller's own group. None when the table cannot say: it
    cannot be read, the file is not a regular one, or it shows no holder where it may leave one out (this process lies
    in a nested pid namespace, or a row names this inode on another device: a file system whose table and stat number
    devices differently)."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None, None
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    try:
        lines = table().splitlines()
    except (OSError, UnicodeError):
        return None
    device = f"{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}"
    pids, elsewhere = set(), False
    for line in lines:
        fields = line.split()
        if len(fields) < 6 or fields[1:4] != ["FLOCK", "ADVISORY", "WRITE"]:
            continue  # a waiter ("->"), a shared or another kind of lock
        where = fields[5].rsplit(":", 1)
        if len(where) != 2 or where[1] != str(info.st_ino):
            continue
        if where[0] != device:
            elsewhere = True
            continue
        try:
            pids.add(int(fields[4]))
        except ValueError:
            return None
    if not pids:  # free, unless the table may leave the holder out, or this reading passed over its row
        if elsewhere or nested_pid_namespace():
            return None
        pids = descriptor_locks(path, info)
        if pids is None:
            return None
        if not pids:
            return False, None
    named = {pid for pid in pids if pid > 0}
    return True, (named.pop() if len(named) == 1 and len(pids) == 1 else None)
