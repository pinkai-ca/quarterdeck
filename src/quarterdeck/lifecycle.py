"""The shared seat-transition claim, also held by ordinary delivery."""

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat

from . import post_dir
from .ship import ShipError


class Busy(ShipError):
    """Another caller owns this seat's transition or delivery."""


@contextmanager
def claim(runtime: Path, name: str):
    """Keep the same lock inode outside removable homes; never unlink it."""
    path = runtime / 'post' / 'lifecycle' / f'{name}.lock'
    descriptor = None
    try:
        try:
            descriptor = post_dir.open_lock(path, mode=0o600, extra=os.O_NONBLOCK)
            os.set_inheritable(descriptor, False)
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise ShipError(f'{name}: lifecycle lock is not a regular file; retained')
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Busy(f'{name}: another lifecycle transition is in progress; retry when it ends') from None
            current = path.lstat()
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise ShipError(f'{name}: lifecycle lock identity changed; transition refused')
        except OSError as error:
            raise ShipError(f'{name}: lifecycle coordination unavailable: {error}') from error
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
