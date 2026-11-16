"""A service stop's process identities, retained until every exit is confirmed."""

from __future__ import annotations

import json
from functools import wraps
import os
import stat

from . import durable, ship_locks, shutdown
from .ship import ShipError

NAME = 'service-stop.json'
MAX_BYTES = 1024 * 1024


def path(runtime):
    return ship_locks.path(runtime, NAME)


def read(runtime):
    target = path(runtime)
    try:
        descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError:
        raise ShipError(f'service stop barrier {target} unreadable; start held') from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError('not a bounded regular record')
        with os.fdopen(os.dup(descriptor), 'rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('too large')
        data = json.loads(raw)
        rows = data['processes']
        if (type(data.get('version')) is not int or data['version'] != 1 or not isinstance(rows, list)
                or not rows or any(not isinstance(row, dict) or type(row.get('pid')) is not int
                                  or row['pid'] <= 0 or not isinstance(row.get('start'), str)
                                  or not row['start'] or len(row['start']) > 128 for row in rows)):
            raise ValueError('unknown identities')
        known = {row['pid']: row['start'] for row in rows}
        if len(known) != len(rows):
            raise ValueError('repeated identity')
        return known
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        raise ShipError(f'service stop barrier {target} unreadable; start held') from None
    finally:
        os.close(descriptor)


def remaining(known):
    return {pid: start for pid, start in known.items()
            if not shutdown.process_gone(dict(pid=pid, start=start))}


def names(known):
    return ', '.join(f'pid {pid} start {start}' for pid, start in sorted(known.items()))


def retain(runtime, known):
    """Publish the full observed set before any signal, serialized with a starter's clearing read."""
    if not known:
        return
    with ship_locks.held(ship_locks.path(runtime, 'service-stop-guard.lock'), 'service stop barrier'):
        previous = read(runtime) or {}
        for pid, start in known.items():
            if pid in previous and previous[pid] != start and remaining({pid: previous[pid]}):
                raise ShipError(f'service stop barrier {path(runtime)} identity changed; start held')
            previous[pid] = start
        durable.replace_file(path(runtime), json.dumps(dict(version=1, processes=[
            dict(pid=pid, start=start) for pid, start in sorted(previous.items())])) + '\n')


def require_clear(runtime):
    """A start clears ended identities itself; a live or unreadable record holds it."""
    if read(runtime) is None:
        return
    with ship_locks.held(ship_locks.path(runtime, 'service-stop-guard.lock'), 'service stop barrier'):
        _clear(runtime)


def _clear(runtime):
    known = read(runtime)
    if known is None:
        return
    if left := remaining(known):
        raise ShipError(f'service stop barrier {path(runtime)}; exit unconfirmed: {names(left)}; start held')
    path(runtime).unlink()


def guard_start(function):
    """Serialize the start through confirmation with publication of a new stop attempt."""
    @wraps(function)
    def guarded(ship, *args, **kwargs):
        with ship_locks.held(ship_locks.path(ship.runtime, 'service-stop-guard.lock'), 'service start'):
            _clear(ship.runtime)
            return function(ship, *args, **kwargs)
    return guarded


def status(runtime):
    """Read-only: a standing barrier is visible until a start or completed stop clears it."""
    try:
        known = read(runtime)
        if known is None:
            return []
        left = remaining(known)
        return [f'service stop barrier: {names(left)}; exit unconfirmed, start held' if left else
                'service stop barrier: recorded processes ended; next start clears it']
    except ShipError as error:
        return [str(error)]
