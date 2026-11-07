"""JSON-lines records: malformed or missing sources are unknown, never empty usage."""

from datetime import datetime, timezone
import json
from pathlib import Path


class RecordError(OSError):
    """A record file that cannot be decoded or is inconsistent."""


def entries(record: Path) -> list[dict]:
    """Read complete entries, retaining the existing tolerance for one unfinished tail."""
    try:
        raw = record.read_text()
    except UnicodeError as error:
        raise RecordError('record is not UTF-8 JSON lines') from error
    lines, out = raw.splitlines(), []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as error:
            if out and index == len(lines)-1 and not raw.endswith('\n'):
                continue  # the writer may still be appending this final entry
            raise RecordError(f'malformed JSON record at line {index+1}') from error
        if not isinstance(entry, dict):
            raise RecordError(f'non-object JSON record at line {index+1}')
        out.append(entry)
    if not out:
        raise RecordError('record has no readable entries yet')
    return out


def when(value: str | None) -> datetime | None:
    """An ISO timestamp as an aware datetime; a bare one is read as UTC."""
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise RecordError('invalid record timestamp') from error
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
