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
SQL_DIAGNOSTICS = {
    sqlite3.SQLITE_BUSY: ('database busy', 'SQLITE_BUSY'),
    sqlite3.SQLITE_LOCKED: ('database locked', 'SQLITE_LOCKED'),
    sqlite3.SQLITE_FULL: ('database full', 'SQLITE_FULL'),
    sqlite3.SQLITE_READONLY: ('database read only', 'SQLITE_READONLY'),
    sqlite3.SQLITE_IOERR: ('database I/O error', 'SQLITE_IOERR'),
    sqlite3.SQLITE_CORRUPT: ('database corrupt', 'SQLITE_CORRUPT'),
    sqlite3.SQLITE_CANTOPEN: ('database unavailable', 'SQLITE_CANTOPEN'),
}
FILE_CAUSES = {
    errno.ENOSPC: 'storage full', errno.EACCES: 'file permission denied',
    errno.EPERM: 'file permission denied', errno.ENOENT: 'source unavailable',
}
USAGE_UNPROVED = 'standalone usage commit outcome unreadable'
USAGE_ROLLED_BACK = 'standalone usage commit rolled back'
CAUSES = frozenset(reason for reason, _ in SQL_DIAGNOSTICS.values()) | frozenset(FILE_CAUSES.values()) | {
    'database error', 'file I/O error', 'invalid accounting evidence', 'accounting exception',
    'record past the read bound', 'record refused', USAGE_UNPROVED, USAGE_ROLLED_BACK,
}


def sql_diagnostic(error):
    code = getattr(error, 'sqlite_errorcode', None)
    return SQL_DIAGNOSTICS.get(code & 255, ('database error', None)) if type(code) is int else ('database error', None)


def cause(error):
    """Fixed labels only: exception messages and supplied class names are content."""
    if isinstance(error, sqlite3.Error):
        return sql_diagnostic(error)[0]
    from .ledger import UsageCommitUnproved, UsageCommitRolledBack
    if isinstance(error, UsageCommitUnproved):
        return USAGE_UNPROVED
    if isinstance(error, UsageCommitRolledBack):
        return USAGE_ROLLED_BACK
    from .harness import record_file
    if isinstance(error, record_file.PastBound):
        return 'record past the read bound'  # its turns are not read: the status line names the seat and its size
    if isinstance(error, record_file.Refused):
        return 'record refused'  # a link, another's file or not a regular one: never read
    if isinstance(error, OSError):
        return FILE_CAUSES.get(error.errno, 'file I/O error') if type(error.errno) is int else 'file I/O error'
    if isinstance(error, (ValueError, TypeError, KeyError)):
        return 'invalid accounting evidence'
    return 'accounting exception'


def report_failure(service, error, at, *, health=None):
    """Log one onset with a known exception type and a fixed, bounded message."""
    reason = cause(error)
    if health is None:
        health = service.accounting_health
    if not health.failed(reason, at):
        return
    from .harness import record_file
    from .ledger import UsageCommitUnproved, UsageCommitRolledBack
    known = (sqlite3.Error, sqlite3.InterfaceError, sqlite3.DatabaseError, sqlite3.DataError, sqlite3.OperationalError,
             sqlite3.IntegrityError, sqlite3.InternalError, sqlite3.ProgrammingError, sqlite3.NotSupportedError,
             OSError, FileNotFoundError, PermissionError, RuntimeError, ValueError, TypeError, KeyError,
             record_file.PastBound, record_file.Refused, UsageCommitUnproved, UsageCommitRolledBack)
    kind = next((cls.__name__ for cls in known if type(error) is cls), 'Exception')
    diagnostic = None
    if isinstance(error, sqlite3.Error):
        _, diagnostic = sql_diagnostic(error)
    elif isinstance(error, OSError) and type(error.errno) is int:
        diagnostic = errno.errorcode.get(error.errno)
    message = f'{reason} ({diagnostic})' if diagnostic else reason
    service.log('accounting-failing', since=health.since, cause=health.current, failures=health.failures,
                causes=dict(health.causes), error_type=kind, message=message)


def report_record_failure(service, error, at):
    from .ledger import UsageCommitRolledBack, UsageCommitUnproved
    if isinstance(error, UsageCommitRolledBack):
        return  # the ledger already reported the settled usage outcome once
    health = service.usage_accounting_health if isinstance(error, UsageCommitUnproved) else service.accounting_health
    report_failure(service, error, at, health=health)


def report_success(service, health):
    previous, failures, causes = health.since, health.failures, dict(health.causes)
    if health.committed():
        service.log('accounting-recovered', since=previous, failures=failures, causes=causes)


@dataclass
class UsagePass:
    rollback: Exception | None = None
    committed: bool = False

    def note(self, error):
        if error is None:
            self.committed = True
        else:
            self.rollback = error


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


@dataclass(frozen=True)
class Reading:
    checked: bool
    failures: int
    since: str | None
    current: str | None
    causes: dict[str, int]

    def shown(self):
        return shown(self.checked, self.failures, self.since, self.current)


def visible(service):
    """Read record check state alongside the selected failure streak."""
    records = service.accounting_health
    usage = getattr(service, 'usage_accounting_health', None)
    streak = records
    if usage is not None and usage.failures and (not records.failures or records.failures < BOUND <= usage.failures):
        streak = usage
    return Reading(records.checked, streak.failures, streak.since, streak.current, dict(streak.causes))


def shown(checked, failures, since, current):
    """Give bounded alarms priority over the initial unknown record state."""
    if failures >= BOUND:
        return f"accounting failing since {since}: {current}"
    return None if checked else UNKNOWN


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
        if data['cause'] not in data['causes'] or not isinstance(data['since'], str):
            return False
        # Only the independent usage streak can accumulate before the first record check.
        if not data['checked'] and not data['causes'].keys() <= {USAGE_UNPROVED, USAGE_ROLLED_BACK}:
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
