"""One matching rule for timestamped input evidence from an already owned record."""

from datetime import datetime
from math import floor
from pathlib import Path

from .harness import get
from .harness.base import Harness


def same_text(recorded: str, wanted: str) -> bool:
    """The native input boundary rule, shared by receipts and native identity projection."""
    return recorded.strip() == wanted.strip()


def recorded_forms(harness: Harness, record: Path, payload: str, since: datetime) -> tuple[str, ...]:
    """The payload as typed, and the one form the adapter measured its program to record it in, if any."""
    paste = getattr(harness, "recorded_paste", None)  # a bare reader declares no form, as with the resolution
    form = paste(payload, record, since) if paste is not None else payload
    return (payload,) if form == payload else (payload, form)


def same_input(recorded: str, forms: tuple[str, ...]) -> bool:
    return any(same_text(recorded, form) for form in forms)


def confirmed(reader: str | Harness, record: Path, text: str, since: datetime) -> str | None:
    """Exact fresh acceptance, queueing or consumption; untimed history proves nothing."""
    harness = get(reader) if isinstance(reader, str) else reader
    resolution = getattr(harness, "record_time_resolution", 0.0)
    if resolution > 0:
        # Native precision affects only this comparison, never the saved attempt.
        since = datetime.fromtimestamp(floor(since.timestamp() / resolution) * resolution, since.tzinfo)
    queued = False
    forms = recorded_forms(harness, record, text, since)
    for item in harness.inputs(record):
        if item.at is None or item.at < since or not same_input(item.text, forms):
            continue
        if item.kind == "accepted":
            return "delivered"
        if item.kind == "consumed" and item.consumed_at is not None and item.consumed_at >= item.at:
            return "delivered"
        if item.kind == "queued":
            queued = True
    return "queued" if queued else None
