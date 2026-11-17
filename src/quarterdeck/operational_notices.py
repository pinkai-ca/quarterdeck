"""Read bounded, expiring status notices from ship-local tools."""

from itertools import islice
import json
import os
from pathlib import Path
import re
import stat
import time

from .procstat import EXITED

DIRECTORY = 'notices'
MAX_ENTRIES = 64
MAX_BYTES = 2048
MAX_TEXT_BYTES = 128
MAX_TTL_NS = 120_000_000_000
CLOCK_SKEW_NS = 1_000_000_000
NAME = re.compile(r'[a-z][a-z0-9_-]{0,47}\.json')
START = re.compile(r'[1-9][0-9]{0,19}')
_TMUX_FORMAT = re.compile(r'#[\[({}#,:DFHIPSTWh]')
FIELDS = {'format', 'text', 'pid', 'process_start', 'wall_ns', 'monotonic_ns', 'expires_ns'}
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


def open_directory(runtime: Path) -> int:
    """Open the same-account notice directory. The caller closes the descriptor."""
    fd = os.open(runtime / DIRECTORY, _DIRECTORY_FLAGS)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise PermissionError('notice directory must be owned by the ship account and protected from other writers')
    except OSError:
        os.close(fd)
        raise
    return fd


def writer_start(pid: int) -> str | None:
    """A live same-account process start, with ownership and stat read through one proc directory."""
    try:
        fd = os.open(f'/proc/{pid}', _DIRECTORY_FLAGS)
        try:
            if os.fstat(fd).st_uid != os.geteuid():
                return None
            info = os.open('stat', _FILE_FLAGS, dir_fd=fd)
            with os.fdopen(info, 'rb') as stream:
                fields = stream.read(4096).rsplit(b')', 1)[1].split()
            start = fields[19].decode('ascii')
            return start if fields[0].decode('ascii') not in EXITED and START.fullmatch(start) else None
        finally:
            os.close(fd)
    except (OSError, IndexError, UnicodeError):
        return None


def _unique_fields(pairs):
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError('duplicate notice fields')
    return fields


def _valid_text(text) -> bool:
    return (isinstance(text, str) and text.isprintable() and text == text.strip()
            and len(text.encode('utf-8')) <= MAX_TEXT_BYTES and _TMUX_FORMAT.search(text) is None)


def _reading(folder: int, name: str) -> str | None:
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=folder)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o022 or info.st_size > MAX_BYTES):
                return None
            raw = os.read(fd, MAX_BYTES + 1)
        finally:
            os.close(fd)
        if len(raw) > MAX_BYTES:
            return None
        data = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_fields)
        if not isinstance(data, dict) or set(data) != FIELDS or type(data['format']) is not int or data['format'] != 1:
            return None
        text, pid, start = data['text'], data['pid'], data['process_start']
        if not _valid_text(text):
            return None
        if type(pid) is not int or not 0 < pid <= 2**31 - 1 or not isinstance(start, str) or not START.fullmatch(start):
            return None
        wall, monotonic, expires = data['wall_ns'], data['monotonic_ns'], data['expires_ns']
        if any(type(value) is not int or value <= 0 for value in (wall, monotonic, expires)):
            return None
        ttl = expires - wall
        age, elapsed = time.time_ns() - wall, time.monotonic_ns() - monotonic
        if not (0 < ttl <= MAX_TTL_NS and 0 <= age < ttl and 0 <= elapsed < ttl
                and abs(age - elapsed) <= CLOCK_SKEW_NS):
            return None
        return text if writer_start(pid) == start else None
    except (OSError, ValueError, RecursionError):
        return None


def readings(runtime: Path) -> list[str]:
    """Return live notice text in tool name order. Absence and unreadable evidence are ordinary."""
    try:
        fd = open_directory(runtime)
        try:
            with os.scandir(fd) as entries:
                names = [entry.name for entry in islice(entries, MAX_ENTRIES + 1)]
            if len(names) > MAX_ENTRIES:
                return []
            return [text for name in sorted(names) if NAME.fullmatch(name) and (text := _reading(fd, name))]
        finally:
            os.close(fd)
    except OSError:
        return []
