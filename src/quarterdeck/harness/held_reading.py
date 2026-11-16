"""Until when a record's turn state is unknown only for a time, as its reader says.

The accounting pass marks a record it read with an unknown turn state and does not read it again until its bytes
change. Most such unknowns hold until a new row is written, so that rule is right for them. Claude's notice window
is different: while the program may be answering its own notice, the turn reads unknown for a grace measured on
the reader's clock, and once the grace has passed the same bytes read idle. If no row ever follows the notice, the
mark would otherwise stand until the record changed or the service restarted, and the record's turns would stay
unaccounted.

This module is the service's alone. No reader imports it, so it is in no reader's stamp set, and it changes no
reading: it asks the reader instance that has just read the record when its unknown ends, by the reader's own
deadline and clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from . import claude
from .json_record import _parsed, _read_lock
from ..record_location import resolve


class Hold:
    """A mark to read again once ``until`` has passed on the clock ``reader`` judges its grace on, read when asked."""

    __slots__ = ('until', 'reader')

    def __init__(self, until: datetime, reader: type):
        self.until, self.reader = until, reader

    def due(self) -> bool:
        return self.reader.clock() >= self.until

    def __eq__(self, other):
        return isinstance(other, Hold) and (self.until, self.reader) == (other.until, other.reader)

    def __repr__(self):
        return f'Hold(until={self.until.isoformat()})'


def due(hold: Hold | None) -> bool:
    return hold is not None and hold.due()


def unknown_until(owned) -> Hold | None:
    """When the turn state ``owned``'s reader gave as unknown is known again without a new row, or None.

    None for every unknown that holds until the record changes, for every reader without such a window, and
    whenever the record cannot be read again exactly: such a mark keeps the ordinary rule.
    """
    reader = getattr(getattr(owned.reader, 'reader', None), '__self__', None)
    if reader is not claude._Reader:  # only Claude's reader has a time-bound unknown
        return None
    record = resolve(Path(owned.path))
    try:
        with _read_lock:
            parsed = _parsed(record)
            state, _ = reader.read_snapshot(record, parsed)  # advances the cached reader to these exact bytes
            found = parsed.readers.get(reader)
    except (OSError, ValueError):
        return None
    if found is None or found[1] != len(parsed.rows) or state.turn_open is not None:
        return None
    instance = found[0]
    at = instance.notice_at
    if not instance.answering_notice() or at is None or at.tzinfo is None:
        return None  # an untimed notice is unknown until a row follows it: the ordinary rule
    return Hold(at + timedelta(seconds=claude.NOTICE_GRACE_SECONDS), reader)
