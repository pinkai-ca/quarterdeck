"""Per-run records of session containers, never an inventory of the shared socket anchor.

Only recorded containers and creator-recorded private servers are retired here. Unrecorded or
unfamiliar endpoints are retained, never adopted or recursively erased.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat

from . import owned, servers
from .layout import RESULTS, ROOT_NAME, RUN_SOCKETS, SOCKETS, Root, Refused, identity, read_json, run_name, write_json

RECORD_ENV = 'TESTRUN_SESSION_RECORDS'


def automatic(parent: Path) -> bool:
    """Only the tool's own outer fixture auto-registers, not a child pytest."""
    registry = os.environ.get(RECORD_ENV)
    return bool(registry and parent.name == RUN_SOCKETS and parent.parent.name == ROOT_NAME
                and Path(registry) == parent.parent / RESULTS / os.environ.get('TESTRUNS_RUN', '') / 'sessions')


def register(parent: Path, folder: Path, descriptor: int, *, registry=None, run=None) -> str | None:
    registry = os.environ.get(RECORD_ENV) if registry is None else registry
    if not registry:
        return None  # ordinary direct pytest owns its container through its fixture, as before
    run = run_name(os.environ.get('TESTRUNS_RUN', '') if run is None else run)
    # A child pytest may use its own project or nest below an outer fixture. It
    # owns that container locally; it must not register it as a direct child of
    # this run's anchor. A nonempty outer container still holds retirement.
    if parent.name not in (SOCKETS, RUN_SOCKETS) or parent.parent.name != ROOT_NAME:
        return None
    if Path(registry) != parent.parent / RESULTS / run / 'sessions':
        return None
    root = Root(parent.parent.parent, create=False)
    try:
        if parent != root.path / parent.name or Path(registry) != root.path / RESULTS / run / 'sessions':
            raise Refused('session registry does not match this run and project socket anchor')
        fd = root.child(RESULTS, run, 'sessions', create=False)
        try:
            token = owned.mark(descriptor)
            try:
                write_json(fd, folder.name + '.json', {'name': folder.name, 'anchor': parent.name,
                                                     'identity': identity(os.fstat(descriptor)),
                                                     'owner': token})
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
    anchor = current_anchor = None
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
            selected_anchor = anchor_name(record)
            where = root.path / selected_anchor / name
            if not record.get('identity') or not record.get('owner'):
                raise Refused(f'{where}: session identity/owner unrecorded; left alone')
            if current_anchor != selected_anchor:
                if anchor is not None:
                    os.close(anchor)
                anchor, current_anchor = None, selected_anchor
            if anchor is None:
                try:
                    anchor = root.child(selected_anchor, create=False)
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
                if os.fstat(descriptor).st_dev != root.device:
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
                        if os.fstat(child).st_dev != root.device:
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
        current_anchor = None
        for filename in names:
            budget.check(root.path / RESULTS / run / 'sessions' / filename)
            record = read_json(records, filename, root.path / RESULTS / run / 'sessions' / filename)
            name = record.get('name') if record else None
            if not isinstance(name, str) or name in ('', '.', '..') or '/' in name or filename != name + '.json':
                raise Refused(f'invalid session record {filename}; metadata retained')
            if not record.get('identity') or not record.get('owner'):
                raise Refused(f'{filename}: session identity/owner unrecorded; metadata retained')
            selected_anchor = anchor_name(record)
            if current_anchor != selected_anchor:
                if anchor is not None:
                    os.close(anchor)
                anchor, current_anchor = None, selected_anchor
                try:
                    anchor = root.child(selected_anchor, create=False)
                except FileNotFoundError:
                    pass
            if anchor is not None:
                try:
                    os.stat(name, dir_fd=anchor, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise Refused(f'{root.path / selected_anchor / name}: session still exists; metadata retained')
            owned.unlink_file(records, filename, root.path / RESULTS / run / 'sessions' / filename)
        if identity(os.stat('sessions', dir_fd=results, follow_symlinks=False)) != original:
            raise Refused('session metadata directory replaced; replacement retained')
        os.rmdir('sessions', dir_fd=results)
    finally:
        for fd in (anchor, records, results):
            if fd is not None:
                os.close(fd)
