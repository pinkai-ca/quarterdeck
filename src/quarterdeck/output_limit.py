"""A seat whose last turn stopped at its program's output limit, with an order of its own open, is seen.

A turn that ends at the output limit may have written nothing: the seat then sits idle with its order open, and an
ordinary reading shows only 'idle'. The reader names such a turn where it has measured its program's form
(``State.ended_at_limit``); where it has not, the state is unknown and nothing is said. The seat is named on the
status line and its alarm goes once to the systems seats; both clear when a new turn starts or the order closes.
Nothing here types into the seat: a systems seat nudges it or starts it fresh (law: one-door).

A seat in a human's session is not read here: an automatic reading never opens the person's conversation.
"""
from __future__ import annotations

from . import post

LINE = '{seat}: its last turn stopped at its output limit with an order open'
CHANNEL = 'output-limit'


def reading(seat, store: post.Store | None = None) -> dict | None:
    """The standing stop as a key (the record and its turn count), or None: no such stop, a turn running, no order
    of the seat's own open, a form the reader has not measured, or a seat in a human's session."""
    session = getattr(seat, 'session', None)
    if session is None or session.human:
        return None
    read = getattr(seat, 'record', None)
    try:
        record = read() if read is not None else None
        if record is None:
            return None
        from . import record_follow  # each pass reads only what the record gained since (law: record-first)
        state = record_follow.state(seat, record)
    except OSError:
        return None  # the unreadable record is reported by the ordinary reading
    if getattr(state, 'ended_at_limit', None) is not True or state.turn_open is not False:
        return None
    if store is None:
        store = post.Store(seat.ship.runtime)
    orders = sorted(m.id for m in store.open_for(seat.name) if m.kind == 'order')
    if not orders:
        return None
    return dict(record=str(record), turns=state.turns, orders=orders)


def line(seat, store: post.Store | None = None) -> str | None:  # law: nothing-waits-silently
    return LINE.format(seat=seat.name) if reading(seat, store) is not None else None


def step(service, seat) -> None:
    """Tell the systems seats once of a standing stop, through the alarm road; a cleared stop clears its alarm."""
    from . import alarms
    key = reading(seat, service.store)
    text = (LINE.format(seat=seat.name) + '. Notification only: nothing was typed. A systems seat nudges it or starts '
            'it fresh.')
    alarms.notify(service, seat.name, False if key is not None else None, key, text, channel=CHANNEL,
                  ship=service.ship, up=service.systems_recipients(seat) if key is not None else None,
                  accepts=(lambda saved: saved == text) if key is not None else None)
