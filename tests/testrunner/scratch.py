"""Ephemeral working storage; durable ownership and test evidence stay in .testruns/results.

Linux statfs establishes the filesystem type. Names such as /tmp never establish that a folder is
in memory. The CI child deliberately models a disk-only filesystem view; the controller admits actual
memory through a separate physical observer. Each run has its own reservation, including a crashed or kept run.
"""
from __future__ import annotations

import ctypes
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import stat
import time

from .layout import DIRECTORY, RESULTS, RUNS, Refused, identity, open_child, read_json, write_json

BOUND = 4 * 1024 ** 3
MARGIN = 8 * 1024 ** 3
INTERVAL = 1.0
METADATA_ALLOWANCE = 4096  # conservative inode/dentry allowance per directory entry, including empty files
MEMORY_TYPES = {0x01021994: 'tmpfs', 0x858458f6: 'ramfs'}
RECORD = 'storage.json'
NO_CI_MEMORY = (
    'CI layout needs real memory working storage; no eligible memory reservation is free; no drive fallback'
)


def physical_filesystem(fd):
    # statfs starts with a native long f_type; an oversized aligned buffer holds the Linux structure.
    libc = ctypes.CDLL(None, use_errno=True)
    buffer = (ctypes.c_long * 64)()
    if libc.fstatfs(ctypes.c_int(fd), ctypes.byref(buffer)):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return MEMORY_TYPES.get(buffer[0] & 0xffffffff)


def filesystem(fd):
    # The CI child models a disk-only runner, including its own synthetic temporary folders.
    return None if os.environ.get(CI_DISK_ENV) == '1' else physical_filesystem(fd)


def project_name(project):
    digest = hashlib.sha256(os.fsencode(project)).hexdigest()[:16]
    return f'qd-{os.getuid()}-{digest}'


def candidates(environ=None):
    environ = os.environ if environ is None else environ
    return list(dict.fromkeys(Path(p).absolute() for p in (environ.get('TMPDIR'), '/dev/shm', '/tmp') if p))


CI_DISK_ENV = 'TESTRUNS_CI_DISK'  # the CI runner reports no eligible memory filesystem


def choices(bound=BOUND, *, physical=False):
    for path in candidates():
        try:
            if path.resolve() != path or not os.access(path, os.W_OK | os.X_OK):
                continue
            fd = os.open(path, DIRECTORY)
            try:
                kind = physical_filesystem(fd) if physical else filesystem(fd)
                space = os.fstatvfs(fd)
                if kind and not space.f_flag & getattr(os, 'ST_NOEXEC', 8) and space.f_bavail * space.f_frsize >= bound:
                    yield path, kind
            finally:
                os.close(fd)
        except (OSError, AttributeError):
            continue


def choose(bound=BOUND):
    return next(choices(bound), None)


def memory_root(project, base, *, create=True, name=None, physical=False):
    base = Path(base)
    if not base.is_absolute() or base.resolve() != base:
        raise Refused(f'{base}: memory storage base is not an absolute plain path')
    fd = os.open(base, DIRECTORY)
    try:
        kind = physical_filesystem(fd) if physical else filesystem(fd)
        if not kind:
            raise Refused(f'{base}: recorded working storage is no longer tmpfs or ramfs')
        name = project_name(project) if name is None else name
        if not isinstance(name, str) or not name or '/' in name or '\0' in name or name in ('.', '..'):
            raise Refused(f'{base}: recorded memory root name is invalid; nothing touched')
        path = base / name
        child = open_child(fd, path.name, path, create=create, device=os.fstat(fd).st_dev)
    finally:
        os.close(fd)
    return path, child


def record(root, run):
    try:
        fd = root.child(RESULTS, run, create=False)
    except FileNotFoundError:
        return None
    try:
        data = read_json(fd, RECORD, root.path / RESULTS / run / RECORD)
        if data is not None:
            valid = (data.get('run') == run and isinstance(data.get('base'), str)
                     and '\0' not in data['base'] and Path(data['base']).is_absolute()
                     and data.get('kind') in MEMORY_TYPES.values()
                     and isinstance(data.get('root'), str) and bool(data['root'])
                     and '/' not in data['root'] and '\0' not in data['root'] and data['root'] not in ('.', '..')
                     and isinstance(data.get('identity'), list) and len(data['identity']) == 3
                     and all(type(value) is int for value in data['identity']))
            if not valid:
                raise Refused(f'{root.path / RESULTS / run / RECORD}: working storage record is incomplete or differs; nothing touched')
        return data
    finally:
        os.close(fd)


def path(root, run):
    data = record(root, run)
    return (Path(data['base']) / data['root'] if data else root.path) / RUNS / run


def parent(root, run, *, create=True):
    data = record(root, run)
    if data is None:
        return root.child(RUNS, create=create)
    from . import procs
    where = Path(data['base']) / data['root']
    try:
        where, fd = memory_root(root.project, data['base'], create=False, name=data['root'])
    except FileNotFoundError:
        if not data.get('boot') or data['boot'] == procs.boot_id():
            raise Refused(f'{where}: recorded memory project root is missing in the same or unknown boot; held') from None
        raise
    try:
        if identity(os.fstat(fd)) != data['identity']:
            if not data.get('boot') or data['boot'] == procs.boot_id():
                raise Refused(f'{where}: recorded memory root identity changed; nothing touched')
            # A reboot can remove tmpfs and recreate its project root. Only absence is accepted;
            # a new run directory at the old name is never adopted or removed.
            runs = open_child(fd, RUNS, where / RUNS, create=False, device=os.fstat(fd).st_dev)
            try:
                try:
                    os.stat(run, dir_fd=runs, follow_symlinks=False)
                except FileNotFoundError:
                    return os.dup(runs)
                raise Refused(f'{where / RUNS / run}: exists in a different memory root after reboot; held')
            finally:
                os.close(runs)
        return open_child(fd, RUNS, where / RUNS, create=create, device=os.fstat(fd).st_dev)
    finally:
        os.close(fd)


def child(root, run, *, create=True):
    fd = parent(root, run, create=create)
    try:
        return open_child(fd, run, path(root, run), create=create, device=os.fstat(fd).st_dev)
    finally:
        os.close(fd)


def pending(root, *, errors=None):
    """Recorded memory runs only: no scanning or guessing ownership in a shared temporary folder."""
    fd = root.child(RESULTS)
    try:
        names = os.listdir(fd)
    finally:
        os.close(fd)
    from .layout import run_name
    found = []
    for name in names:
        try:
            run_name(name)
        except Refused:
            continue
        try:
            data = record(root, name)
        except (Refused, OSError) as error:
            message = f'{root.path / RESULTS / name / RECORD}: invalid or unreadable storage record ({error}); retained'
            if errors is not None:
                errors[name] = message
            else:
                print(f'testrun: {message}', flush=True)
            found.append(name)
        else:
            if data is not None:
                found.append(name)
    return sorted(found)


def disk_fallback(root, run, reason):
    print(f'testrun: working files on disk: {reason}', flush=True)
    return {'kind': 'disk', 'path': str(path(root, run)), 'bound_bytes': None, 'reason': reason}


@contextmanager
def reservation_lock(root):
    # A fresh open description also serializes callers sharing a Root in one process.
    fd = os.open('.', DIRECTORY, dir_fd=root.fd)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def reserved(root, device):
    """Charge each outstanding promise in full, even if some bytes are already used.

    Durable records are the authority; never infer a dead run or release its promise here.
    Full charging is conservative because statvfs also charges files already written.
    """
    total = 0
    errors = {}
    for run in pending(root, errors=errors):
        if run in errors:
            raise Refused(errors[run])
        data = record(root, run)
        if data is None:
            continue
        if data['identity'][0] == device:
            bound = data.get('bound_bytes', BOUND)  # older reservations used the fixed bound
            if type(bound) is not int or bound <= 0:
                raise Refused(f'{root.path / RESULTS / run / RECORD}: invalid reservation bound; held')
            total += bound
    return total


def eligible_memory(root, *, physical):
    """The first base ``prepare`` would reserve, or None. Creates nothing.

    ``physical`` is the controller's real tmpfs/ramfs reading. The child disk model must not
    hide a reservation the refusal itself can still take. A missing root is a fresh project:
    it holds no outstanding reservation.
    """
    available = choices(BOUND + MARGIN, physical=True) if physical else choices(BOUND + MARGIN)
    for base, kind in available:
        # Capacity fallback must not create an unused project root in this candidate.
        base_fd = os.open(base, DIRECTORY)
        try:
            space = os.fstatvfs(base_fd)
            promised = 0 if root is None else reserved(root, os.fstat(base_fd).st_dev)
            if space.f_bavail * space.f_frsize < BOUND + MARGIN + promised:
                continue
        finally:
            os.close(base_fd)
        return base, kind
    return None


def prepare(root, run, *, memory_required=False):
    with reservation_lock(root):
        if record(root, run) is not None:
            raise Refused(f'run {run}: already has a memory reservation; retained')
        # Selection, the fresh free-space check, and publishing the promise share this lock.
        selected = eligible_memory(root, physical=memory_required)
        if selected is not None:
            base, kind = selected
            where, fd = (memory_root(root.project, base, physical=True) if memory_required
                         else memory_root(root.project, base))
            try:
                from . import procs
                data = {'run': run, 'base': str(base), 'root': where.name,
                        'identity': identity(os.fstat(fd)), 'kind': kind,
                        'boot': procs.boot_id(), 'bound_bytes': BOUND}
                result = root.child(RESULTS, run)
                try:
                    write_json(result, RECORD, data)
                finally:
                    os.close(result)
            finally:
                os.close(fd)
            print(f'testrun: working files in {kind}: {where / RUNS / run} (4 GiB bound); results on disk', flush=True)
            return {**data, 'path': str(path(root, run)), 'sample_seconds': INTERVAL,
                    'metadata_allowance_per_entry': METADATA_ALLOWANCE}
    if memory_required:
        raise Refused(NO_CI_MEMORY)
    return disk_fallback(root, run, 'no writable executable tmpfs/ramfs has room for a 4 GiB reservation '
                         'plus the 8 GiB margin and outstanding reservations')


def discard(root, run):
    with reservation_lock(root):
        fd = root.child(RESULTS, run, create=False)
        try:
            if record(root, run) is not None:
                os.unlink(RECORD, dir_fd=fd)
        finally:
            os.close(fd)


def usage(path):
    """Conservative working bytes, no followed links, hard-linked inodes counted once; tolerate removals."""
    total = {'bytes': 0, 'file_bytes': 0, 'allocated_bytes': 0, 'metadata_allowance_bytes': 0, 'files': 0, 'dirs': 0}
    seen = set()
    def walk(folder, *, root=False):
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    try:
                        info = entry.stat(follow_symlinks=False)
                        total['metadata_allowance_bytes'] += METADATA_ALLOWANCE
                        if stat.S_ISDIR(info.st_mode):
                            total['dirs'] += 1
                            walk(entry.path)
                        elif stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) not in seen:
                            seen.add((info.st_dev, info.st_ino))
                            total['files'] += 1
                            total['file_bytes'] += max(info.st_size, info.st_blocks * 512)
                            total['allocated_bytes'] += info.st_blocks * 512
                    except FileNotFoundError:
                        pass
        except FileNotFoundError:
            if root:
                raise  # a missing measurement root is unknown, rather than measured zero
    try:
        walk(path, root=True)
    except (OSError, RecursionError) as error:
        raise Refused(f'cannot measure working files at {path}: {type(error).__name__}: {error}; run stopped') from None
    total['bytes'] = total['file_bytes'] + total['metadata_allowance_bytes']
    return total


class Monitor:
    def __init__(self, path, *, bound=BOUND, extra=()):
        self.path, self.bound = Path(path), bound
        self.paths = [self.path]
        for other in map(Path, extra):
            if not any(other == root or other.is_relative_to(root) for root in self.paths):
                self.paths = [root for root in self.paths if not root.is_relative_to(other)] + [other]
        self.peak = dict.fromkeys(('bytes', 'file_bytes', 'allocated_bytes', 'metadata_allowance_bytes', 'files', 'dirs'), 0)
        self.next = 0.0
        self.exceeded = False

    def check(self, *, force=False):
        if not force and time.monotonic() < self.next:
            return
        current = dict.fromkeys(self.peak, 0)
        for path in self.paths:
            for key, value in usage(path).items():
                current[key] += value
        self.peak = {key: max(value, self.peak[key]) for key, value in current.items()}
        self.next = time.monotonic() + INTERVAL
        self.exceeded = self.exceeded or current['bytes'] > self.bound
        if self.exceeded:
            raise Refused(f'working files exceeded the {self.bound} byte bound (4 GiB by default); run stopped')


def diskstats(project):
    """The project's block-device counters are machine-wide, never attributed to this run alone."""
    device = os.stat(project).st_dev
    key = (os.major(device), os.minor(device))
    try:
        for line in Path('/proc/diskstats').read_text().splitlines():
            fields = line.split()
            if tuple(map(int, fields[:2])) == key:
                return {'device': fields[2], 'sectors_written': int(fields[9])}
    except (OSError, ValueError, IndexError):
        pass
    return None


def process_io():
    try:
        return {key: int(value) for key, value in
                (line.split(':', 1) for line in Path('/proc/self/io').read_text().splitlines())}
    except (OSError, ValueError):
        return {}


def writes_since(project, before, io_before):
    after = diskstats(project)
    io_after = process_io()
    return {'device_before': before, 'device_after': after,
            'device_bytes_written': (after['sectors_written'] - before['sectors_written']) * 512
            if before and after and before['device'] == after['device'] else None,
            'device_scope': 'machine-wide device delta, includes other writers; not run-only attribution',
            'interval': 'scratch setup through retirement; final evidence write after this sample is excluded',
            'process_io_before': io_before, 'process_io_after': io_after,
            'process_write_bytes': io_after.get('write_bytes', 0) - io_before.get('write_bytes', 0),
            'process_cancelled_write_bytes': io_after.get('cancelled_write_bytes', 0) - io_before.get('cancelled_write_bytes', 0)}


def plain(project):
    try:
        return _plain(project)
    except (Refused, OSError) as error:
        print(f'pytest: working files on disk: memory storage unavailable ({error}); continue on disk', flush=True)
        return None


def _plain(project):
    current = Path(os.environ.get('TMPDIR', '/'))
    if (os.environ.get(CI_DISK_ENV) != '1'
            and current.name == 't' and current.parent.name == project_name(project)):
        where, fd = memory_root(project, current.parent.parent, create=False)
        try:
            space = os.fstatvfs(fd)
            if space.f_bavail * space.f_frsize >= BOUND:
                return where / 't'
        finally:
            os.close(fd)
    selected = choose()
    if selected is None:
        print('pytest: working files on disk: no writable executable tmpfs/ramfs has 4 GiB free', flush=True)
        return None
    base, _ = selected
    where, fd = memory_root(project, base)
    try:
        folder = open_child(fd, 't', where / 't', create=True, device=os.fstat(fd).st_dev)
        os.close(folder)
    finally:
        os.close(fd)
    return where / 't'


def in_memory(path):
    path = Path(path).absolute()
    while not path.exists() and path != path.parent:
        path = path.parent
    fd = os.open(path, DIRECTORY)
    try:
        return bool(filesystem(fd))
    finally:
        os.close(fd)


class PlainMonitor:
    """Fresh interpreter: tests' monkeypatches cannot alter the sampler's filesystem observations.

    The parent holds the stdin pipe. EOF, including a dead parent, ends the observer; it never polls
    another session, and SIGINT is addressed only to the process that created it.
    """
    def __init__(self, path, extra=()):
        import subprocess
        import sys
        root = Path(__file__).resolve().parents[2]
        code = ('import sys; sys.path.insert(0, sys.argv.pop(1)); '
                'from tests.testrunner.scratch import watch_plain; watch_plain()')
        self.process = subprocess.Popen([sys.executable, '-B', '-I', '-c', code, str(root),
                                         str(os.getpid()), str(path), *map(str, extra)],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

    def finish(self):
        import json
        output, _ = self.process.communicate(timeout=10)
        if self.process.returncode:
            raise Refused('working-file observer ended without a measurement; session cannot certify its bound')
        return json.loads(output)


def watch_plain():
    import json
    import select
    import signal
    import sys
    parent = int(sys.argv[1])
    monitor = Monitor(Path(sys.argv[2]), extra=sys.argv[3:])
    refusal = None
    while True:
        if os.getppid() != parent:
            break
        ending = bool(select.select([sys.stdin], [], [], INTERVAL)[0])
        try:
            monitor.check(force=True)
        except Refused as error:
            refusal = str(error)
            if not ending:
                try:
                    os.kill(parent, signal.SIGINT)
                except ProcessLookupError:
                    pass
            break
        if ending:
            break
    print(json.dumps({'peak': monitor.peak, 'refusal': refusal}), flush=True)
