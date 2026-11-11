"""One matching rule for timestamped input evidence from an already owned record."""

from datetime import datetime
from math import floor
from pathlib import Path

from .harness import get
from .harness.base import Harness


def same_text(recorded: str, wanted: str) -> bool:
    """The native input boundary rule, shared by receipts and native identity projection."""
    return recorded.strip() == wanted.strip()


def confirmed(reader: str | Harness, record: Path, text: str, since: datetime, *, before: datetime | None = None) -> str | None:
    """Exact fresh acceptance, queueing or consumption; untimed history proves nothing."""
    harness = get(reader) if isinstance(reader, str) else reader
    resolution = getattr(harness, "record_time_resolution", 0.0)
    if resolution > 0:
        # Native precision affects only this comparison, never the saved attempt.
        since = datetime.fromtimestamp(floor(since.timestamp() / resolution) * resolution, since.tzinfo)
    queued = False
    for item in harness.inputs(record):
        if item.at is None or item.at < since or not same_text(item.text, text):
            continue
        if before is not None and (item.at>=before or item.consumed_at is not None and item.consumed_at>=before):
            continue
        if item.disposition in ('refused','uncertain'):
            return item.disposition
        elif item.kind == "accepted":
            return "delivered"
        elif item.kind == "consumed" and item.consumed_at is not None and item.consumed_at >= item.at:
            return "delivered"
        elif item.kind == "queued":
            queued=True
    return 'queued' if queued else None
