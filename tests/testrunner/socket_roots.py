"""Fixed-length socket roots; results and creator records stay in the project.

The canonical Git common project is hashed for the name and stored in full inside
its own root. Separate direct/run anchors hold the existing random containers.
Only recorded containers are retired; the small shared root is never swept.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import stat

from .layout import DIRECTORY, RUN_SOCKETS, SOCKETS, Refused, identity, open_child, read_json, write_json

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
