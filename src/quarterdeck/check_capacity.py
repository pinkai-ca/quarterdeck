"""A daily live check shares one existing slot with the repository's test tool.

Only the four established lock files are opened, never created or rewritten.
The kernel lock is held through the check and its cleanup. There is one pass,
with no waiting, retry, test-tool import or alternate lock namespace.
"""

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
import subprocess

from .ship import ShipError

NAMES = ('slot.lock', 'slot-2.lock', 'slot-3.lock', 'slot-4.lock')
BUSY = 'all four test slots are occupied; try again the next night'
UNKNOWN = 'shared test slot availability is unknown; try again the next night'


def folder(ship):
    from .install import source_repo
    repo = source_repo(ship)
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    done = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--path-format=absolute', '--git-common-dir'],
                          env=env, capture_output=True, text=True, timeout=5, check=False)
    if done.returncode or not done.stdout.strip():
        raise ShipError(UNKNOWN)
    return Path(done.stdout.strip()).parent / '.testruns'


@contextmanager
def reserve_at(path):
    directory = descriptor = None
    reason = UNKNOWN
    try:
        directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise OSError('shared slot folder is not private')
        reason = BUSY
        for name in NAMES:
            descriptor = os.open(name, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise OSError('shared slot file is not owned and regular')
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(descriptor)
                descriptor = None
                continue
            reason = None
            break
    except OSError:
        reason = UNKNOWN
    try:
        yield reason
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)


@contextmanager
def reserve(ship):
    try:
        path = folder(ship)
    except (OSError, ShipError, subprocess.SubprocessError, ValueError):
        yield UNKNOWN
        return
    with reserve_at(path) as reason:
        yield reason
