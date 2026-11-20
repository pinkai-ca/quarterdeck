"""The shared seat-transition claim, also held by ordinary delivery."""

from contextlib import contextmanager
from contextvars import ContextVar
import fcntl
import os
from pathlib import Path
import stat
import time

from . import post_dir
from .ship import ShipError


WAIT_SECONDS = 30.0  # explicit lifecycle callers wait at most this long for one seat

_EXPLICIT = ContextVar('explicit_lifecycle', default=False)


@contextmanager
def explicit():
    """Scope a person's or script's act without changing background callers' policy."""
    token = _EXPLICIT.set(True)
    try:
        yield
    finally:
        _EXPLICIT.reset(token)


def explicit_wait() -> float:
    return WAIT_SECONDS if _EXPLICIT.get() else 0


class Busy(ShipError):
    """Another caller owns this seat's transition or delivery."""


def lock_path(runtime: Path, name: str) -> Path:
    """A seat's lifecycle lock, under runtime/locks, which every jail hides."""
    from . import ship_locks
    return ship_locks.path(runtime, 'lifecycle', f'{name}.lock')


@contextmanager
def claim(runtime: Path, name: str, *, wait: float = 0, retire: bool = False):
    """Claim the named inode; a completed temporary check may retire its empty lock."""
    path = lock_path(runtime, name)
    descriptor = None
    try:
        try:
            from . import ship_locks
            ship_locks.prepare(path)
            descriptor = post_dir.open_lock(path, mode=0o600, extra=os.O_NONBLOCK)
            os.set_inheritable(descriptor, False)
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise ShipError(f'{name}: lifecycle lock is not a regular file; retained')
            deadline = time.monotonic() + wait
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise Busy(f'{name}: another lifecycle transition is in progress; retry when it ends') from None
                    time.sleep(min(0.05, remaining))
            current = path.lstat()
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise ShipError(f'{name}: lifecycle lock identity changed; transition refused')
            if retire and (opened.st_nlink != 1 or opened.st_size):
                raise ShipError(f'{name}: lifecycle check lock is not an empty owned file; retained')
        except OSError as error:
            raise ShipError(f'{name}: lifecycle coordination unavailable: {error}') from error
        yield
        if retire:
            path.unlink()
    finally:
        if descriptor is not None:
            os.close(descriptor)
