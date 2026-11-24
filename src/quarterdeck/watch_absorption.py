"""Passive status for an inbox watch whose printed post was absorbed mid-turn."""

from collections import defaultdict

from . import idle, inbox_watch, post
from .harness.base import record_fingerprint
from .ship import ShipError
from .tmux import TmuxError


def alerts(seats, store, records):
    watch_attempts = defaultdict(dict)
    for attempt in records:
        if attempt.result == 'watch':
            watch_attempts[attempt.seat][attempt.path.name] = attempt
    lines = []
    for name, attempts in watch_attempts.items():
        seat = seats.get(name)
        if seat is None or not seat.session.human:
            continue
        try:
            lines.extend(_for_seat(seat, store, attempts))
        except (OSError, ValueError, TypeError, ShipError, TmuxError):
            pass  # absent or unreadable evidence proves no association
    return lines


def _for_seat(seat, store, attempts):
    match = getattr(seat.harness, 'inbox_watch_absorption', None)
    if match is None:
        return []
    name = seat.name
    runtime = seat.ship.runtime
    watch = post.watch_reading(runtime, name)
    if watch.armed is not False or watch.generation is None:
        return []
    batch, proof = inbox_watch.accepted_batch(runtime, watch.generation, attempts)
    if not batch:
        return []
    open_posts = {message.id: message for message in store.open_for(name)}
    unclaimed = [open_posts[a.message] for a in batch if a.message in open_posts
                 and not any(reply.sender == name and reply.kind in ('ack', 'report', 'answer') and reply.at > a.when
                             for reply in store.replies(open_posts[a.message]))]
    if not unclaimed:
        return []

    launch = seat.launched()
    pane = seat.pane()  # this pane belongs to the launch by ID and PID
    record = seat.lookup_record()
    if (launch is None or pane is None or pane.dead or pane.in_mode
            or record is None):
        return []
    before = record_fingerprint(record)
    state = seat.harness.state(record)
    if (idle.check(state) is not None or state.failure
            or not match(record, seat.ship.cwd_of(seat.spec), launch, state, proof, (a.when for a in batch))):
        return []
    pane = seat.pane()
    if (pane is None or pane.dead or pane.in_mode or seat.launched() != launch
            or seat.lookup_record() != record or record_fingerprint(record) != before
            or post.watch_reading(runtime, name) != watch):
        return []
    return [f'{name}: {message.short} absorbed mid-turn, not taken up' for message in unclaimed]
