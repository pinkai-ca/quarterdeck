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


def ignored(harness: Harness, record: Path, payload: str, since: datetime) -> frozenset[str]:
    """The characters the adapter measured its program to drop from this payload as it records a paste."""
    drop = getattr(harness, "paste_ignored", None)  # a bare reader declares none
    return drop(payload, record, since) if drop is not None else frozenset()


def without(text: str, chars: frozenset[str]) -> str:
    return "".join(char for char in text if char not in chars) if chars else text


def same_input(recorded: str, forms: tuple[str, ...], ignoring: frozenset[str] = frozenset()) -> bool:
    """The recorded text is one of the forms, once the ignored characters are removed from both sides alike."""
    return any(same_text(without(recorded, ignoring), without(form, ignoring)) for form in forms)


def confirmed(reader: str | Harness, record: Path, text: str | tuple[str, ...], since: datetime) -> str | None:
    """Exact fresh acceptance, queueing or consumption; untimed history proves nothing.

    ``text`` may name several forms of one message, its first and second delivery: any of them is its receipt.
    """
    harness = get(reader) if isinstance(reader, str) else reader
    resolution = getattr(harness, "record_time_resolution", 0.0)
    if resolution > 0:
        # Native precision affects only this comparison, never the saved attempt.
        since = datetime.fromtimestamp(floor(since.timestamp() / resolution) * resolution, since.tzinfo)
    queued = False
    texts = (text,) if isinstance(text, str) else text
    forms = tuple(form for each in texts for form in recorded_forms(harness, record, each, since))
    ignoring = frozenset().union(*(ignored(harness, record, each, since) for each in texts))
    for item in harness.inputs(record):
        if item.at is None or item.at < since or not same_input(item.text, forms, ignoring):
            continue
        if item.kind == "accepted":
            return "delivered"
        if item.kind == "consumed" and item.consumed_at is not None and item.consumed_at >= item.at:
            return "delivered"
        if item.kind == "queued":
            queued = True
    return "queued" if queued else None
