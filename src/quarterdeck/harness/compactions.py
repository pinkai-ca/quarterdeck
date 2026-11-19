"""A complete current-record snapshot and opaque native work frontier for fresh readiness.

The caller supplies adapter interpretation. The generic lifecycle never parses program rows.
"""

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path

from .json_record import _held, _parsed, _read_lock, _stamp, snapshot_bytes
from . import record_file
from ..record_location import resolve
from .types import State


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def identity(row: dict) -> str:
    return digest(json.dumps(row, sort_keys=True, separators=(',', ':')).encode())


@dataclass(frozen=True)
class PrefixProof:
    record: Path
    source: Path
    stamp: tuple
    previous: tuple | None = None
    bound: int | None = None

    def matches(self, extent, expected, current_digest):
        if type(extent) is not int or extent < 0:
            return False
        bound = record_file.LINES_BOUND if self.bound is None else self.bound
        with record_file.Opened(self.source, bound) as stream:
            if resolve(self.record) != self.source or _stamp(stream.stat) != self.stamp:
                raise record_file.RecordError('record changed after observing its compactions')
            if extent > self.stamp[2]:
                found = None
            elif extent == self.stamp[2]:
                found = current_digest
            elif self.previous and extent == self.previous[0]:
                found = self.previous[1]
            else:
                hasher, left = hashlib.sha256(), extent
                while left:
                    data = stream.read(min(left, 64 * 1024))
                    if not data:
                        raise record_file.RecordError('record ended during its prefix proof')
                    hasher.update(data)
                    left -= len(data)
                found = hasher.hexdigest()
            named, info = record_file.open_record(self.source)
            try:
                if (resolve(self.record) != self.source or _stamp(info) != self.stamp
                        or _stamp(record_file.restat(stream.descriptor)) != self.stamp):
                    raise record_file.RecordError('record changed during its prefix proof')
            finally:
                os.close(named)
        return found is not None and found == expected


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
    prefix: PrefixProof | None = field(default=None, repr=False, compare=False)
    state: State | None = field(default=None, repr=False)

    def matches_prefix(self, extent, expected) -> bool:
        if self.prefix is None:
            raise record_file.RecordError('no guarded native prefix proof')
        return self.prefix.matches(extent, expected, self.digest)


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


def read(record: Path, interpreter, reader, *, parse=_parsed, bound=None) -> Compactions:
    """The record's compaction reading. ``interpreter()`` makes the adapter's history, fed rows in turn; it is kept
    with the record (json_record's ``interpreters``) and fed only the rows an append adds, so a changed record is not
    interpreted whole again. A history it refused stays refused until the record is rewritten."""
    try:
        with _read_lock:
            parsed = parse(record)
            if reader in parsed.compactions:
                return parsed.compactions[reader]
            if not parsed.ends_newline or parsed.skipped:
                raise ValueError('incomplete or malformed native history')
            extent, checksum = parsed.total_bytes, parsed.raw_digest
            if checksum is None:
                raw = snapshot_bytes(parsed)
                extent, checksum = len(raw), digest(raw)
            prefix = PrefixProof(Path(record), parsed.source or resolve(record), parsed.stamp, parsed.previous_prefix, bound)
            kept = parsed.interpreters.get(reader)
            if kept is None or kept[1] > len(parsed.rows):
                kept = interpreter(), 0, None
            interpret, count, refused = kept
            if refused is None:
                try:
                    interpret.feed(row for _, row in parsed.rows[count:])
                except (ValueError, TypeError, KeyError, AttributeError):
                    refused = 'native compaction history unreadable or incomplete'
            if _held(record, parsed) and parsed.ends_newline:
                parsed.interpreters[reader] = interpret, len(parsed.rows), refused
            if refused is not None:
                raise ValueError(refused)
            history = interpret.history
            if not history.conversation or not history.turn or history.origin is None:
                raise ValueError('native conversation origin not proved')
            state, _ = reader.read_snapshot(record, parsed)
            found = Compactions(history.conversation, history.count, None, extent, checksum,
                                digest(''.join(history.frontier).encode()), history.turn,
                                history.complete, history.last_call, history.origin, prefix, state)
            parsed.compactions[reader] = found
            return found
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        # Fixed reasons only; native content/ids never become diagnostics.
        return Compactions(reason='native compaction history unreadable or incomplete')
