"""Disposable service move decisions; presentation only, never lifecycle clearance.

Human status reads these categories and launch/watch metadata, never a conversation.
"""

import json
import time

from . import trusted_files
from . import durable, idle, post


MAX_AGE_NS = 5_000_000_000


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
    data = dict(version=1, generation=binding, category=category,
                detail=hold.reason if hold is not None and hold.dimension == 'tools-pending'
                and not seat.session.human else None,
                wall=time.time_ns(), monotonic=time.monotonic_ns())
    target = path(seat)
    try:
        previous = json.loads(target.read_text())
        if (previous.get('version') == 1 and previous.get('generation') == binding
                and previous.get('category') == category
                and previous.get('detail') == data['detail']
                and type(previous.get('wall')) is int and type(previous.get('monotonic')) is int
                and 0 <= data['wall'] - previous['wall'] <= MAX_AGE_NS // 2
                and 0 <= data['monotonic'] - previous['monotonic'] <= MAX_AGE_NS // 2):
            return  # bounded refresh, not a durable write on every service pass
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    durable.replace_file(target, json.dumps(data, sort_keys=True) + '\n')


def reading(seat):
    """Return a current category, clear decision, or an explicit unknown, without a record read."""
    try:
        before = generation(seat)
        data = json.loads(path(seat).read_text())
        category = data['category']
        if (type(data['version']) is not int or data['version'] != 1
                or before is None or data['generation'] != before or generation(seat) != before
                or category is not None and category not in idle.REASONS
                or type(data['wall']) is not int or type(data['monotonic']) is not int):
            return 'move proof unknown'
        wall_age, age = time.time_ns() - data['wall'], time.monotonic_ns() - data['monotonic']
        if not (0 <= wall_age <= MAX_AGE_NS and 0 <= age <= MAX_AGE_NS
                and abs(wall_age - age) <= 50_000_000):
            return 'move proof unknown'
        detail = data.get('detail')
        if not seat.session.human and category == 'tools-pending' and detail is not None:
            if (not isinstance(detail, str) or len(detail) > 512 or not detail.startswith('tools pending: ')
                    or any(not c.isprintable() for c in detail)):
                return 'move proof unknown'
            return detail
        return idle.REASONS[category] if category is not None else None
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return 'move proof unknown'
