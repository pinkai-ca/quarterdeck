"""Bounded accounting failures and service-owned, disposable status evidence."""

from __future__ import annotations

import errno
import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime

from . import presentation, servicectl

BOUND = 3
NAME = 'accounting-health.json'
UNKNOWN = 'accounting health unknown'
SQL_CAUSES = {
    sqlite3.SQLITE_BUSY: 'database busy', sqlite3.SQLITE_LOCKED: 'database locked',
    sqlite3.SQLITE_FULL: 'database full', sqlite3.SQLITE_READONLY: 'database read only',
    sqlite3.SQLITE_IOERR: 'database I/O error', sqlite3.SQLITE_CORRUPT: 'database corrupt',
    sqlite3.SQLITE_CANTOPEN: 'database unavailable',
}
FILE_CAUSES = {
    errno.ENOSPC: 'storage full', errno.EACCES: 'file permission denied',
    errno.EPERM: 'file permission denied', errno.ENOENT: 'source unavailable',
}
CAUSES = frozenset(SQL_CAUSES.values()) | frozenset(FILE_CAUSES.values()) | {
    'database error', 'file I/O error', 'invalid accounting evidence', 'accounting exception',
    'record past the read bound', 'record refused',
}


def cause(error):
    """Fixed labels only: exception messages and supplied class names are content."""
    if isinstance(error, sqlite3.Error):
        code = getattr(error, 'sqlite_errorcode', None)
        return SQL_CAUSES.get(code & 255, 'database error') if type(code) is int else 'database error'
    from .harness import record_file
    if isinstance(error, record_file.PastBound):
        return 'record past the read bound'  # its turns are not read: the status line names the seat and its size
    if isinstance(error, record_file.Refused):
        return 'record refused'  # a link, another's file or not a regular one: never read
    if isinstance(error, OSError):
        return FILE_CAUSES.get(error.errno, 'file I/O error')
    if isinstance(error, (ValueError, TypeError, KeyError)):
        return 'invalid accounting evidence'
    return 'accounting exception'


@dataclass
class Health:
    failures: int = 0
    since: str | None = None
    current: str | None = None
    causes: dict[str, int] = field(default_factory=dict)
    checked: bool = False

    def failed(self, reason, at):
        if reason not in CAUSES:
            raise ValueError('unclassified accounting cause')
        if not self.failures:
            self.since = at.isoformat(timespec='seconds')
        self.failures += 1
        self.current = reason
        self.causes[reason] = self.causes.get(reason, 0) + 1
        self.checked = True
        return self.failures == BOUND

    def shown(self):
        return shown(self.checked, self.failures, self.since, self.current)

    def committed(self):
        alarmed = self.failures >= BOUND
        self.failures, self.since, self.current, self.causes = 0, None, None, {}
        self.checked = True
        return alarmed


def shown(checked, failures, since, current):
    """What the status line says of accounting: nothing while it commits or below the bound."""
    if not checked:
        return UNKNOWN
    if failures >= BOUND:
        return f"accounting failing since {since}: {current}"
    return None  # below the explicit transient-failure bound, not a measurement of turns


def publish(runtime, health):
    """The service refreshes display evidence alongside its independent heartbeat."""
    data = dict(format=1, pid=os.getpid(), process_start=presentation.process_start(os.getpid()),
                observed=time.time(), checked=health.checked, failures=health.failures,
                since=health.since, cause=health.current, causes=health.causes)
    path = runtime / NAME
    temporary = path.with_suffix('.tmp')
    written = False
    try:
        temporary.write_text(json.dumps(data, allow_nan=False) + '\n')
        os.replace(temporary, path)
        written = True
    finally:
        if not written:
            path.unlink(missing_ok=True)  # a failed rendering must not leave old healthy evidence available


def _valid(data):
    if (not isinstance(data, dict) or set(data) != {
            'format', 'pid', 'process_start', 'observed', 'checked', 'failures', 'since', 'cause', 'causes'}
            or type(data['format']) is not int or data['format'] != 1
            or type(data['pid']) is not int or data['pid'] <= 0
            or not isinstance(data['process_start'], str) or not data['process_start'].isdigit()
            or type(data['observed']) not in (int, float)
            or type(data['checked']) is not bool
            or type(data['failures']) is not int or data['failures'] < 0
            or not isinstance(data['causes'], dict)
            or any(key not in CAUSES or type(value) is not int or value <= 0
                   for key, value in data['causes'].items())
            or sum(data['causes'].values()) != data['failures']):
        return False
    if data['failures']:
        if not data['checked'] or data['cause'] not in data['causes'] or not isinstance(data['since'], str):
            return False
        since = datetime.fromisoformat(data['since'])
        return (since.tzinfo is not None and since.isoformat(timespec='seconds') == data['since']
                and since.timestamp() <= data['observed'])
    return data['since'] is None and data['cause'] is None


def reading(runtime):
    """A missing, stale or differently owned snapshot cannot say accounting is healthy."""
    try:
        beat = servicectl.read_heartbeat(runtime)
        data = json.loads((runtime / NAME).read_text())
        if (not _valid(data) or beat is None or not 0 <= beat[1] <= servicectl.STALE_SECONDS
                or not 0 <= time.time() - data['observed'] <= servicectl.STALE_SECONDS
                or data['pid'] != beat[0] or not servicectl.alive(runtime)
                or presentation.process_start(beat[0]) != data['process_start']):
            return UNKNOWN
        again = servicectl.read_heartbeat(runtime)
        if again is None or again[0] != beat[0] or not 0 <= again[1] <= servicectl.STALE_SECONDS:
            return UNKNOWN
        return shown(data['checked'], data['failures'], data['since'], data['cause'])
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return UNKNOWN
