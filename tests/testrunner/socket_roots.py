"""Fixed-length socket roots; results and creator records stay in the project.

The canonical Git common project is hashed for the name and stored in full inside
its own root. Separate direct/run anchors hold the existing random containers.
Only recorded containers are retired. Empty roots can be swept after their
recorded project is gone; a live project or a nonempty anchor stays untouched.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import re

from . import FORMAT
from .layout import DIRECTORY, RUN_SOCKETS, SOCKETS, Refused, identity, open_child, read_json, write_json, write_new

SYSTEM_TMP = Path('/tmp')  # fixed, independent of inherited TMPDIR and checkout length
LAYOUT = 'short-v1'
PROJECT = 'project.json'


def path(project: Path) -> Path:
    digest = hashlib.sha256(os.fsencode(project)).hexdigest()[:12]
    return SYSTEM_TMP / f'qd-{os.getuid()}-{digest}'


@contextmanager
def opened(project: Path, *, create: bool = False):
    where = path(project)
    if SYSTEM_TMP.resolve() != SYSTEM_TMP:
        raise Refused(f'socket parent {SYSTEM_TMP} is reached through a link')
    parent = os.open(SYSTEM_TMP, DIRECTORY)
    fd = None
    try:
        fd = open_child(parent, where.name, where, create=create, device=os.fstat(parent).st_dev)
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o700:
            raise Refused(f'socket root {where} must have mode 0700')
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            record = read_json(fd, PROJECT, where / PROJECT)
            if record is None:
                if not create or os.listdir(fd):
                    raise Refused(f'socket root {where} has no project identity; left alone')
                write_json(fd, PROJECT, {'project': str(project), 'layout': LAYOUT})
                record = read_json(fd, PROJECT, where / PROJECT)
            if record.get('project') != str(project) or record.get('layout') != LAYOUT:
                raise Refused(f'socket root {where} does not belong to this project; left alone')
            metadata = os.stat(PROJECT, dir_fd=fd, follow_symlinks=False)
            if stat.S_IMODE(metadata.st_mode) != 0o600:
                raise Refused(f'socket project identity {where / PROJECT} must have mode 0600')
            if identity(os.lstat(where)) != identity(os.fstat(fd)):
                raise Refused(f'socket root {where} changed while opening; left alone')
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
        yield fd
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def open_anchor(project: Path, name: str, *, create: bool = False) -> int:
    if name not in (SOCKETS, RUN_SOCKETS):
        raise Refused(f'unknown short socket anchor {name!r}')
    with opened(project, create=create) as fd:
        where = path(project) / name
        child = open_child(fd, name, where, create=create, device=os.fstat(fd).st_dev)
        if stat.S_IMODE(os.fstat(child).st_mode) != 0o700:
            os.close(child)
            raise Refused(f'socket anchor {where} must have mode 0700')
        return child


def ensure(project: Path, name: str) -> Path:
    os.close(open_anchor(project, name, create=True))
    return path(project) / name


def enabled(top: Path) -> bool:
    """Older targets keep their old fixture/record layout when tested by this tool."""
    return (top / 'tests/testrunner/socket_roots.py').is_file()


def project_record(fd: int, where: Path) -> tuple[dict, bytes, list]:
    """Read one bounded, regular identity file without following links or waiting on a FIFO."""
    metadata = os.open(PROJECT, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
    try:
        info = os.fstat(metadata)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 65536):
            raise Refused(f'{where / PROJECT}: not a bounded private project identity; retained')
        with os.fdopen(os.dup(metadata), 'rb') as handle:
            raw = handle.read(65537)
        if len(raw) > 65536:
            raise Refused(f'{where / PROJECT}: identity exceeds its bound; retained')
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get('format') != FORMAT or data.get('layout') != LAYOUT:
            raise Refused(f'{where}: unrecognized socket root identity; retained')
        project = data.get('project')
        if not isinstance(project, str) or not Path(project).is_absolute() or path(Path(project)) != where:
            raise Refused(f'{where}: identity does not derive this root; retained')
        return data, raw, identity(info)
    finally:
        os.close(metadata)


def project_gone(project: Path) -> bool:
    try:
        project.lstat()
    except FileNotFoundError:
        return True
    return False


def remove_orphan(where: Path, *, expected_project: Path | None = None) -> str:
    """Remove only this derived empty root after proving that its own project is gone."""
    if where.parent != SYSTEM_TMP or not re.fullmatch(rf'qd-{os.getuid()}-[0-9a-f]{{12}}', where.name):
        raise Refused(f'{where}: not a derived socket root for this user; retained')
    if SYSTEM_TMP.resolve() != SYSTEM_TMP:
        raise Refused(f'socket parent {SYSTEM_TMP} is reached through a link')
    parent = os.open(SYSTEM_TMP, DIRECTORY)
    fd = None
    anchors = []
    try:
        try:
            fd = open_child(parent, where.name, where, create=False, device=os.fstat(parent).st_dev)
        except FileNotFoundError:
            return 'absent'
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o700:
            raise Refused(f'{where}: root is not private mode 0700; retained')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused(f'{where}: root is busy; retained') from None
        data, raw, metadata_identity = project_record(fd, where)
        project = Path(data['project'])
        if expected_project is not None and project != expected_project:
            raise Refused(f'{where}: recorded project differs from the explicit list; retained')
        if not project_gone(project):
            return 'project-present'
        if set(os.listdir(fd)) - {PROJECT, SOCKETS, RUN_SOCKETS}:
            raise Refused(f'{where}: root has other entries; retained')
        for name in (SOCKETS, RUN_SOCKETS):
            try:
                child = open_child(fd, name, where / name, create=False, device=os.fstat(fd).st_dev)
            except FileNotFoundError:
                continue
            anchors.append((name, child, identity(os.fstat(child))))
            if stat.S_IMODE(os.fstat(child).st_mode) != 0o700 or os.listdir(child):
                raise Refused(f'{where / name}: anchor is not private and empty; retained')
        if not project_gone(project) or project_record(fd, where)[1:] != (raw, metadata_identity):
            raise Refused(f'{where}: project or identity changed; retained')
        for name, child, expected in anchors:
            if identity(os.stat(name, dir_fd=fd, follow_symlinks=False)) != expected:
                raise Refused(f'{where / name}: anchor changed; retained')
            os.rmdir(name, dir_fd=fd)  # empty directory only; no recursive removal
        if (not project_gone(project)
                or project_record(fd, where)[1:] != (raw, metadata_identity)
                or identity(os.stat(where.name, dir_fd=parent, follow_symlinks=False)) != identity(os.fstat(fd))):
            raise Refused(f'{where}: project or root identity changed; retained')
        os.unlink(PROJECT, dir_fd=fd)
        try:
            os.rmdir(where.name, dir_fd=parent)
        except OSError:
            # Preserve the exact identity if concurrent activity made the root nonempty.
            try:
                write_new(fd, PROJECT, raw)
            except FileExistsError:
                pass
            raise
        return 'removed'
    finally:
        for _, child, _ in anchors:
            os.close(child)
        if fd is not None:
            os.close(fd)
        os.close(parent)


def sweep(roots: list[Path] | None = None, *, expected_projects: list[Path] | None = None) -> dict:
    """An explicit list, or derived names only; no recursion and no process signalling."""
    if expected_projects is not None and (roots is None or len(roots) != len(expected_projects)):
        raise Refused('each explicit socket root needs exactly one expected project')
    expected = dict(zip(roots, expected_projects)) if expected_projects is not None else {}
    if roots is None:
        if SYSTEM_TMP.resolve() != SYSTEM_TMP:
            raise Refused(f'socket parent {SYSTEM_TMP} is reached through a link')
        parent = os.open(SYSTEM_TMP, DIRECTORY)
        try:
            with os.scandir(parent) as entries:
                roots = sorted(SYSTEM_TMP / entry.name for entry in entries
                               if re.fullmatch(rf'qd-{os.getuid()}-[0-9a-f]{{12}}', entry.name))
        finally:
            os.close(parent)
    result = {'removed': [], 'absent': [], 'kept': []}
    for where in roots:
        try:
            outcome = remove_orphan(where, expected_project=expected.get(where))
        except (OSError, ValueError, Refused) as error:
            result['kept'].append({'path': str(where), 'reason': str(error)})
        else:
            if outcome == 'project-present':
                result['kept'].append({'path': str(where), 'reason': outcome})
            else:
                result[outcome].append(str(where))
    return result
