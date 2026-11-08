"""Isolate delivery items: a failed transport cannot poison another item's paste."""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import pending
from .door import Door
from .post import Message

if TYPE_CHECKING:
    from .seat import Seat
    from .service import Service


def deliver(service: Service, seat: Seat, messages: list[Message], resend: bool = False) -> str:
    # Persisted attempts, rather than an in-memory retry counter, decide priority.
    # Untried items get a usable door before previously withheld items. Every item
    # gets its own paste/check/submit and exact record-confirmation evidence.
    if resend:
        pending.explicit_resend(service, seat, messages)
    latest = service.attempts.last_results(seat.name)
    ordered = sorted(messages, key=lambda m: latest.get(m.id) == "withheld")
    lines = []
    for message in ordered:
        lines.append(service._deliver(seat, [message], resend))
        reading = Door(seat).reading()
        if not reading.open and reading.clears != "dismissable":
            break  # foreign/uncertain text is an unavoidable hold, never bypassed
    return "\n".join(lines)
