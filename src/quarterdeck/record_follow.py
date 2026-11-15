"""The status line's failure scans read each seat's JSON-lines record from where they last stopped.

A collection asks every seat's record for its state (turn_failure.current, and the queue error of a program
that reads one). The shared snapshot cache in json_record holds 16 records and 128 MB, fewer than a crew's
records, so each collection read them whole again. Here one entry per seat keeps the harness's own reader, fed
every complete line so far, and the record's identity, the offset after its last complete line and a short tail
of the bytes before that offset. A later reading feeds that reader only the lines appended since.

An entry starts over whenever it cannot prove the record only grew: the record's device or inode changed, its
size is below the offset, the tail no longer matches, or the seat has a new launch or a new record pointer. A
record that ends mid-line, a reader that is not a JSON-lines reader, or any bytes this path cannot read take
the whole reading, as before, so the state given is always the one a whole reading gives.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from threading import Lock

TAIL = 64  # bytes before the offset, compared on each reading to prove the record was only appended to

_followed: dict[str, '_Follow'] = {}
_lock = Lock()


@dataclass
class _Follow:
    key: tuple  # the seat's launch identity and record pointer
    identity: tuple  # (st_dev, st_ino)
    offset: int  # bytes of complete lines fed to the reader
    tail: bytes  # the TAIL bytes before offset
    lines: int  # complete physical lines fed, for the reader's own numbering of skipped lines
    reader: object


def reader_class(harness):
    """The harness's JSON-lines reader class, or None where its records are read another way."""
    from .harness.json_record import JsonLinesReader
    read = getattr(harness, 'reader', None)
    cls = getattr(read, '__self__', None)
    return cls if isinstance(cls, type) and issubclass(cls, JsonLinesReader) else None


def forget(seat_name: str) -> None:
    with _lock:
        _followed.pop(seat_name, None)


def state(seat, record: Path):
    """The seat's record state, as ``seat.harness.state(record)`` gives it, read from where it last stopped."""
    cls = reader_class(seat.harness)
    if cls is None:
        return seat.harness.state(record)
    from .turn_failure import identity
    key = (json.dumps(identity(seat), sort_keys=True), str(record))
    with _lock:
        try:
            followed = _follow(seat.name, cls, key, Path(record))
        except (OSError, ValueError, UnicodeError):
            _followed.pop(seat.name, None)  # what it held can no longer be proved
            followed = None
        if followed is None:
            return seat.harness.state(record)  # the whole reading, which raises what it raises
        return followed.reader.state()


def _follow(name, cls, key, record):
    from .record_location import resolve
    source = resolve(record)
    previous = _followed.get(name)
    with open(source, 'rb') as stream:
        info = os.fstat(stream.fileno())
        identity = (info.st_dev, info.st_ino)
        if previous is not None and not (
                previous.key == key and previous.identity == identity and info.st_size >= previous.offset
                and _tail(stream, previous.offset) == previous.tail):
            previous = None  # it cannot be proved to have only grown: start over
        start = previous.offset if previous is not None else 0
        stream.seek(start)
        data = stream.read(info.st_size - start)
    end = data.rfind(b'\n') + 1
    if data[end:].strip():
        # A final line still being written: the whole reading gives its exact state. What is complete is kept.
        kept = (_feed(previous, data[:end], start) if previous is not None
                else _start(cls, record, key, identity, data[:end], start) if end else None)
        if kept is not None:
            _followed[name] = kept
        else:
            _followed.pop(name, None)
        return None
    if previous is None:
        followed = _start(cls, record, key, identity, data[:end], start)
        if followed is None:
            _followed.pop(name, None)
            return None
    else:
        followed = _feed(previous, data[:end], start)
    _followed[name] = followed
    return followed


def _rows(text: str, first: int):
    rows, skipped = [], []
    for number, line in enumerate(text.splitlines(), first + 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            skipped.append((number, 'json', 'invalid JSON'))
            continue
        if isinstance(entry, dict):
            rows.append((number, entry))
        else:
            skipped.append((number, 'non-object', 'expected JSON object'))
    return rows, skipped, len(text.splitlines())


def _start(cls, record, key, identity, complete: bytes, start: int):
    from .harness.json_record import RecordError, _Snapshot
    text = complete.decode('utf-8')
    rows, skipped, count = _rows(text, 0)
    if text.strip() and not rows:
        return None  # the whole reading refuses such a record, and says why
    snapshot = _Snapshot('', (), rows, skipped, None, 0, count)
    try:
        reader = cls(record, snapshot)
    except RecordError:
        return None
    end = start + len(complete)
    return _Follow(key, identity, end, complete[-TAIL:], count, reader)


def _feed(followed: _Follow, complete: bytes, start: int) -> _Follow:
    if complete:
        text = complete.decode('utf-8')
        rows, skipped, count = _rows(text, followed.lines)
        if skipped:
            followed.reader.tools_known = False  # as a snapshot's new skipped lines do
        followed.reader.take_rows(row for _, row in rows)
        followed.lines += count
        followed.offset = start + len(complete)
        followed.tail = (followed.tail + complete)[-TAIL:]
    return followed


def _tail(stream, offset: int) -> bytes:
    length = min(TAIL, offset)
    stream.seek(offset - length)
    return stream.read(length)
