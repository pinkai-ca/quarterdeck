"""Cascade runtime evidence: checked file reads/appends and derived operational holds."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
from contextlib import ExitStack, contextmanager
from pathlib import Path

from . import durable, post, post_dir
from .ship import ShipError

HOLD_REASONS = frozenset({'cascade evidence is malformed JSON', 'cascade evidence is inconsistent',
                          'cascade evidence is unreadable', 'cascade coordination could not finish',
                          'cascade step exclusion evidence is unreadable'})


class ExclusionUnknown(OSError):
    """An existing exclusion reading is unavailable; no acceptance decision is proved."""


def key(question: str) -> str:
    if re.fullmatch(r'que-[0-9]{8}-[0-9]{4}-[A-Za-z0-9_-]+-[0-9a-f]{4}', question):
        return question
    return 'run-' + hashlib.sha256(question.encode('utf-8')).hexdigest()[:16]


def _parent(path: Path) -> int:
    """Open the existing directory chain without following links."""
    path = path.absolute()
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for name in path.parent.parts[1:]:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _lock_path(path: Path) -> Path:
    """The exclusion's lock: the runtime is the folder above ring/ or post/, where cascade and group evidence lie."""
    from . import ship_locks
    absolute = Path(os.path.abspath(path))
    runtime = absolute.parent.parent.parent
    return ship_locks.hashed(runtime, 'cascade-files', str(absolute))


@contextmanager
def _exclusion(path: Path):
    from . import ship_locks
    try:
        with ship_locks.held(_lock_path(path), 'cascade notes', wait=0):
            yield
    except ship_locks.Busy:
        raise BlockingIOError(errno.EWOULDBLOCK, 'cascade evidence is held by another reader or writer') from None


@contextmanager
def checked_file(path: Path, *, append: bool = False):
    parent = _parent(path)
    descriptor = None
    stack = ExitStack()
    try:
        flags = os.O_RDWR | os.O_APPEND | os.O_CREAT if append else os.O_RDONLY
        descriptor = os.open(path.name, flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                             0o600, dir_fd=parent)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise ShipError('cascade runtime file is not a single regular file')
        # Only the appender locks, and with a lock of the ship's own under runtime/locks, never the file itself: a
        # jail sees the post's files and could hold them (flock needs no write access). Every file read here is
        # published whole by rename, so a reader needs no lock and writes nothing. Busy stays an OSError.
        if append:
            stack.enter_context(_exclusion(path))

        def verify():
            current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            named_parent = path.parent.stat(follow_symlinks=False)
            held_parent = os.fstat(parent)
            if ((current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
                    or not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                    or (named_parent.st_dev, named_parent.st_ino) != (held_parent.st_dev, held_parent.st_ino)):
                raise ShipError('cascade runtime file identity changed')

        verify()
        yield descriptor, verify
        verify()
    finally:
        stack.close()
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


def _read(descriptor: int, *, max_bytes: int | None = None) -> bytes:
    if max_bytes is not None:
        if type(max_bytes) is not int or max_bytes < 0:
            raise ValueError('read limit must be a nonnegative integer')
        if os.fstat(descriptor).st_size > max_bytes:
            raise ShipError('cascade runtime file exceeds read limit')
    pieces = []
    size = 0
    while max_bytes is None or size < max_bytes:
        count = 65536 if max_bytes is None else min(65536, max_bytes - size)
        piece = os.read(descriptor, count)
        if not piece:
            break
        pieces.append(piece)
        size += len(piece)
    if max_bytes is not None and os.fstat(descriptor).st_size > max_bytes:
        raise ShipError('cascade runtime file exceeds read limit')
    return b''.join(pieces)


def read_file(path: Path, *, max_bytes: int | None = None) -> bytes:
    """Read through the checked descriptor, optionally capping actual bytes read."""
    with checked_file(path) as (descriptor, verify):
        raw = _read(descriptor) if max_bytes is None else _read(descriptor, max_bytes=max_bytes)
        verify()
        return raw


def append_notes(path: Path, evidence: dict) -> dict:
    """Finish only the saved suffix, through the same checked, locked file descriptor."""
    addition = evidence['pending'].encode('utf-8')
    offset = evidence['size']
    with checked_file(path / 'responses.md', append=True) as (descriptor, verify):
        if os.fstat(descriptor).st_size > offset + len(addition):
            raise ShipError('cascade notes differ from their append intent')
        raw = _read(descriptor)
        if (len(raw) < offset or hashlib.sha256(raw[:offset]).hexdigest() != evidence['sha256']
                or not addition.startswith(raw[offset:])):
            raise ShipError('cascade notes differ from their append intent')
        verify()
        view = memoryview(addition[len(raw) - offset:])
        while view:
            view = view[os.write(descriptor, view):]
        verify()
    complete = raw[:offset] + addition
    return dict(size=len(complete), sha256=hashlib.sha256(complete).hexdigest(), pending=None)


def error_reason(error: Exception) -> str:
    if isinstance(error, ExclusionUnknown):
        return 'cascade step exclusion evidence is unreadable'
    if isinstance(error, json.JSONDecodeError):
        return 'cascade evidence is malformed JSON'
    if isinstance(error, (ShipError, ValueError, KeyError, TypeError)):
        return 'cascade evidence is inconsistent'
    if isinstance(error, OSError):
        return 'cascade evidence is unreadable'
    return 'cascade coordination could not finish'


def hold(runtime: Path, question: str, reason: str, signature: str) -> bool:
    question = key(question)
    data = dict(format=1, request=question, reason=reason, signature=signature)
    path = runtime / 'post' / 'cascade-holds' / (question + '.json')
    try:
        if json.loads(read_file(path)) == data:
            return False
    except (OSError, ValueError, ShipError):
        pass
    post_dir.ensure_directory(path.parent)
    durable.replace_file(path, json.dumps(data) + '\n', encoding='utf-8')
    return True


def clear(runtime: Path, question: str) -> None:
    path = runtime / 'post' / 'cascade-holds' / (key(question) + '.json')
    try:
        post_dir.remove(path)
    except FileNotFoundError:
        return


def holds(runtime: Path) -> list[str]:
    """Read only fixed derived operational reasons, never run/report/provider bodies."""
    try:
        paths = post_dir.json_paths(runtime / 'post' / 'cascade-holds')
    except ShipError as error:
        return [str(error)] if str(error).startswith(post_dir.LINK_HELD) else ['cascades held: hold evidence unreadable']
    except OSError:
        return ['cascades held: hold evidence unreadable']
    lines = []
    for path in paths:
        reason = 'hold evidence unreadable'
        try:
            data = json.loads(read_file(path))
            if (type(data['format']) is int and data['format'] == 1 and data['request'] == path.stem
                    and data['reason'] in HOLD_REASONS and isinstance(data['signature'], str)):
                reason = data['reason']
        except FileNotFoundError:
            if post_dir.gone(path):
                continue
        except (OSError, ValueError, ShipError, KeyError, TypeError):
            pass
        label = post.short_of(path.stem) if path.stem.startswith('que-') and key(path.stem) == path.stem else 'run'
        lines.append(f'{label} cascade held: {reason}')
    return lines
