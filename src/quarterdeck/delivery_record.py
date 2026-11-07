"""Reconcile legacy attempts that predate the exact transport journal."""

from datetime import datetime

from . import post
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

def taken(seat: Seat, message: post.Message, since: datetime) -> bool:
    """Whether the record shows this very message, by its id and body, as an input after ``since``;
    for a program whose record is opaque, whether its transcript echoes it."""
    wanted = " ".join(message.body.split())

    def carries(text: str | None) -> bool:
        flat = " ".join((text or "").split())
        return message.short in flat and wanted in flat

    if not seat.harness.record_confirms:
        return any(carries(e) for e in Door(seat).echoes())
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
    return any(carries(text) for text in inputs + list(state.queued) + list(state.consumed))
