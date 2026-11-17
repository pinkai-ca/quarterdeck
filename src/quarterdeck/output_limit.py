"""A seat whose last turn stopped at its program's output limit, with an order of its own open, is seen.

A turn that ends at the output limit may have written nothing: the seat then sits idle with its order open, and an
ordinary reading shows only 'idle'. The reader names such a turn where it has measured its program's form
(``State.ended_at_limit``); where it has not, the state is unknown and nothing is said. The seat is named on the
status line while the stop stands; both clear when a new turn starts or the order closes. Nothing here types into the
seat: a systems seat nudges it or starts it fresh (law: one-door).

The alarm to the systems seats waits for the thinking recovery, which runs earlier in the same pass and takes such a
stop itself where it can (``recovery``). It goes, once, when the recovery has declined the stop: no recovery for the
seat, no capped turn it reads, one it does not take, or a mode it does not take; or when its proof is held. One whose
continuation it delivered says nothing for WAIT_SECONDS after the paste, and then goes if the stop still stands: the
continuation never started, or it stopped there again with the one recovery that turn allows already spent. One the
recovery would take but has not claimed, or is claiming, waits as long from the pass that first saw it, then goes if
the stop still stands: a claim whose door delivery was interrupted stays at 'off' and is never advanced. A service
restart starts that wait again.

A seat in a human's session is not read here: an automatic reading never opens the person's conversation.
"""
from __future__ import annotations

from . import post

LINE = '{seat}: its last turn stopped at its output limit with an order open'
CHANNEL = 'output-limit'
# How long a stop the recovery has in hand stands before the alarm goes. The recovery's continuation is a door
# delivery, and the door's own wait for a delivery to show in the record is pending.WAIT_SECONDS, 600 s: past it the
# door itself calls an unconfirmed delivery a person's matter. The recovery's own steps take seconds (its controls
# wait recovery.MODE_SECONDS, 2 s, each, and it runs every pass), so 600 s covers a continuation's start with room.
WAIT_SECONDS = 600


def reading(seat, store: post.Store | None = None) -> dict | None:
    """The standing stop as a key (the record and its turn count), or None: no such stop, a turn running, no order
    of the seat's own open, a form the reader has not measured, or a seat in a human's session."""
    return _stop(seat, store)[0]


def _stop(seat, store: post.Store | None = None):
    session = getattr(seat, 'session', None)
    if session is None or session.human:
        return None, None
    read = getattr(seat, 'record', None)
    try:
        record = read() if read is not None else None
        if record is None:
            return None, None
        from . import record_follow  # each pass reads only what the record gained since (law: record-first)
        state = record_follow.state(seat, record)
    except OSError:
        return None, None  # the unreadable record is reported by the ordinary reading
    if getattr(state, 'ended_at_limit', None) is not True or state.turn_open is not False:
        return None, state
    if store is None:
        store = post.Store(seat.ship.runtime)
    orders = sorted(m.id for m in store.open_for(seat.name) if m.kind == 'order')
    if not orders:
        return None, state
    return dict(record=str(record), turns=state.turns, orders=orders), state


def line(seat, store: post.Store | None = None) -> str | None:  # law: nothing-waits-silently
    return LINE.format(seat=seat.name) if reading(seat, store) is not None else None


def _recovery(seat, state):
    """The thinking recovery's decision on this stop, read only: 'claiming', 'delivered' (with its paste's moment),
    'pending' (it would take it and has not claimed it), 'held', or 'declined'.

    Its journals first, then its own reading of the turn, as recovery._seat takes a stop: the seat's recovery client,
    the capped turn, its eligibility, the one allowance a turn's conversation and work share, and the mode."""
    from datetime import datetime
    from . import pending, recovery
    proofs, errors = pending.journals(seat)
    if errors:
        return 'declined', None  # an unreadable journal: the recovery holds the seat, and so does a person
    for proof in proofs:
        meta = proof.data.get('recovery')
        if meta is None or meta['mode'] == 'done':
            continue
        if meta['mode'] == 'held':
            return 'held', None
        if meta['mode'] in ('submitted', 'restoring'):
            return 'delivered', datetime.fromisoformat(proof.data['since'])
        return 'claiming', None
    candidate = getattr(state, 'capped', None)
    record = seat.record()
    for proof in proofs:
        # The turn its continuation began, or one sharing the allowance recovery._seat finds spent: no further
        # recovery for it. Its continuation's text is the seat's last input, or its work is the recovery's input.
        meta = proof.data.get('recovery')
        if meta is None or proof.data['record'] != str(record):
            continue
        conversation = candidate.conversation if candidate is not None else getattr(state, 'conversation', None)
        if conversation == meta['source']['conversation'] and (
                state.last_input == proof.data['payload']
                or (meta['recovery_input'] is not None and getattr(state, 'work_id', None) == meta['recovery_input'])
                or (candidate is not None and candidate.work in (meta['source']['work'], meta['recovery_input']))):
            return 'delivered', datetime.fromisoformat(proof.data['since'])
    client = recovery._client(seat)
    if client is None or candidate is None or not client.eligible(candidate):
        return 'declined', None
    from .door import Door
    if client.mode(state, Door(seat).screen()) != 'original':
        return 'declined', None
    return 'pending', None


def _waits(service, seat, key, state) -> bool:
    """Whether the recovery still has this stop in hand, so the alarm waits (see the module's words)."""
    pending_since = service.__dict__.setdefault('output_limit_pending', {})
    try:
        verdict, since = _recovery(seat, state)
    except Exception:  # what the recovery's reading cannot say is not in its hand: the alarm goes as before
        verdict, since = 'declined', None
    now = post.now()
    if verdict in ('pending', 'claiming'):
        # Both wait WAIT_SECONDS from the pass that first saw this stop: a claim whose door delivery was interrupted
        # stays at 'off' before 'delivered', and the recovery never advances it, so it would otherwise wait for good.
        first = pending_since.get(seat.name)
        if first is None or first[0] != key:
            pending_since[seat.name] = first = (key, now)
        return (now - first[1]).total_seconds() < WAIT_SECONDS
    pending_since.pop(seat.name, None)
    if verdict == 'delivered':
        return (now - since).total_seconds() < WAIT_SECONDS
    return False  # declined, or held


def step(service, seat) -> None:
    """Tell the systems seats once of a standing stop the recovery has not taken, through the alarm road; a cleared
    stop clears its alarm. While the recovery has it in hand nothing is told and nothing is cleared."""
    from . import alarms
    key, state = _stop(seat, service.store)
    if key is None:
        service.__dict__.setdefault('output_limit_pending', {}).pop(seat.name, None)
    elif _waits(service, seat, key, state):
        key = alarms.UNKNOWN
    text = (LINE.format(seat=seat.name) + '. Notification only: nothing was typed. A systems seat nudges it or starts '
            'it fresh.')
    standing = key is not None and key is not alarms.UNKNOWN
    alarms.notify(service, seat.name, False if standing else None, key, text, channel=CHANNEL,
                  ship=service.ship, up=service.systems_recipients(seat) if standing else None,
                  accepts=(lambda saved: saved == text) if standing else None)
