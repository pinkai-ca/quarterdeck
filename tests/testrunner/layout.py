"""The fixed layout under ``<project>/.testruns/``, its authority, atomic control state and the one slot.

Authority over the folder comes from derivation, never from what the folder says about itself: the project
is the parent of the target's Git common directory, the root is ``<project>/.testruns``, and every component
is checked by ``lstat`` (a real directory, not a link, ours, on the project's device, private) and opened
without following links. Identities recorded in control state are consistency checks on top of that.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import time

from . import FORMAT

ROOT_NAME = '.testruns'
SOCKETS = 's'      # the fixed short anchor for native test sockets, shared with tests/conftest.py
RUN_SOCKETS = 'r'  # tool-run containers do not appear among direct pytest's shared socket containers
PLAIN_TEMP = 't'   # TMPDIR of a plain pytest run (tests/conftest.py)
RUNS = 'runs'      # scratch, always removed
RESULTS = 'results'  # retained evidence: result.json, report.md, failures/
RETIREMENTS = 'retirements'  # explicit retirement command results; the original test evidence stays byte-identical
CHANGES = 'changes'  # durable control state: registrations and clean journals
KEEPS = 'keeps'      # durable control state: explicit keep notes
SLOT = 'slot.lock'
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class Refused(Exception):
    """A refusal: the reason and, where one exists, what would lift it. Exit 2, nothing removed."""


def identity(info: os.stat_result) -> list:
    return [info.st_dev, info.st_ino, info.st_uid]


def private_directory(info: os.stat_result, where, device=None) -> None:
    if not stat.S_ISDIR(info.st_mode):
        raise Refused(f'{where} is not a directory (a link or a file stands there); nothing touched')
    if info.st_uid != os.getuid():
        raise Refused(f'{where} belongs to uid {info.st_uid}, not this user; nothing touched')
    if device is not None and info.st_dev != device:
        raise Refused(f'{where} is on another filesystem than the project; nothing touched')
    if stat.S_IMODE(info.st_mode) & 0o022:
        raise Refused(f'{where} is writable by others; nothing touched')


def open_child(parent_fd: int, name: str, where, *, create: bool, device: int, mode: int = 0o700) -> int:
    """Open (or make) one directory below an already verified one, never following a link."""
    if create:
        try:
            os.mkdir(name, mode, dir_fd=parent_fd)
        except FileExistsError:
            pass
    try:
        fd = os.open(name, DIRECTORY, dir_fd=parent_fd)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise Refused(f'{where} is a link or not a directory; nothing touched') from None
        raise
    try:
        info = os.fstat(fd)
        private_directory(info, where, device)
        if identity(os.stat(name, dir_fd=parent_fd, follow_symlinks=False)) != identity(info):
            raise Refused(f'{where} was replaced while being opened; nothing touched')
    except BaseException:
        os.close(fd)
        raise
    return fd


class Root:
    """``<project>/.testruns``: opened once per command and held by descriptor."""

    def __init__(self, project: Path, *, create: bool = True):
        self.project = project
        self.path = project / ROOT_NAME
        project_fd = os.open(project, DIRECTORY)
        try:
            pinfo = os.fstat(project_fd)
            if pinfo.st_uid != os.getuid() or identity(os.lstat(project)) != identity(pinfo):
                raise Refused(f'project {project} is not this user\'s plain directory')
            self.device = pinfo.st_dev
            existed = True
            try:
                os.stat(ROOT_NAME, dir_fd=project_fd, follow_symlinks=False)
            except FileNotFoundError:
                if not create:
                    raise Refused(f'{self.path} does not exist; nothing to do') from None
                existed = False
            self.fd = open_child(project_fd, ROOT_NAME, self.path, create=create, device=self.device)
        finally:
            os.close(project_fd)
        if not existed:
            # The folder ignores itself, so it never shows as untracked even before a checkout's own
            # .gitignore names it; .gitignore names it too.
            try:
                initial_ignore(self.fd, self.path)
            except BaseException:
                os.close(self.fd)
                raise
        ignored = subprocess.run(['git', '-C', str(project), 'check-ignore', '-q', '--', f'{ROOT_NAME}/{SOCKETS}'],
                                 capture_output=True, env=git_environment())
        if ignored.returncode != 0:
            raise Refused(f'{self.path} is not ignored by the project repository; add {ROOT_NAME}/ to .gitignore')

    def child(self, *names: str, create: bool = True) -> int:
        """A verified descriptor for a directory under the root; the caller closes it."""
        fd = os.dup(self.fd)
        where = self.path
        try:
            for name in names:
                if not name or '/' in name or name in ('.', '..'):
                    raise Refused(f'unsafe layout name {name!r}')
                where = where / name
                child = open_child(fd, name, where, create=create, device=self.device)
                os.close(fd)
                fd = child
        except BaseException:
            os.close(fd)
            raise
        return fd

    def close(self) -> None:
        os.close(self.fd)


def initial_ignore(dir_fd: int, where: Path) -> None:
    """Coordinate initial writers on Linux's already verified root directory descriptor.

    Directory flock is Linux-specific, as is the test tool's /proc process evidence. The brief lock keeps
    an equal peer's exclusive write and fsync complete before it is read; no control file is introduced.
    """
    name, data = '.gitignore', b'*\n'
    fcntl.flock(dir_fd, fcntl.LOCK_EX)
    try:
        try:
            write_new(dir_fd, name, data)
        except FileExistsError:
            try:
                peer = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                               dir_fd=dir_fd)
                try:
                    info = os.fstat(peer)
                    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                        raise Refused(f'{where / name} is not this user\'s regular file; nothing touched')
                    raw = os.read(peer, len(data) + 1)
                    current = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                    if identity(current) != identity(info) or raw != data or info.st_size != len(data):
                        raise Refused(f'{where / name} is not the intended initial ignore file; nothing touched')
                finally:
                    os.close(peer)
            except OSError:
                raise Refused(f'{where / name} could not be verified; nothing touched') from None
    finally:
        fcntl.flock(dir_fd, fcntl.LOCK_UN)


def git_environment() -> dict:
    """The caller's environment for the tool's own git commands, minus what would redirect the repository."""
    return {key: value for key, value in os.environ.items()
            if key not in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR',
                           'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_NAMESPACE')}


def write_new(dir_fd: int, name: str, data: bytes) -> None:
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dir_fd)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def write_atomic(dir_fd: int, name: str, data: bytes) -> None:
    """Write beside, fsync, rename over, fsync the directory: a reader sees the old file or the new one."""
    temporary = f'.{name}.{os.getpid()}.{secrets.token_hex(4)}.tmp'
    write_new(dir_fd, temporary, data)
    try:
        os.replace(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        os.unlink(temporary, dir_fd=dir_fd)
        raise
    os.fsync(dir_fd)


def write_json(dir_fd: int, name: str, data: dict) -> None:
    write_atomic(dir_fd, name, (json.dumps({'format': FORMAT, **data}, indent=2, sort_keys=True) + '\n').encode())


def read_json(dir_fd: int, name: str, where) -> dict | None:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise Refused(f'{where} is a link; nothing touched') from None
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise Refused(f'{where} is not this user\'s regular file; nothing touched')
        with os.fdopen(os.dup(fd), 'rb') as handle:
            raw = handle.read()
    finally:
        os.close(fd)
    try:
        data = json.loads(raw)
    except ValueError:
        raise Refused(f'{where} is not readable control state (malformed JSON); nothing touched') from None
    if not isinstance(data, dict) or data.get('format') != FORMAT:
        raise Refused(f'{where} has format {data.get("format") if isinstance(data, dict) else "?"}; '
                      f'this tool reads format {FORMAT} only; nothing touched')
    return data


def change_file(change: str) -> str:
    """A branch name as a file name: refused unless it is plain, '/' written as '+'."""
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,120}', change) or '..' in change or change.endswith('/'):
        raise Refused(f'change name {change!r} is not a plain branch name')
    return change.replace('/', '+')


def new_run_id() -> str:
    return time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(2)


def run_name(run: str) -> str:
    if not re.fullmatch(r'\d{8}-\d{6}-[0-9a-f]{4}', run):
        raise Refused(f'{run!r} is not a run id (yyyymmdd-hhmmss-xxxx)')
    return run


class Slot:
    """The one exclusive slot every real run, clean and recovery holds, its holder written inside it."""

    def __init__(self, root: Root):
        self.root = root
        self.fd = os.open(SLOT, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=root.fd)
        info = os.fstat(self.fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            os.close(self.fd)
            raise Refused(f'{root.path / SLOT} is not this user\'s regular file; nothing touched')

    def holder(self) -> str:
        try:
            data = json.loads(os.pread(self.fd, 4096, 0) or b'{}')
        except ValueError:
            return 'unknown holder (unreadable slot note)'
        return ', '.join(f'{key} {data[key]}' for key in ('verb', 'run', 'change', 'seat', 'pid', 'since') if key in data) \
            or 'unknown holder'

    def acquire(self, note: dict, wait: float = 0.0) -> None:
        deadline = time.monotonic() + max(wait, 0.0)
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Refused(f'the test slot is busy: {self.holder()}; '
                                  f'{"waited " + str(int(wait)) + " s; " if wait else ""}'
                                  'try again when it ends, or pass --wait SECONDS') from None
                time.sleep(0.5)
        os.ftruncate(self.fd, 0)
        os.pwrite(self.fd, json.dumps({**note, 'pid': os.getpid(), 'since': time.strftime('%Y-%m-%dT%H:%M:%S%z')}).encode(), 0)

    def release(self) -> None:
        try:
            os.ftruncate(self.fd, 0)
        finally:
            os.close(self.fd)  # closing the descriptor releases the lock, also through failure
