"""Derived settlement of exact informational notices; never a read receipt."""

from collections import Counter
from datetime import datetime
from pathlib import Path
import re
import time

from . import post


def notice_class(message):
    if message.sender != post.SHIP or message.kind != 'report' or message.re is not None:
        return None
    if message.body in (f"ship's orders changed · read runtime/brief/{message.to}.md",
                        f"brief changed · read runtime/brief/{message.to}.md"):
        return 'changed orders'
    if re.fullmatch(r'moved to pool [^\s]+ from [^\s]+ (?:at its wall|by qd set)', message.body):
        return 'pool move'
    return None


def held_request(message):
    """Only the ship's complete held-relaunch report, naming its own request."""
    if message.sender != post.SHIP or message.kind != 'report' or message.re is not None:
        return None
    tail = re.fullmatch(r'(?P<head>[^\n]+) · request (?P<request>[A-Za-z0-9][A-Za-z0-9_.-]*)', message.body)
    if tail is None:
        return None
    body = tail['head']
    for verb, name in (('restart', 'restart'), ('fresh', 'fresh start')):
        word = (f'give your own word as the last act of a turn: '
                f'qd {verb} {message.to} --release')
        held = (rf'your own {name} was not applied: your turn ended with it held · [^\n]+ · '
                rf'ask again once that has ended; or, losing what you have running, {re.escape(word)}')
        waiting = (rf'(?:your own {name} waits|a {name} of this seat, asked by [^\n]+, waits) · '
                   rf'[^\n]+ · it applies once that has ended; to apply it without waiting, losing '
                   rf'what you have running, {re.escape(word)}')
        if re.fullmatch(held, body) or re.fullmatch(waiting, body):
            return tail['request']
    return None


# Assumes one reading thread (the service's own loop). A race here could only
# repeat a read, never settle wrongly: reuse requires an unchanged stamp.
# A second reading thread takes a lock first.
class _LaunchCache:
    def __init__(self):
        self.declarations = {}
        self.owner_ship = None
        self.owners = {}
        self.launches = {}
        self.requests = {}


def _stamp(path):
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode)


def _old(stamp, started):
    return stamp is not None and all(
        max(item[3:5]) + post.POST_TIMESTAMP_TICK_NS < started
        for item in stamp if item is not None)


def _observed(cache, key, stamp, read):
    """Reuse only an unchanged, previously aged observation; never cache a failed read."""
    started = time.time_ns()
    previous = cache.pop(key, None)
    before = stamp()
    checked = time.time_ns()
    if (previous is not None and previous[0] == before and previous[1] <= started <= checked
            and _old(before, started)):
        cache[key] = previous
        return previous[2]
    value = read()
    after = stamp()
    finished = time.time_ns()
    if before != after:
        raise ValueError('notice input changed while reading')
    if started <= checked <= finished and _old(before, started):
        cache[key] = before, started, value
    return value


def launched_at(runtime, seat, *, cache=None):
    """Only a readable, validated recorded launch can settle earlier notices."""
    from .seat import Seat
    from . import ship as shipmod
    if Path(seat).name != seat or seat in ('', '.', '..'):
        return None
    cache = cache if cache is not None else _LaunchCache()
    try:
        ship = _observed(cache.declarations, runtime,
                         lambda: (_stamp(runtime.parent / 'ship.toml'),),
                         lambda: shipmod.load(runtime.parent))
        if cache.owner_ship is not ship:
            cache.owner_ship = ship
            cache.owners.clear()
            cache.launches.clear()
        owner = cache.owners.get(seat)
        fresh = owner is None
        if fresh:
            owner = Seat(ship, ship.seat(seat), None)
            cache.owners[seat] = owner

        def read_launch():
            current = owner if fresh else Seat(ship, ship.seat(seat), None)
            cache.owners[seat] = current
            launched = current.launched()
            if launched is None:
                return None
            at = launched.when
            if at.tzinfo is None:
                raise ValueError('notice launch time has no timezone')
            return at

        return _observed(cache.launches, seat, owner.launch_stamp, read_launch)
    except (OSError, ValueError, shipmod.ShipError):
        return None  # missing/unreadable evidence grants no settlement



def request_records(runtime, seat, *, cache):
    """The queue's trusted record API, under the launch cache's same tick rule."""
    from . import request_queue
    return _observed(cache.requests, seat,
                     lambda: request_queue.notice_stamp(runtime, seat),
                     lambda: request_queue.notice_records(runtime, seat))


def request_state(runtime, identifier, seat, notice, *, cache):
    from . import request_queue
    from .ship import ShipError
    try:
        records = request_records(runtime, seat, cache=cache)
        return request_queue.notice_state(runtime, identifier, seat, notice, records=records)
    except (OSError, ValueError, TypeError, ShipError):
        return None


def settled(store, seat, *, brief_at=None):
    """A later launch or newer exact notice; no durable certificate or message edits.

    brief_at is used only while planning an actual launch which supplies the brief.
    If startup fails, ordinary readers still use the unchanged recorded launch.
    """
    incoming = store.for_seat(seat)
    candidates = [(m, notice_class(m)) for m in incoming]
    candidates = [(m, kind) for m, kind in candidates if kind is not None and m.at.tzinfo is not None]
    held = [(m, held_request(m)) for m in incoming if m.at.tzinfo is not None]
    held = [(m, request) for m, request in held if request is not None]
    if candidates or held:
        cache = getattr(store, '_notice_launch_cache', None)
        if cache is None:
            cache = store._notice_launch_cache = _LaunchCache()
        launch = launched_at(store.dir.parent.parent, seat, cache=cache)
    else:
        launch = None
    if isinstance(brief_at, datetime) and brief_at.tzinfo is not None:
        launch = max(launch, brief_at) if launch is not None else brief_at
    latest = {}
    for message, kind in candidates:
        latest[kind] = max(latest.get(kind, (message.at, message.id)), (message.at, message.id))
    result = {}
    for message, kind in candidates:
        if launch is not None and message.at < launch:
            result[message.id] = f'{kind}: settled by a later launch'
        elif (message.at, message.id) < latest[kind]:
            result[message.id] = f'{kind}: settled by a newer notice'
    for message, request in held:
        if launch is not None and message.at < launch:
            result[message.id] = 'held restart: settled by a later launch'
        elif request_state(store.dir.parent.parent, request, seat, message.id, cache=cache) in ('applied', 'withdrawn', 'retired'):
            result[message.id] = 'held restart: settled by completion of its request'
    # A saved group/cascade terminal notice has no further duty after its own
    # root is closed. An arbitrary report on a closed root proves no such thing.
    terminal = [m for m in incoming if m.sender == post.SHIP and m.kind == 'report' and m.re]
    if terminal:
        from .thread_context import _terminal_notice
        by_id = {m.id: m for m in store.all()}
        for message in terminal:
            root = by_id.get(message.re)
            if (root is not None and root.to == seat and root.obligation and not store.is_open(root)
                    and _terminal_notice(store, message, root, seat, by_id)):
                result[message.id] = 'terminal notice: settled by closure of its work'
    return result


def summary(settled):
    """One bounded history line, with classes/counts only, never bodies or ids."""
    counts = Counter(settled.values())
    return ('qd > notice history: ' + '; '.join(f'{count} {reason}' for reason, count in sorted(counts.items()))
            if counts else '')
