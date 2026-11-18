"""Read the identity needed for a launch from the seat's already joined record."""

from dataclasses import dataclass
import json
from pathlib import Path
from uuid import UUID

from . import record_file
from ..ship import ShipError


NEW_CONVERSATION = 'started a new conversation'
FIRST_ROW_BYTES = 64 * 1024


class Unproved(ShipError):
    def __init__(self, seat: str, reason: str = 'the existing conversation could not be identified', *, launch=None):
        self.reason, self.launch = reason, launch
        super().__init__(f'{seat}: {reason}. Use qd fresh {seat} to start a new conversation carrying its handoff.')


@dataclass(frozen=True)
class Target:
    id: str
    record: Path


def identify(harness, record: Path) -> str | None:
    """Use the ordinary reader, with a bounded identity-only lookup past its normal read limit."""
    try:
        identity = harness.session_id(record)
    except record_file.PastBound:
        if harness.name not in ('claude', 'codex', 'grok', 'pi'):
            return None
        identity = _oversized_id(harness.name, record)
    if (isinstance(identity, str) and 0 < len(identity) <= 512 and identity.isprintable()
            and identity == identity.strip() and not identity.startswith('-')):
        return identity
    return None


def _uuid(value: str) -> UUID | None:
    try:
        result = UUID(value)
    except ValueError:
        return None
    return result if str(result) == value else None


def _oversized_id(program: str, record: Path) -> str | None:
    with record_file.Opened(record, FIRST_ROW_BYTES) as opened:
        if program == 'claude' and record.parent.parent.name == 'projects' and _uuid(record.stem):
            # Native Claude conversation filenames are their session IDs.
            return record.stem
        row, newline, _ = opened.read(FIRST_ROW_BYTES).partition(b'\n')
    if not newline:
        return None
    row = json.loads(row)
    if not isinstance(row, dict):
        return None
    if program == 'claude':
        return row.get('sessionId')
    if program == 'codex' and row.get('type') == 'session_meta':
        payload = row.get('payload')
        return payload.get('id') if isinstance(payload, dict) else None
    if program == 'pi' and row.get('type') == 'session':
        return row.get('id')
    if program == 'grok':
        from .grok_record import update
        folder = record.parent.name
        identity = _uuid(folder)
        if (identity is not None and identity.version == 7 and update(row) is not None
                and row['params'].get('sessionId') == folder):
            return folder
    return None
