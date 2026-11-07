"""JSON-lines records: a line the reader cannot parse is skipped, a file it cannot read is an error.

A record is written by a program that is still running: its last line may be half written, and a
version the adapter has not seen may add a line of a shape it does not know. Neither makes the
record unreadable, so neither blinds the seat; they are skipped. A file that is not text, or not
there, is a RecordError, and the reading says so instead of showing nothing.
"""

from datetime import datetime, timezone
import json
from pathlib import Path


class RecordError(OSError):
    """A record file that cannot be read or decoded at all."""


def entries(record: Path) -> list[dict]:
    """Every parseable object line of the record, in order; the rest is skipped."""
    try:
        raw = record.read_text()
    except UnicodeError as error:
        raise RecordError("record is not UTF-8 JSON lines") from error
    out = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue  # half written, or not JSON at all: not this reader's to interpret
        if isinstance(entry, dict):
            out.append(entry)
    if raw.strip() and not out:
        raise RecordError("no JSON object lines in this record: not a record this reader can read")
    return out


def when(value: str | None) -> datetime | None:
    """An ISO timestamp as an aware datetime; a bare one is read as UTC; one that is not a timestamp is None."""
    if not value or not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
