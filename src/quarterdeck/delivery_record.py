"""Receipt checks for attempts recorded before the durable paste journal."""

from datetime import datetime

from . import input_receipt, post, record_owner
from .seat import Seat


def receipt(reader, record, message: post.Message, since: datetime) -> str | None:
    if record is None:
        return None
    try:
        return input_receipt.confirmed(reader, record, post.typed([message]), since)
    except OSError:
        return None


def receipt_anywhere(seat: Seat, message: post.Message, since: datetime) -> str | None:
    """Acceptance in the current or a historically owned record, read by its saved reader."""
    results=set()
    for owned in record_owner.records(seat):
        if owned.reader is None:
            continue
        result = receipt(owned.reader, owned.path, message, since)
        if result:
            results.add(result)
    settled=results-{'queued'}
    if 'uncertain' in settled or len(settled)>1:
        return 'uncertain'
    return next(iter(settled)) if settled else 'queued' if results else None
