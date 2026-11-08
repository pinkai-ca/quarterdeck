"""One matching rule for timestamped input evidence from an already owned record."""

from datetime import datetime
from pathlib import Path

from .harness import get
from .harness.base import Harness


def confirmed(reader: str | Harness, record: Path, text: str, since: datetime) -> str | None:
    """Exact fresh acceptance, queueing or consumption; untimed history proves nothing."""
    harness = get(reader) if isinstance(reader, str) else reader
    queued = False
    for item in harness.inputs(record):
        if item.at is None or item.at < since or item.text.strip() != text.strip():
            continue
        if item.kind == "accepted":
            return "delivered"
        if item.kind == "consumed" and item.consumed_at is not None and item.consumed_at >= item.at:
            return "delivered"
        if item.kind == "queued":
            queued = True
    return "queued" if queued else None
