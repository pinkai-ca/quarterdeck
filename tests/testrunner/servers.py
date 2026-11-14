"""Durable birth records and bounded shutdown of a run's recorded private tmux servers.

This is test-tool infrastructure. It never discovers servers outside an identity/token-proved
session container, and a container alone grants no authority over an unrecorded endpoint.
"""
from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path
import socket
import stat
import subprocess
import time

from . import owned, procs
from .layout import DIRECTORY, RESULTS, Root, Refused, identity, read_json, write_json

LOCK = '.qd-tmux-lock'
HEADER = b'quarterdeck private tmux namespace 1\n'
OWNER = 'QD_PRIVATE_TMUX_OWNER'


def legacy(container, where, data, run, budget):
    """Measure only this proved container; legacy authority also requires the server's run environment."""
    births = dict(data.get('servers', {}))

    def visit(descriptor, path, parts, directories):
        budget.check(path)
        entries = os.listdir(descriptor)
        if LOCK in entries:
            lock = os.open(LOCK, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
            try:
                info = os.fstat(lock)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o600 or os.read(lock, len(HEADER) + 1) != HEADER):
                    raise Refused(f'{path}: legacy namespace header unproved; held')
                lock_identity = identity(info)
            finally:
                os.close(lock)
            for name in entries:
                budget.check(path / name)
                entry = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if not stat.S_ISSOCK(entry.st_mode) or '/'.join((*parts, name)) in births:
                    continue
                command = ['tmux', '-S', str(path / name), '-N', '-f', '/dev/null',
                           'display-message', '-p', '#{pid} #{' + OWNER + '}']
                env = {k: v for k, v in os.environ.items() if k not in ('TMUX', 'TMUX_PANE')}
                try:
                    found = subprocess.run(command, env=env, capture_output=True, text=True, timeout=2)
                    pid_text, nonce = found.stdout.strip().split()
                    pid = int(pid_text)
                    fields = procs.stat_fields(pid)
                    environment = Path(f'/proc/{pid}/environ').read_bytes().split(b'\0')
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    raise Refused(f'{path / name}: legacy server process/run ownership unproved; held') from None
                if (found.returncode or not fields or fields[0] == 'Z'
                        or f'TESTRUNS_RUN={run}'.encode() not in environment
                        or f'{OWNER}={nonce}'.encode() not in environment
                        or identity(os.stat(name, dir_fd=descriptor, follow_symlinks=False)) != identity(entry)):
                    raise Refused(f'{path / name}: legacy server process/run ownership unproved; held')
                births['/'.join((*parts, name))] = {
                    'parts': [*parts, name], 'directories': directories, 'socket': identity(entry),
                    'lock': lock_identity, 'pid': pid, 'start': fields[19], 'boot': procs.boot_id(), 'owner': nonce}
        for name in entries:
            budget.check(path / name)
            entry = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(entry.st_mode):
                child = owned._open_exact(descriptor, name, identity(entry), path / name)
                try:
                    if entry.st_dev != os.fstat(container).st_dev:
                        raise Refused(f'{path / name}: legacy namespace filesystem differs; held')
                    visit(child, path / name, [*parts, name], [*directories, identity(entry)])
                finally:
                    os.close(child)

    visit(container, where, [], [])
    return births


def record(server) -> None:
    """Called after the creator has verified a server's birth, while its namespace lock is held."""
    registry = os.environ.get('TESTRUN_SESSION_RECORDS')
    if not registry:
        return
    registry = Path(registry)
    run = os.environ.get('TESTRUNS_RUN', '')
    if registry.name != 'sessions' or registry.parent.name != run or registry.parent.parent.name != RESULTS:
        raise Refused('server registry does not name this run')
    project = registry.parents[3]
    root = Root(project, create=False)
    records = container = None
    try:
        records = root.child(RESULTS, run, 'sessions', create=False)
        # The server must be below one of this run's recorded session containers.
        path = Path(server.path)
        with os.scandir(records) as files:
            filenames = [entry.name for entry in files]
        for filename in filenames:
            data = read_json(records, filename, registry / filename)
            if not data:
                continue
            from .sessions import anchor_name
            folder = root.path / anchor_name(data) / data['name']
            if not path.is_relative_to(folder):
                continue
            parts = path.relative_to(folder).parts
            if len(parts) < 2:
                raise Refused('private server has no recorded namespace')
            anchor = root.child(anchor_name(data), create=False)
            try:
                container = owned._open_exact(anchor, data['name'], data['identity'], folder, data['owner'])
            finally:
                os.close(anchor)
            directories = []
            descriptor = os.dup(container)
            try:
                where = folder
                for name in parts[:-1]:
                    where /= name
                    child = owned._open_exact(descriptor, name, None, where)
                    os.close(descriptor)
                    descriptor = child
                    directories.append(identity(os.fstat(descriptor)))
                lock = os.stat(LOCK, dir_fd=descriptor, follow_symlinks=False)
                endpoint = os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
                if identity(endpoint) != identity(os.fstat(server.socket_fd)):
                    raise Refused('server changed before its durable birth record')
                birth = {'parts': list(parts), 'directories': directories, 'socket': identity(endpoint),
                         'lock': identity(lock), 'pid': server.pid, 'start': server.start,
                         'boot': procs.boot_id(), 'owner': server.nonce}
            finally:
                os.close(descriptor)
            # Parallel server births in the same pytest process preserve one another's records.
            fcntl.flock(records, fcntl.LOCK_EX)
            try:
                current = read_json(records, filename, registry / filename)
                if not current or current.get('owner') != data['owner'] or current.get('identity') != data['identity']:
                    raise Refused('session record changed while recording server birth')
                current.setdefault('servers', {})['/'.join(parts)] = birth
                write_json(records, filename, current)
            finally:
                fcntl.flock(records, fcntl.LOCK_UN)
            return
        # Nested child pytest owns its container locally and has no outer run authority.
    finally:
        for descriptor in (container, records):
            if descriptor is not None:
                os.close(descriptor)
        root.close()


def ended(birth):
    if birth.get('boot') != procs.boot_id():
        return True
    fields = procs.stat_fields(birth['pid'])
    if fields is None:
        if Path(f"/proc/{birth['pid']}").exists():
            raise Refused('recorded private server process is unreadable; held')
        return True
    return fields[0] == 'Z' or fields[19] != birth['start']


def endpoint(descriptor, name, birth):
    try:
        info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not stat.S_ISSOCK(info.st_mode) or identity(info) != birth['socket']:
        raise Refused('recorded private endpoint changed; held')
    return True


def stop(descriptor, path, birth, budget):
    """Hold the creator's exact lock, verify birth, stop only -S path, then unlink a proved dead socket."""
    name = path.name
    def verify():
        if (identity(path.parent.lstat()) != identity(os.fstat(descriptor))
                or identity(os.stat(LOCK, dir_fd=descriptor, follow_symlinks=False)) != birth['lock']):
            raise Refused('private namespace or lock replaced; held')

    lock = os.open(LOCK, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
    try:
        info = os.fstat(lock)
        if (not stat.S_ISREG(info.st_mode) or identity(info) != birth['lock']
                or stat.S_IMODE(info.st_mode) != 0o600 or os.read(lock, len(HEADER) + 1) != HEADER):
            raise Refused('recorded private namespace lock changed; held')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused('private namespace lock is busy; held') from None
        verify()
        present = endpoint(descriptor, name, birth)
        if not ended(birth):
            if not present:
                raise Refused('recorded private server still live without its endpoint; held')
            env = dict(os.environ)
            env.pop('TMUX', None)
            env.pop('TMUX_PANE', None)
            command = ['tmux', '-S', str(path), '-N', '-f', '/dev/null']
            try:
                query = subprocess.run([*command, 'display-message', '-p', '#{pid} #{' + OWNER + '}'],
                                       env=env, capture_output=True, text=True, timeout=2)
            except subprocess.TimeoutExpired:
                raise Refused('private server did not answer; held') from None
            if query is not None and not query.returncode and query.stdout.strip() == f"{birth['pid']} {birth['owner']}":
                verify()
                endpoint(descriptor, name, birth)
                guard = '#{&&:#{==:#{pid},%s},#{==:#{%s},%s}}' % (birth['pid'], OWNER, birth['owner'])
                try:
                    subprocess.run([*command, 'if-shell', '-F', guard, 'kill-server'], env=env,
                                   capture_output=True, timeout=2, check=False)
                except subprocess.TimeoutExpired:
                    raise Refused('private server did not answer; held') from None
            # A server already shutting down may have stopped answering. Send nothing further;
            # wait for the same recorded process and endpoint to be proved dead, or hold.
        deadline = time.monotonic() + 2
        while True:
            budget.check(path)
            gone = ended(birth)
            present = endpoint(descriptor, name, birth)
            code = errno.ECONNREFUSED
            if present:
                with socket.socket(socket.AF_UNIX) as probe:
                    probe.settimeout(.1)
                    code = probe.connect_ex(str(path))
            if gone and (not present or code == errno.ECONNREFUSED):
                break
            if time.monotonic() >= deadline:
                raise Refused(f'private server or socket listener remains live or uncertain ({code}); held')
            time.sleep(.01)
        if present:
            verify()
            if endpoint(descriptor, name, birth) and ended(birth):
                os.unlink(name, dir_fd=descriptor)
    finally:
        os.close(lock)


def retire(container, where, data, budget):
    """Only explicitly recorded endpoints, then their empty recorded namespaces, deepest first."""
    namespaces = {}
    locks = {}
    for birth in data.get('servers', {}).values():
        parts = birth.get('parts', [])
        directories = birth.get('directories', [])
        if (len(parts) < 2 or len(directories) != len(parts) - 1
                or any(not isinstance(p, str) or p in ('', '.', '..') or '/' in p for p in parts)
                or not isinstance(birth.get('pid'), int) or birth['pid'] <= 0
                or not isinstance(birth.get('owner'), str) or len(birth['owner']) != 32
                or any(c not in '0123456789abcdef' for c in birth['owner'])
                or not str(birth.get('start', '')).isdigit() or not birth.get('boot')):
            raise Refused('private server birth record is incomplete; held')
        descriptor = os.dup(container)
        path = where
        try:
            for name, expected in zip(parts[:-1], directories):
                path /= name
                budget.check(path)
                try:
                    child = owned._open_exact(descriptor, name, expected, path)
                except Refused:
                    try:
                        os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    except FileNotFoundError:
                        if ended(birth):
                            break
                    raise
                os.close(descriptor)
                descriptor = child
                namespaces[tuple(path.relative_to(where).parts)] = expected
            else:
                stop(descriptor, path / parts[-1], birth, budget)
                locks[tuple(parts[:-1])] = birth['lock']
        finally:
            os.close(descriptor)
    for parts in sorted(namespaces, key=len, reverse=True):
        descriptor = os.dup(container)
        try:
            path = where
            for name in parts:
                path /= name
                child = owned._open_exact(descriptor, name, namespaces[tuple(path.relative_to(where).parts)], path)
                os.close(descriptor)
                descriptor = child
            entries = os.listdir(descriptor)
            if entries == [LOCK] and parts in locks:
                # Every endpoint was processed above. No listener/server was inferred from a filename.
                if identity(os.stat(LOCK, dir_fd=descriptor, follow_symlinks=False)) != locks[parts]:
                    raise Refused(f'{path}: recorded namespace lock changed; held')
                os.unlink(LOCK, dir_fd=descriptor)
                entries = []
            if entries:
                raise Refused(f'{path}: nonempty recorded namespace; held')
            parent = container if len(parts) == 1 else os.open(path.parent, DIRECTORY)
            try:
                if identity(os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)) != identity(os.fstat(descriptor)):
                    raise Refused(f'{path}: namespace replaced; held')
                os.rmdir(parts[-1], dir_fd=parent)
            finally:
                if parent != container:
                    os.close(parent)
        finally:
            os.close(descriptor)
