"""What a record says about a delivery: whether this very message was taken, and whether a queued one was read."""

from datetime import datetime

from . import post, record_owner
from .door import Door
from .seat import Seat


def read_now(seat: Seat, message: post.Message) -> bool:
    """Whether a message once queued has been read: it is an input or a consumed entry, no longer only queued."""
    try:
        state, turns = seat.harness.read(seat.record())
    except OSError:
        return False
    flat = " ".join(" ".join(t.split()) for t in [*(x.input for x in turns), *state.consumed, state.last_input or ""])
    return message.short in flat and message.short not in " ".join(state.queued)


def _carries(message: post.Message, text: str | None) -> bool:
    flat = " ".join((text or "").split())
    return message.short in flat and " ".join(message.body.split()) in flat


def taken(seat: Seat, message: post.Message, since: datetime) -> bool:
    """Whether the record shows this very message, by its id and body, as an input after ``since``;
    for a program whose record is opaque, whether its transcript echoes it."""
    if not seat.harness.record_confirms:
        return any(_carries(message, e) for e in Door(seat).echoes())
    record = seat.record()
    if record is None:
        return False
    try:
        state, turns = seat.harness.read(record)
    except OSError:
        return False
    inputs = [t.input for t in turns if t.started and t.started >= since]
    if state.turn_open and state.last_input_at and state.last_input_at >= since:
        inputs.append(state.last_input)
    return any(_carries(message, text) for text in inputs + list(state.queued) + list(state.consumed))


def taken_anywhere(seat: Seat, message: post.Message, since: datetime) -> bool:
    """``taken``, over every record the seat has owned: a message typed before a fresh start or a change
    of program may stand in an older record than the one the seat runs now."""
    if taken(seat, message, since):
        return True
    current = seat.record()
    for owned in record_owner.records(seat):
        if owned.path == current:
            continue
        try:
            state, turns = owned.read()
        except OSError:
            continue
        inputs = [t.input for t in turns if t.started and t.started >= since] + list(state.consumed)
        if any(_carries(message, text) for text in inputs):
            return True
    return False
