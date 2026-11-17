"""A complete current-record snapshot and opaque native work frontier for fresh readiness.

The caller supplies adapter interpretation. The generic lifecycle never parses program rows.
"""

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
from pathlib import Path

from .json_record import _entry, _parsed, _read_lock, _snapshots, snapshot_bytes
from .types import State


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def identity(row: dict) -> str:
    return digest(json.dumps(row, sort_keys=True, separators=(',', ':')).encode())


@dataclass(frozen=True)
class Compactions:
    conversation: str | None = None
    count: int | None = None
    reason: str | None = 'compaction reading unsupported'
    extent: int = 0
    digest: str | None = None
    frontier: str | None = None
    turn: str | None = None
    complete: bool = False
    last_call: str | None = None
    origin: datetime | None = None
    raw: bytes = field(default=b'', repr=False)
    state: State | None = field(default=None, repr=False)


class History:
    def __init__(self):
        self.conversation = None
        self.count = 0
        self.frontier = []
        self.turn = None
        self.complete = False
        self.last_call = None
        self.origin = None

    def work(self, row, *, turn=False, call=None):
        key = identity(row)
        self.frontier.append(key)
        if turn:
            self.turn, self.complete, self.last_call = key, False, None
        if call is not None:
            if not isinstance(call, str) or not call:
                raise ValueError('native call identity missing')
            self.last_call = call


def read(record: Path, interpreter, reader) -> Compactions:
    """The record's compaction reading. ``interpreter()`` makes the adapter's history, fed rows in turn; it is kept
    with the record (json_record's ``interpreters``) and fed only the rows an append adds, so a changed record is not
    interpreted whole again. A history it refused stays refused until the record is rewritten."""
    try:
        with _read_lock:
            parsed = _parsed(record)
            if reader in parsed.compactions:
                return parsed.compactions[reader]
            raw = snapshot_bytes(parsed)  # this reading's bytes, in hand: digested whole, never parsed for it
            if not raw or not raw.endswith(b'\n') or parsed.skipped:
                raise ValueError('incomplete or malformed native history')
            kept = parsed.interpreters.get(reader)
            if kept is None or kept[1] > len(parsed.rows):
                kept = interpreter(), 0, None
            interpret, count, refused = kept
            if refused is None:
                try:
                    interpret.feed(row for _, row in parsed.rows[count:])
                except (ValueError, TypeError, KeyError, AttributeError):
                    refused = 'native compaction history unreadable or incomplete'
            if _snapshots.get(Path(record)) is _entry(parsed) and parsed.ends_newline:
                parsed.interpreters[reader] = interpret, len(parsed.rows), refused
            if refused is not None:
                raise ValueError(refused)
            history = interpret.history
            if not history.conversation or not history.turn or history.origin is None:
                raise ValueError('native conversation origin not proved')
            state, _ = reader.read_snapshot(record, parsed)
            found = Compactions(history.conversation, history.count, None, len(raw), digest(raw),
                                digest(''.join(history.frontier).encode()), history.turn,
                                history.complete, history.last_call, history.origin, raw, state)
            parsed.compactions[reader] = found
            return found
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        # Fixed reasons only; native content/ids never become diagnostics.
        return Compactions(reason='native compaction history unreadable or incomplete')
