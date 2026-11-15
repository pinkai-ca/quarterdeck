"""Per-run records of session containers, never an inventory of the shared socket anchor.

Only recorded containers and creator-recorded private servers are retired here. Unrecorded or
unfamiliar endpoints are retained, never adopted or recursively erased.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat

from . import owned, servers, socket_roots
from .layout import RESULTS, ROOT_NAME, RUN_SOCKETS, SOCKETS, Root, Refused, identity, read_json, run_name, write_json

RECORD_ENV = 'TESTRUN_SESSION_RECORDS'


def registry_project(registry: str, run: str) -> Path:
    where = Path(registry)
    if not where.is_absolute() or len(where.parents) < 4:
        raise Refused('session registry does not name an absolute project result')
    if where.name != 'sessions' or where.parent.name != run or where.parent.parent.name != RESULTS:
        raise Refused('session registry does not name this run')
    if where.parents[2].name != ROOT_NAME:
        raise Refused('session registry is not under the project results')
    return where.parents[3]


def automatic(parent: Path) -> bool:
    """Only the tool's outer run anchor auto-registers, not a nested child pytest."""
    registry = os.environ.get(RECORD_ENV)
    if not registry:
        return False
    project = registry_project(registry, os.environ.get('TESTRUNS_RUN', ''))
    return parent in (project / ROOT_NAME / RUN_SOCKETS, socket_roots.path(project) / RUN_SOCKETS)


def anchor_path(root: Root, record: dict) -> Path:
    name = anchor_name(record)
    layout = record.get('socket_layout')
    if layout is None:
        return root.path / name
    if layout != socket_roots.LAYOUT:
        raise Refused('unrecognized recorded socket layout; retained')
    return socket_roots.path(root.project) / name


def open_anchor(root: Root, record: dict) -> int:
    name = anchor_name(record)
    where = anchor_path(root, record)
    if record.get('socket_layout') is None:
        return root.child(name, create=False)
    fd = socket_roots.open_anchor(root.project, name)
    if not record.get('anchor_identity') or identity(os.fstat(fd)) != record['anchor_identity']:
        os.close(fd)
        raise Refused(f'{where}: recorded socket anchor identity changed; retained')
    return fd


def retired_anchor(root: Root, parent: Path) -> int:
    """Verify absence after completed retirement, deriving both supported layouts."""
    if parent in (root.path / SOCKETS, root.path / RUN_SOCKETS):
        return root.child(parent.name, create=False)
    if parent in (socket_roots.path(root.project) / SOCKETS, socket_roots.path(root.project) / RUN_SOCKETS):
        return socket_roots.open_anchor(root.project, parent.name)
    raise Refused(f'{parent}: not a recorded session anchor for this project; held')


def register(parent: Path, folder: Path, descriptor: int, *, registry=None, run=None) -> str | None:
    registry = os.environ.get(RECORD_ENV) if registry is None else registry
    if not registry:
        return None
    run = run_name(os.environ.get('TESTRUNS_RUN', '') if run is None else run)
    project = registry_project(registry, run)
    short = parent in (socket_roots.path(project) / SOCKETS, socket_roots.path(project) / RUN_SOCKETS)
    if not short and parent not in (project / ROOT_NAME / SOCKETS, project / ROOT_NAME / RUN_SOCKETS):
        return None  # a child pytest owns its own nested container locally
    root = Root(project, create=False)
    try:
        extra = {}
        if short:
            anchor = socket_roots.open_anchor(project, parent.name)
            try:
                extra = {'socket_layout': socket_roots.LAYOUT, 'anchor_identity': identity(os.fstat(anchor))}
                if identity(os.lstat(parent)) != extra['anchor_identity']:
                    raise Refused('session socket anchor changed during registration')
            finally:
                os.close(anchor)
        fd = root.child(RESULTS, run, 'sessions', create=False)
        try:
            token = owned.mark(descriptor)
            try:
                write_json(fd, folder.name + '.json', {'name': folder.name, 'anchor': parent.name,
                                                     'identity': identity(os.fstat(descriptor)),
                                                     'owner': token, **extra})
            except BaseException as error:
                try:
                    unmark_empty(descriptor, token)
                except OSError as cleanup_error:
                    error.add_note(f'unrecorded session container retained: {folder}: {cleanup_error}')
                raise
            return token
        finally:
            os.close(fd)
    finally:
        root.close()


def unmark_empty(descriptor: int, token: str) -> None:
    """Release the fixture's marker only when nothing else remains in its own container."""
    with os.scandir(descriptor) as entries:
        if any(item.name != owned.MARKER for item in entries):
            raise OSError('recorded session container is not empty; owner marker retained')
    marker = os.open(owned.MARKER, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
    try:
        if not stat.S_ISREG(os.fstat(marker).st_mode):
            raise OSError('session owner marker is not a regular file')
        if os.read(marker, 64).decode() != token:
            raise OSError('session owner marker changed')
    finally:
        os.close(marker)
    os.unlink(owned.MARKER, dir_fd=descriptor)


def anchor_name(record: dict) -> str:
    """Old records predate the separate tool-run anchor."""
    name = record.get('anchor', SOCKETS)
    if name not in (SOCKETS, RUN_SOCKETS):
        raise Refused('unrecognized recorded session anchor; retained')
    return name


def retire(root: Root, run: str, *, budget=None) -> dict:
    """Use this run's records only; even reporting never walks a foreign session folder."""
    entries = []
    budget = budget or owned.Budget()
    try:
        records = root.child(RESULTS, run, 'sessions', create=False)
    except FileNotFoundError:
        return {'outcome': 'unrecorded', 'scope': 'recorded session folders only', 'entries': []}
    anchor = None
    try:
        with os.scandir(records) as iterator:
            filenames = []
            for item in iterator:
                budget.check(root.path / RESULTS / run / 'sessions' / item.name)
                filenames.append(item.name)
        for filename in filenames:
            budget.check(root.path / RESULTS / run / 'sessions' / filename)
            record = read_json(records, filename, root.path / RESULTS / run / 'sessions' / filename)
            if not record:
                raise Refused(f'session record {filename} disappeared; remainder retained')
            name = record.get('name')
            if not isinstance(name, str) or name in ('', '.', '..') or '/' in name or filename != name + '.json':
                raise Refused(f'invalid session record {filename}; nothing inferred')
            where = anchor_path(root, record) / name
            if not record.get('identity') or not record.get('owner'):
                raise Refused(f'{where}: session identity/owner unrecorded; left alone')
            if anchor is not None:
                os.close(anchor)
                anchor = None
            try:
                anchor = open_anchor(root, record)
            except FileNotFoundError:
                entries.append({'path': str(where), 'outcome': 'complete'})
                continue
            descriptor = None
            try:
                try:
                    os.stat(name, dir_fd=anchor, follow_symlinks=False)
                except FileNotFoundError:
                    entries.append({'path': str(where), 'outcome': 'complete'})
                    continue
                descriptor = owned._open_exact(anchor, name, record['identity'], where, record['owner'])
                if os.fstat(descriptor).st_dev != os.fstat(anchor).st_dev:
                    raise Refused(f'{where}: session filesystem differs; retained')
                births = servers.legacy(descriptor, where, record, run, budget)
                if births != record.get('servers', {}):
                    record['servers'] = births
                    write_json(records, filename, record)  # persist every legacy proof before sending a signal
                servers.retire(descriptor, where, record, budget)
                # The native tmux subdirectory is removable only when empty, never by a recursive walk.
                native = f'tmux-{os.getuid()}'
                try:
                    os.stat(native, dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    has_native = False
                else:
                    has_native = True
                if has_native:
                    child = owned._open_exact(descriptor, native, None, where / native)
                    try:
                        if os.fstat(child).st_dev != os.fstat(anchor).st_dev:
                            raise Refused(f'{where / native}: native filesystem differs; retained')
                        with os.scandir(child) as children:
                            if next(children, None) is not None:
                                raise Refused(f'{where / native}: nonempty native namespace retained')
                        original = identity(os.fstat(child))
                        if identity(os.stat(native, dir_fd=descriptor, follow_symlinks=False)) != original:
                            raise Refused(f'{where / native}: native namespace replaced; retained')
                        os.rmdir(native, dir_fd=descriptor)
                    finally:
                        os.close(child)
                unmark_empty(descriptor, record['owner'])
                try:
                    if identity(os.stat(name, dir_fd=anchor, follow_symlinks=False)) != record['identity']:
                        raise Refused(f'{where}: session replaced during retirement; replacement retained')
                    os.rmdir(name, dir_fd=anchor)
                except (OSError, Refused):
                    marker = os.open(owned.MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                     0o600, dir_fd=descriptor)
                    try:
                        os.write(marker, record['owner'].encode())
                    finally:
                        os.close(marker)
                    raise
                entries.append({'path': str(where), 'outcome': 'complete'})
            except (OSError, Refused) as error:
                entries.append({'path': str(where), 'outcome': 'held', 'reason': str(error)})
            finally:
                if descriptor is not None:
                    os.close(descriptor)
    except (OSError, Refused) as error:
        entries.append({'path': str(root.path / RESULTS / run / 'sessions'), 'outcome': 'held', 'reason': str(error)})
    finally:
        os.close(records)
        if anchor is not None:
            os.close(anchor)
    return {'outcome': 'held' if any(item['outcome'] == 'held' for item in entries) else 'complete',
            'scope': 'recorded session folders only', 'entries': entries}


def discard_records(root: Root, run: str, budget) -> None:
    """Remove completed session metadata, after independently checking each recorded path is absent."""
    results = root.child(RESULTS, run, create=False)
    records = anchor = None
    try:
        try:
            records = root.child(RESULTS, run, 'sessions', create=False)
        except FileNotFoundError:
            return
        original = identity(os.fstat(records))
        with os.scandir(records) as iterator:
            names = []
            for item in iterator:
                budget.check(root.path / RESULTS / run / 'sessions' / item.name)
                names.append(item.name)
        for filename in names:
            budget.check(root.path / RESULTS / run / 'sessions' / filename)
            record = read_json(records, filename, root.path / RESULTS / run / 'sessions' / filename)
            name = record.get('name') if record else None
            if not isinstance(name, str) or name in ('', '.', '..') or '/' in name or filename != name + '.json':
                raise Refused(f'invalid session record {filename}; metadata retained')
            if not record.get('identity') or not record.get('owner'):
                raise Refused(f'{filename}: session identity/owner unrecorded; metadata retained')
            if anchor is not None:
                os.close(anchor)
                anchor = None
            try:
                anchor = open_anchor(root, record)
            except FileNotFoundError:
                pass
            if anchor is not None:
                try:
                    os.stat(name, dir_fd=anchor, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise Refused(f'{anchor_path(root, record) / name}: session still exists; metadata retained')
            owned.unlink_file(records, filename, root.path / RESULTS / run / 'sessions' / filename)
        if identity(os.stat('sessions', dir_fd=results, follow_symlinks=False)) != original:
            raise Refused('session metadata directory replaced; replacement retained')
        os.rmdir('sessions', dir_fd=results)
    finally:
        for fd in (anchor, records, results):
            if fd is not None:
                os.close(fd)
