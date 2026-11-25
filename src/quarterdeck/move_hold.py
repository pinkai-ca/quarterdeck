"""Disposable service move decisions; presentation only, never lifecycle clearance.

Human status reads these categories and launch/watch metadata, never a conversation.
"""

import json
import time

from . import trusted_files
from . import durable, idle, post
from .ship import ShipError


MAX_AGE_NS = 5_000_000_000
WALL_MOVE = 'wall-move.json'
UNKNOWN = 'move proof unknown'
SELECTED = {
    'lifecycle-busy': 'another lifecycle transition is in progress',
    'resume-unproved': 'its conversation could not be identified',
    'outcome-unknown': 'move outcome unknown',
    'resting-unit': 'configured user unit is running or its timer is due within five minutes',
    'resting-unit-unknown': 'configured user unit state unreadable; needs a human',
}


def generation(seat):
    launched = seat.launched()
    if launched is None:
        return None
    try:
        info = post.watch_file(seat.ship.runtime, seat.name).stat()
        watch = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
    except FileNotFoundError:
        watch = None
    return dict(pane=launched.pane, pid=launched.pid, at=launched.at,
                harness=launched.harness, watch=watch)


def path(seat):
    return seat.trusted / trusted_files.MOVE_HOLD


def publish(seat, binding, hold):
    """Service-only decision: no command, output or native call identity is saved."""
    if binding is None or generation(seat) != binding:
        return
    category = hold.dimension if hold is not None else None
    if category is not None and category not in idle.REASONS:
        raise ValueError('unsupported move hold category')
    kept = (category == 'tools-pending' and not seat.session.human) or category in NAMED
    detail = hold.reason if kept else None
    _write(path(seat), dict(version=1, generation=binding, category=category, detail=detail))


def _write(target, data):
    wall, monotonic = time.time_ns(), time.monotonic_ns()
    try:
        previous = json.loads(target.read_text())
        if (type(previous.get('version')) is int and all(previous.get(key) == value for key, value in data.items())
                and type(previous.get('wall')) is int and type(previous.get('monotonic')) is int
                and 0 <= wall - previous['wall'] <= MAX_AGE_NS // 2
                and 0 <= monotonic - previous['monotonic'] <= MAX_AGE_NS // 2):
            return  # bounded refresh, not a durable write on every service pass
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    durable.replace_file(target, json.dumps(dict(data, wall=wall, monotonic=monotonic), sort_keys=True) + '\n')


def _fresh(seat, target):
    before = generation(seat)
    data = json.loads(target.read_text())
    if (type(data['version']) is not int or data['version'] != 1
            or before is None or data['generation'] != before or generation(seat) != before
            or type(data['wall']) is not int or type(data['monotonic']) is not int):
        raise ValueError('invalid move observation')
    wall_age, age = time.time_ns() - data['wall'], time.monotonic_ns() - data['monotonic']
    if not (0 <= wall_age <= MAX_AGE_NS and 0 <= age <= MAX_AGE_NS and abs(wall_age - age) <= 50_000_000):
        raise ValueError('expired move observation')
    return data


NAMED = ('input-unknown', 'queue-unknown')  # holds whose exact name the wall reading shows


def _idle_reason(seat, data):
    category, detail = data['category'], data.get('detail')
    if category in NAMED and detail is not None:
        if not isinstance(detail, str) or len(detail) > 512 or any(not c.isprintable() for c in detail):
            raise ValueError('invalid move detail')
        return detail
    if not seat.session.human and category == 'tools-pending' and detail is not None:
        if (not isinstance(detail, str) or len(detail) > 512 or not detail.startswith('tools pending: ')
                or any(not c.isprintable() for c in detail)):
            raise ValueError('invalid move detail')
        return detail
    return idle.REASONS[category] if category is not None else None


def reading(seat):
    """Return a current category, clear decision, or an explicit unknown, without a record read."""
    try:
        return _idle_reason(seat, _fresh(seat, path(seat)))
    except (OSError, ShipError, ValueError, TypeError, KeyError, AttributeError):
        return UNKNOWN


def publish_wall(seat, binding, source, category=None, *, target=None, detail=None):
    """Record the service's stage, independently of ordinary idle observations."""
    try:
        if binding is None or generation(seat) != binding or seat.pool.name != source:
            return
        kept = (category == 'tools-pending' and not seat.session.human) or category in NAMED
        data = dict(version=1, generation=binding, source=source, category=category, target=target,
                    detail=detail if kept else None)
        _write(seat.trusted / WALL_MOVE, data)
    except (OSError, ShipError):
        pass  # this optional reading grants no clearance and never changes the move


def wall_reading(seat):
    """Return the stage's wording and whether it was observed by the wall decision itself."""
    try:
        data = _fresh(seat, seat.trusted / WALL_MOVE)
        category, target = data['category'], data['target']
        source = seat.pool.name
        if data['source'] != source:
            raise ValueError('move source changed')
        if target is not None:
            if target == source or target not in seat.spec.pools:
                raise ValueError('move destination changed')
            reason = SELECTED[category]
            if category == 'outcome-unknown':
                return f'{target} was chosen: {reason}', True
            return f'move held for {target}: {reason}', True
        if category == 'record':
            return 'record unreadable: no move weighed', True
        if category == 'record-missing':
            return 'record unavailable: no move weighed', True
        reason = _idle_reason(seat, data)
        return (f'{reason}: no move weighed' if reason else None), True
    except (OSError, ShipError, ValueError, TypeError, KeyError, AttributeError):
        # Other idle callers and older services only observed an idle guard, with no decision-stage evidence.
        hint = reading(seat)
        return (f'{hint}; move decision unknown' if hint and hint != UNKNOWN else UNKNOWN), False
