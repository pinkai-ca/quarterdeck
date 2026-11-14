"""Creator evidence and serialization for private tmux endpoints, never transport authority."""
from contextlib import contextmanager
from dataclasses import dataclass
import errno
import fcntl
import os
from pathlib import Path
import secrets
import signal
import socket
import stat
import tempfile
import time

from .procstat import EXITED

LOCK = '.qd-tmux-lock'
HEADER = b'quarterdeck private tmux namespace 1\n'
OWNER = 'QD_PRIVATE_TMUX_OWNER'
# How long an exclusive operation waits for the namespace lock. A holder holds it for one tmux client
# command, which a loaded machine can take well over a quarter second to run; a holder still there
# after this is stuck or suspended, and the operation refuses with ownership uncertainty.
EXCLUSIVE_WAIT = 2.0


class OwnershipError(Exception):
    pass


class ShutdownPending(OwnershipError):
    pass


def identity(info):
    return info.st_dev, info.st_ino


@dataclass
class Locked:
    folder: Path
    directory: int
    lock: int

    def verify(self):
        if (identity(self.folder.lstat()) != identity(os.fstat(self.directory))
                or identity(os.stat(LOCK, dir_fd=self.directory, follow_symlinks=False)) != identity(os.fstat(self.lock))):
            raise OwnershipError('private namespace or lock changed; retained')


@contextmanager
def locked(path, namespace=None, *, exclusive=True):
    """Observers open only an existing lock. A waiter must revalidate after acquisition."""
    directory = os.open(Path(path).parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    lock = None
    try:
        if namespace is not None and identity(os.fstat(directory)) != identity(os.fstat(namespace.directory)):
            raise OwnershipError('private namespace replaced; retained')
        candidate = os.stat(LOCK, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISREG(candidate.st_mode):
            raise OwnershipError('private namespace lock is unfamiliar; retained')
        lock = os.open(LOCK, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
        info = os.fstat(lock)
        if (not stat.S_ISREG(info.st_mode) or identity(candidate) != identity(info)
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
            raise OwnershipError('private namespace lock is unfamiliar; retained')
        deadline = time.monotonic() + (EXCLUSIVE_WAIT if exclusive else 0)
        operation = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
        attempted = False
        while True:
            if attempted and time.monotonic() >= deadline:
                raise OwnershipError('private namespace busy; ownership uncertain, retained')
            attempted = True
            try:
                fcntl.flock(lock, operation)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise OwnershipError('private namespace busy; ownership uncertain, retained') from None
                time.sleep(min(.01, remaining))
        held = Locked(Path(path).parent, directory, lock)
        held.verify()
        if os.read(lock, len(HEADER) + 1) != HEADER:
            raise OwnershipError('private namespace lock header is unfamiliar; retained')
        if namespace is not None and identity(info) != namespace.lock_identity:
            raise OwnershipError('creator namespace lock replaced; retained')
        yield held
    finally:
        if lock is not None:
            os.close(lock)  # releases the lock even through failure/interruption
        os.close(directory)


def private_endpoint(path) -> bool:
    """A path inside a creator's private namespace: an exact, ownership-checked scope for tests and the drill.

    Read only: the folder is this user's, mode 700 and no link; its lock is this user's regular mode-600 file
    with the namespace header; the endpoint's name is the creator's or a delegated peer's; anything already
    at it is this user's socket.
    """
    path = Path(path)
    if not path.is_absolute() or path.name != 's' and not (path.name.startswith('s-') and len(path.name) > 2 and all(
            c in '-abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789' for c in path.name[2:])):
        return False
    try:
        folder = path.parent.lstat()
        if (not stat.S_ISDIR(folder.st_mode) or folder.st_uid != os.getuid()
                or stat.S_IMODE(folder.st_mode) != 0o700):
            return False
        descriptor = os.open(path.parent / LOCK, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600
                    or os.read(descriptor, len(HEADER) + 1) != HEADER):
                return False
        finally:
            os.close(descriptor)
        try:
            entry = path.lstat()
        except FileNotFoundError:
            return True
        return stat.S_ISSOCK(entry.st_mode) and entry.st_uid == os.getuid()
    except OSError:
        return False


class Namespace:
    def __init__(self, fallback):
        parent = Path(os.environ.get('TMUX_TMPDIR') or fallback).expanduser().resolve()
        parent.mkdir(parents=True, exist_ok=True)
        self.folder = Path(tempfile.mkdtemp(prefix='qd-pt-', dir=parent))
        if len(os.fsencode(self.folder / 's-replacement')) >= 108:
            self.folder.rmdir()
            raise OwnershipError('private tmux path too long; set TMUX_TMPDIR to a shorter project-owned directory')
        self.directory = None
        lock = None
        try:
            self.directory = os.open(self.folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            lock = os.open(LOCK, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=self.directory)
            if os.write(lock, HEADER) != len(HEADER):
                raise OwnershipError('incomplete private namespace header')
            self.lock_identity = identity(os.fstat(lock))
        except BaseException as error:
            if self.directory is not None:
                os.close(self.directory)
            detail = f'new private namespace retained at {self.folder}'
            if isinstance(error, (OSError, OwnershipError)):
                raise OwnershipError(f'{error}; {detail}') from error
            error.add_note(detail)
            raise
        finally:
            if lock is not None:
                os.close(lock)
        self.closed = False

    def remove(self, held):
        held.verify()
        if os.listdir(held.directory) != [LOCK]:
            raise OwnershipError(f'private namespace has peer or unfamiliar entries; retained at {self.folder}')
        os.unlink(LOCK, dir_fd=held.directory)
        self.folder.rmdir()  # never recursively removes an endpoint or peer
        os.close(self.directory)
        self.closed = True


def process_state(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    except (FileNotFoundError, ProcessLookupError):
        return []  # gone, or reaped between the open and the read (ESRCH): either way it has ended
    if len(fields) <= 19 or not fields[19].isdigit():
        raise OwnershipError('private server process identity unreadable; retained')
    return fields


class OwnedSocket:
    def __init__(self, namespace, suffix='', root_owner=False):
        if any(c not in '-abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789' for c in suffix):
            raise OwnershipError('invalid private endpoint suffix')
        self.namespace, self.name, self.root_owner = namespace, 's' + suffix, root_owner
        self.path = namespace.folder / self.name
        if len(os.fsencode(self.path)) >= 108:
            raise OwnershipError('private endpoint path too long, including suffix')
        self.nonce = secrets.token_hex(16)
        self.socket_fd = self.pid = self.start = None

    def current(self, held):
        try:
            info = os.stat(self.name, dir_fd=held.directory, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if self.socket_fd is None or identity(info) != identity(os.fstat(self.socket_fd)):
            raise OwnershipError('private socket replaced or not created by this owner; retained')
        return True

    def ended(self):
        if self.pid is None:
            return self.socket_fd is None
        fields = process_state(self.pid)
        return not fields or fields[0] in EXITED or fields[19] != self.start

    def record(self, held, query):
        descriptor = os.open(self.name, os.O_PATH | os.O_NOFOLLOW, dir_fd=held.directory)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise OwnershipError('private endpoint is not an owned socket; retained')
            pid, environment = query().splitlines()
            if environment != f'{OWNER}={self.nonce}':
                raise OwnershipError('private server birth identity unknown; retained')
            fields = process_state(int(pid))
            if not fields or fields[0] in EXITED:
                raise OwnershipError('private server ended before ownership was recorded; retained')
            held.verify()
            if identity(os.stat(self.name, dir_fd=held.directory, follow_symlinks=False)) != identity(info):
                raise OwnershipError('private socket changed during ownership read; retained')
            self.socket_fd, self.pid, self.start = descriptor, int(pid), fields[19]
            descriptor = None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def terminate(self):
        """Ask this creator's own recorded server to end, once, by its pid: only when that pid is still the process
        this owner recorded (same start) and its environment still carries this owner's nonce. tmux ends its panes on
        SIGTERM as on kill-server; no stronger signal is sent, so no pane's program is left behind."""
        if self.pid is None:
            return False
        fields = process_state(self.pid)
        if not fields or fields[0] in EXITED or fields[19] != self.start:
            return False
        try:
            environment = Path(f'/proc/{self.pid}/environ').read_bytes().split(b'\0')
        except OSError:
            return False
        if f'{OWNER}={self.nonce}'.encode() not in environment or process_state(self.pid)[19:20] != [self.start]:
            return False
        try:
            os.kill(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            return False
        return True

    def remove_dead(self, held):
        present = self.current(held)
        if not self.ended():
            raise ShutdownPending('private server still running; socket retained')
        if present:
            with socket.socket(socket.AF_UNIX) as probe:
                probe.settimeout(.1)
                code = probe.connect_ex(str(self.path))
            if code != errno.ECONNREFUSED:
                raise ShutdownPending(f'private endpoint live or connection state uncertain ({code}); retained')
            held.verify()
            self.current(held)
            os.unlink(self.name, dir_fd=held.directory)

    def reset(self):
        if self.socket_fd is not None:
            os.close(self.socket_fd)
        self.socket_fd = self.pid = self.start = None
        self.nonce = secrets.token_hex(16)
