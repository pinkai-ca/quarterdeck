"""The fixed layout under ``<project>/.testruns/``, its authority, atomic control state and four run slots.

Authority over the folder comes from derivation, never from what the folder says about itself: the project
is the parent of the target's Git common directory, the root is ``<project>/.testruns``, and every component
is checked by ``lstat`` (a real directory, not a link, ours, on the project's device, private) and opened
without following links. Identities recorded in control state are consistency checks on top of that.
"""
from __future__ import annotations

import errno
from contextlib import contextmanager
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
SLOT = 'slot.lock'  # first slot retains the original name for older tool copies
SLOT_COUNT = 4
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
    an equal peer's exclusive write is complete before it is read; no control file is introduced.
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
        remaining = memoryview(data)
        while remaining:
            remaining = remaining[os.write(fd, remaining):]
    finally:
        os.close(fd)


def write_atomic(dir_fd: int, name: str, data: bytes) -> None:
    """Write beside, close, then rename over: readers see one whole file; no storage sync."""
    temporary = f'.{name}.{os.getpid()}.{secrets.token_hex(4)}.tmp'
    write_new(dir_fd, temporary, data)
    try:
        os.replace(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        os.unlink(temporary, dir_fd=dir_fd)
        raise


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


def reserve_run_id(root: Root) -> str:
    """Reserve a fresh result directory exclusively; a random candidate is never ownership."""
    parent = root.child(RESULTS)
    try:
        while True:
            run = new_run_id()
            try:
                os.mkdir(run, mode=0o700, dir_fd=parent)
            except FileExistsError:
                continue
            return run
    finally:
        os.close(parent)


def run_name(run: str) -> str:
    if not re.fullmatch(r'\d{8}-\d{6}-[0-9a-f]{4}', run):
        raise Refused(f'{run!r} is not a run id (yyyymmdd-hhmmss-xxxx)')
    return run


class Slot:
    """One or more of four eight-worker slots, acquired without holding a partial set while waiting."""

    def __init__(self, root: Root):
        self.root, self.fds, self.selected, self.held = root, [], None, []
        try:
            for index in range(SLOT_COUNT):
                name = SLOT if index == 0 else f'slot-{index + 1}.lock'
                fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=root.fd)
                self.fds.append(fd)
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                    raise Refused(f'{root.path / name} is not this user\'s regular file; nothing touched')
            self.fd = self.fds[0]
        except BaseException:
            self.release()
            raise

    def holder(self) -> str:
        holders = []
        for index, fd in enumerate(self.fds):
            try:
                data = json.loads(os.pread(fd, 4096, 0) or b'{}')
                note = ', '.join(f'{key} {data[key]}' for key in ('verb', 'run', 'change', 'seat', 'pid', 'since') if key in data)
            except (ValueError, TypeError):
                note = 'unreadable holder note'
            holders.append(f'slot {index + 1}: {note or "unknown holder"}')
        return '; '.join(holders)

    def acquire(self, note: dict, wait: float = 0.0, *, count: int = 1) -> None:
        if type(count) is not int or not 1 <= count <= SLOT_COUNT:
            raise Refused(f'a run needs 1 to {SLOT_COUNT} test slots')
        deadline = time.monotonic() + max(wait, 0.0)
        while True:
            acquired = []
            try:
                for index, fd in enumerate(self.fds):
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    acquired.append((index, fd))
                    if len(acquired) == count:
                        self.selected, self.fd = acquired[0]
                        self.held = [entry[1] for entry in acquired]
                        self.describe(note)
                        return
            finally:
                if len(acquired) < count:
                    # An unsuccessful attempt holds nothing while it waits for the complete set.
                    for index, fd in acquired:
                        fcntl.flock(fd, fcntl.LOCK_UN)
            if time.monotonic() >= deadline:
                reason = (f'all {SLOT_COUNT} test slots are busy' if count == 1 else
                          f'{count} free test slots are needed together')
                raise Refused(f'{reason}: {self.holder()}; '
                              f'{"waited " + str(int(wait)) + " s; " if wait else ""}'
                              'try again when one ends, or pass --wait SECONDS')
            time.sleep(0.5)

    def describe(self, note: dict) -> None:
        """Publish the current run identity in every slot this caller already holds."""
        for index, fd in enumerate(self.fds):
            if fd in self.held:
                os.ftruncate(fd, 0)
                os.pwrite(fd, json.dumps({**note, 'slot': index + 1, 'slots': len(self.held),
                                         'pid': os.getpid(),
                                         'since': time.strftime('%Y-%m-%dT%H:%M:%S%z')}).encode(), 0)

    def release(self) -> None:
        try:
            for fd in self.held:
                os.ftruncate(fd, 0)
        finally:
            for fd in self.fds:
                os.close(fd)
            self.fds, self.selected, self.held = [], None, []


def run_holds_slot(root: Root, run: str) -> bool:
    """An older tool may not hold the new result-directory guard yet."""
    for index in range(SLOT_COUNT):
        name = SLOT if index == 0 else f'slot-{index + 1}.lock'
        try:
            fd = os.open(name, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=root.fd)
        except FileNotFoundError:
            continue
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise Refused(f'{root.path / name}: not this user\'s regular slot file')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                try:
                    data = json.loads(os.pread(fd, 4096, 0))
                except ValueError:
                    continue
                if isinstance(data, dict) and data.get('verb') == 'run' and data.get('run') == run:
                    return True
        finally:
            os.close(fd)
    return False


class RunBusy(Refused):
    """This exact run is still using or retiring its own control directory."""


class RunGuard:
    """A stable existing result-directory lock; no new file or cleanup authority."""

    def __init__(self, root: Root, run: str, *, create=False):
        self.run = run_name(run)
        self.root, self.owner = root, create
        self.fd = root.child(RESULTS, self.run, create=create)

    def acquire(self) -> None:
        if not self.owner and run_holds_slot(self.root, self.run):
            raise RunBusy(f'run {self.run}: its run slot is still held; left alone')
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RunBusy(f'run {self.run}: active or being retired; left alone') from None

    def release(self) -> None:
        os.close(self.fd)


def run_active(root: Root, run: str) -> bool:
    try:
        guard = RunGuard(root, run)
    except FileNotFoundError:
        return False
    try:
        try:
            guard.acquire()
        except RunBusy:
            return True
        return False
    finally:
        guard.release()


@contextmanager
def all_slots(root: Root, wait: float = 0.0):
    """A nonsharing live-proof part holds all four slots, and releases them on leaving this context."""
    pool = Slot(root)
    deadline = time.monotonic() + max(wait, 0.0)
    try:
        while True:
            acquired = []
            for fd in pool.fds:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired.append(fd)
                except BlockingIOError:
                    break
            if len(acquired) == SLOT_COUNT:
                pool.held = acquired
                for fd in acquired:
                    os.ftruncate(fd, 0)
                    os.pwrite(fd, json.dumps({'verb': 'live-proof', 'all_slots': True, 'pid': os.getpid()}).encode(), 0)
                yield
                return
            # No partial reservation is held while waiting for peers to finish.
            for fd in acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
            if time.monotonic() >= deadline:
                raise Refused(f'all four test slots are needed for this proof part; {pool.holder()}')
            time.sleep(0.5)
    finally:
        pool.release()
