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


def unreadable(target, why):
    """The refusal for a barrier no reader can trust: it names the file, why, and what settles it."""
    return ShipError(f'service stop barrier {target} unreadable ({why}); stop and start held: '
                     'qd service --settle-barrier moves it aside unchanged once no process an earlier stop '
                     'recorded can be running, then stop or start again')


def read(runtime):
    target = path(runtime)
    try:
        descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise unreadable(target, type(error).__name__) from None
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
    except (OSError, ValueError, TypeError, KeyError, UnicodeError) as error:
        # Its own refusals say why in words; any other error is named by its kind, never by what the file holds.
        raise unreadable(target, str(error) if type(error) is ValueError else type(error).__name__) from None
    finally:
        os.close(descriptor)


def remaining(known):
    """The recorded identities still running that a stop must end: never a tmux server nor anything under one.

    A stop spares a tmux server in the service's scope with everything under it (service_processes.server), so
    such an identity is not waited for: a barrier an older stop wrote with the ship's server in it would else
    never clear. ``spared`` names them."""
    live = {pid: start for pid, start in known.items() if not shutdown.process_gone(dict(pid=pid, start=start))}
    held = spared(live)
    return {pid: start for pid, start in live.items() if pid not in held}


def spared(known):
    """The recorded identities still running that are a tmux server, or under one: left running by every stop."""
    from . import service_processes
    live = {pid: start for pid, start in known.items() if not shutdown.process_gone(dict(pid=pid, start=start))}
    servers = [pid for pid in live if service_processes.server(pid)]
    if not servers:
        return {}
    under = service_processes.descendants(servers)
    return {pid: start for pid, start in live.items() if pid in under}


def spared_words(known):
    """The stop line's words for the tmux servers a barrier recorded and every stop leaves running."""
    from . import service_processes
    servers = sorted(pid for pid in spared(known) if service_processes.server(pid))
    if not servers:
        return ''
    return (f"an earlier stop recorded a tmux server (pid {', '.join(map(str, servers))}); "
            "left running with its panes")


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
