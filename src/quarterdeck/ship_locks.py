"""The ship's own lock files: every one under ``runtime/locks``, and every wait on one bounded.

``flock`` needs no write access: a descriptor opened read-only takes an exclusive lock. So a lock file a
jailed seat can open is one it can hold, and a service or a ``qd`` command waiting on it would wait for as
long as that seat chose. Every jail hides ``runtime/locks`` behind an empty private tmpfs, so a lock kept
there is out of every jail's reach; what a jail can still open is never locked by the ship. And no wait is
open-ended: a lock still held at its bound raises ``Busy``, named, and the caller skips or refuses with that
reason, as the service's pass does for any failing step, and tries again later.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .ship import ShipError

FOLDER = "locks"  # runtime/locks, hidden inside every jail (sandbox.LOCKS)
WAIT = 30.0  # seconds any caller waits for a ship lock before it gives up with the lock's name
POLL = 0.02


class Busy(ShipError):
    """A ship lock stayed held for the whole bounded wait."""


def folder(runtime: Path) -> Path:
    return Path(runtime) / FOLDER


def path(runtime: Path, *parts: str) -> Path:
    """A lock's place under ``runtime/locks``: each part one plain name, never a path of its own."""
    for part in parts:
        if not isinstance(part, str) or not part or part in (".", "..") or "/" in part or "\0" in part:
            raise ShipError(f"lock name {part!r} is not a plain name")
    return folder(runtime).joinpath(*parts)


def hashed(runtime: Path, kind: str, key: str) -> Path:
    """A lock named for something whose own name is no safe file name: a seat, a data file's path."""
    return path(runtime, kind, hashlib.sha256(key.encode()).hexdigest()[:32] + ".lock")


def for_seat_file(seat_folder: Path, name: str) -> Path:
    """The lock for a file of a seat's (``runtime/seats/<seat>/<name>`` or ``runtime/trusted/<seat>/<name>``), kept
    beside the ship's other locks so that no jail can hold it. A file keeps one lock in either folder."""
    seat_folder = Path(seat_folder)
    runtime = seat_folder.parent.parent if seat_folder.parent.name in ("seats", "trusted") else seat_folder
    return path(runtime, "seat-files", f"{seat_folder.name}--{name}.lock")


def prepare(target: Path) -> None:
    """Make the lock's folders, private, refusing a link or anything that is not a folder on the way."""
    root = target.parent
    chain = []
    while root.name != FOLDER:
        if root == root.parent:
            raise ShipError(f"{target}: a ship lock lies outside runtime/{FOLDER}; refused")
        chain.append(root)
        root = root.parent
    root.parent.mkdir(parents=True, exist_ok=True)  # the runtime itself, as any of its stores makes it
    for folder_path in (root, *reversed(chain)):
        try:
            folder_path.mkdir(mode=0o700)
        except FileExistsError:
            pass
        info = folder_path.lstat()
        if stat.S_ISLNK(info.st_mode):
            from .post_dir import _link_held
            _link_held(folder_path)  # a link on the way is a hold, in the post store's own words
        if not stat.S_ISDIR(info.st_mode):
            raise ShipError(f"{folder_path}: a ship lock folder is not a real folder; retained")


@contextmanager
def held(lock: Path, what: str, *, wait: float = WAIT, shared: bool = False) -> Iterator[int]:
    """Hold ``lock`` (exclusive unless ``shared``), waiting at most ``wait`` seconds; else raise ``Busy``.

    The file is opened without following a link, must be a regular file, and must still be the same file
    once it is held; each refusal names the lock and keeps the file for inspection.
    """
    lock = Path(lock)
    prepare(lock)
    try:
        descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
    except OSError as error:
        if error.errno == errno.ELOOP:
            from .post_dir import _link_held
            _link_held(lock)
        raise ShipError(f"{what}: lock {lock} cannot be opened ({error.strerror or type(error).__name__}); retained") from None
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ShipError(f"{what}: lock {lock} is not a regular file; retained")
        deadline = time.monotonic() + max(0.0, wait)
        operation = (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB
        while True:
            try:
                fcntl.flock(descriptor, operation)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Busy(f"{what}: lock {lock.name} still held after {wait:g}s; skipped, tried again later") from None
                time.sleep(min(POLL, remaining))
        current = lock.lstat()
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise ShipError(f"{what}: lock {lock} changed while it was taken; retained")
        yield descriptor
    finally:
        os.close(descriptor)

