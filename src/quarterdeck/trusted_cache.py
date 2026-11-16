"""What the service derives from a seat's small trusted files, kept while those files stand as they were read.

A value is kept per key beside the stamps of the files it was read from: device, inode, size and the nanosecond
modification and change times of each, or None for a file that is absent. It is answered again only while every
stamp is the same, and it is kept at all only where no stamp moved while it was read and every present file was older
than the post's filesystem tick (post.POST_TIMESTAMP_TICK_NS) when the read began: the post store's own rule, so a
write inside a coarse timestamp can never be missed. Anything young, changed or unreadable is read afresh. One entry
per key, dropped when its first file goes; nothing is kept across a restart. Callers keep only values nobody changes,
or hand out copies. No record reader imports this module.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from .post import POST_TIMESTAMP_TICK_NS

_now = time.time_ns  # the clock a read is certified by; tests inject their own


def now() -> int:
    return _now()


def stamp_of(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def stamp(path: Path) -> tuple | None:
    """A file's stamp, None where it is absent. Any other failure to read it raises: nothing is kept on a doubt."""
    try:
        return stamp_of(os.stat(path))
    except FileNotFoundError:
        return None


def link_stamp(path: Path) -> tuple | None:
    """The stamp of the name itself, a link not followed: for a file read without following one."""
    try:
        return stamp_of(os.lstat(path))
    except FileNotFoundError:
        return None


def certified(stamps: tuple, started: int) -> bool:
    return all(entry is None or max(entry[3], entry[4]) + POST_TIMESTAMP_TICK_NS < started for entry in stamps)


class Kept:
    """One kept value per key, beside the stamps it was read under."""

    def __init__(self):
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[tuple, object]] = {}

    def get(self, key: str, stamps: tuple):
        with self._lock:
            entry = self._entries.get(key)
        return entry[1] if entry is not None and entry[0] == stamps else None

    def keep(self, key: str, before: tuple, after: tuple, started: int, value) -> bool:
        """Keep ``value`` read under ``before`` only where ``after`` is the same and was certified when it began."""
        with self._lock:
            if before == after and certified(before, started):
                self._entries[key] = (before, value)
                return True
            self._entries.pop(key, None)
            return False

    def drop(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __contains__(self, key: str) -> bool:
        with self._lock:
            return key in self._entries
