"""JSON-lines records: a line the reader cannot parse is skipped, a file it cannot read is an error.

A record is written by a program that is still running: its last line may be half written, and a
version the adapter has not seen may add a line of a shape it does not know. Neither makes the
record unreadable, so neither blinds the seat; they are skipped. A file that is not text, or not
there, is a RecordError, and the reading says so instead of showing nothing.
"""

from datetime import datetime, timezone
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field
from functools import wraps
import json
from pathlib import Path
import time
from threading import RLock
from typing import TYPE_CHECKING
from . import reading_scope
from ..record_location import resolve

if TYPE_CHECKING:
    from .base import State, Turn


class RecordError(OSError):
    """A record file that cannot be read or decoded at all."""


def records_matching(home: Path, pattern: str) -> list[Path]:
    """Every record file under a home that matches ``pattern``, oldest written first."""
    return sorted((p for p in home.glob(pattern) if p.is_file()), key=lambda p: p.stat().st_mtime)


@dataclass
class _Snapshot:
    raw: str
    stamp: tuple
    rows: list
    skipped: list
    certified_at: int | None
    complete_end: int
    complete_lines: int
    readers: dict = field(default_factory=dict)
    inputs: dict = field(default_factory=dict)
    compactions: dict = field(default_factory=dict)


_snapshots = OrderedDict()
_CACHE_BYTES = 128 * 1024 * 1024
_TIMESTAMP_TICK_NS = 2_000_000_000  # same conservative local metadata window as the post listing
_read_lock = RLock()


def release(record: Path) -> None:
    """A background sweep need not retain parsed native text after its record."""
    with _read_lock:
        _snapshots.pop(Path(record), None)


def _stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _parsed(record: Path) -> _Snapshot:
    with _read_lock:
        return _parse(record)


def _parse(record: Path) -> _Snapshot:
    """Private immutable views; every changed/racy source is byte-checked before prefix reuse."""
    source = resolve(record)
    record = Path(record)
    cached = _snapshots.pop(record, None)  # a failed read cannot preserve a certified success
    for attempt in range(3):
        started = time.time_ns()
        info = source.stat()
        checked = time.time_ns()
        stamp = _stamp(info)
        if (attempt == 0 and cached is not None and cached.stamp == stamp and cached.certified_at is not None
                and cached.certified_at <= started <= checked):
            _snapshots[record] = cached
            return cached
        try:
            raw = source.read_bytes().decode('utf-8')
        except UnicodeError as error:
            raise RecordError("record is not UTF-8 JSON lines") from error
        if _stamp(source.stat()) == stamp:
            break
        # A live writer can append between stat and read. Re-read fresh bytes,
        # never a cached fallback; a continuously changing source still refuses.
    else:
        raise RecordError('record changed while being read')
    finished = time.time_ns()
    certified = started if max(stamp[-2:]) + _TIMESTAMP_TICK_NS < started <= checked <= finished else None
    if cached is not None and raw == cached.raw:
        cached.stamp, cached.certified_at = stamp, certified
        _snapshots[record] = cached
        return cached
    rows, skipped, offset, number = [], [], 0, 0
    # Only complete physical lines are retained. An unterminated final object may
    # become invalid when extended; a partial final row must be reparsed on recovery.
    if cached is not None:
        end = cached.complete_end
        if raw.startswith(cached.raw[:end]):
            offset = end
            number = cached.complete_lines
            rows = (list(cached.rows) if end == len(cached.raw) else
                    [(n, row) for n, row in cached.rows if n <= number])
            skipped = [item for item in cached.skipped if item[0] <= number]
    complete_end = raw.rfind('\n') + 1
    complete_lines = number + len(raw[offset:complete_end].splitlines())
    for number, line in enumerate(raw[offset:].splitlines(), number + 1):
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
    if raw.strip() and not rows:
        raise RecordError("no JSON object lines in this record: not a record this reader can read")
    result = _Snapshot(raw, stamp, rows, skipped, certified, complete_end, complete_lines)
    if cached is not None and cached.complete_end == len(cached.raw) and offset == len(cached.raw) and raw.endswith('\n'):
        result.readers, result.inputs = dict(cached.readers), dict(cached.inputs)
    if stamp[2] <= _CACHE_BYTES:
        _snapshots[record] = result
        while len(_snapshots) > 16 or sum(value.stamp[2] for value in _snapshots.values()) > _CACHE_BYTES:
            _snapshots.popitem(last=False)
    return result


def _object_lines(record: Path, skip=None):
    """Only native readers receive these private rows; they do not mutate source objects."""
    snapshot = _parsed(record)
    if skip is not None:
        for diagnostic in snapshot.skipped:
            skip(*diagnostic)
    return snapshot.rows


def entries(record: Path) -> list[dict]:
    """Every parseable object line of the record, in order; the rest is skipped."""
    return [entry for _, entry in object_lines(record)]


def object_lines(record: Path, skip=None) -> list[tuple[int, dict]]:
    """Keep original line numbers; optional diagnostics contain no source values."""
    return deepcopy(_object_lines(record, skip))


def input_rows(record: Path, kinds: tuple[str, ...], label: str, evidence):
    rows = _object_lines(record, evidence.skip)
    if not rows:
        evidence.skip(0, 'record', 'no recognized native history')
    if rows and not any(row.get('type') in kinds for _, row in rows):
        raise RecordError(f"no {label} entries in this record: another program's file")
    return rows


def input_reader(relevant):
    """An adapter classifies which appended native rows can change its input evidence."""
    def decorate(read):
        @wraps(read)
        def cached(record):
            with _read_lock:
                snapshot = _parsed(record)
                old = snapshot.inputs.get(read)
                if (old is not None and old[2] == len(snapshot.skipped)
                        and not any(relevant(row) for _, row in snapshot.rows[old[1]:])):
                    result = old[0]
                else:
                    result = read(record)
                if _snapshots.get(Path(record)) is snapshot and snapshot.raw.endswith('\n'):
                    snapshot.inputs[read] = result, len(snapshot.rows), len(snapshot.skipped)
                result = deepcopy(result)
                last = len(snapshot.raw.splitlines())
                if (snapshot.raw and not snapshot.raw.endswith('\n')
                        and not any(line == last for line, _, _ in snapshot.skipped)):
                    result.skip(last, 'record', 'unterminated final row')
                return result
        return cached
    return decorate


def when(value: str | None) -> datetime | None:
    """An ISO timestamp as an aware datetime; a bare one is read as UTC; one that is not a timestamp is None."""
    if not value or not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


class RecordReader:
    """Read live state and ended turns together; the format supplies construction and state."""

    @classmethod
    def read(cls, record: Path) -> tuple["State", list["Turn"]]:
        reader = cls(record)
        return reader.state(), reader.turns

    @classmethod
    def read_state(cls, record: Path) -> "State":
        return cls.read(record)[0]

    @classmethod
    def read_turns(cls, record: Path) -> list["Turn"]:
        return cls.read(record)[1]


class JsonLinesReader(RecordReader):
    """The shared walk, tool lifetime and live state of a JSON-lines conversation."""

    kinds: tuple[str, ...] = ()  # format-identification markers, not an exhaustive native row taxonomy
    label: str = "program"

    @classmethod
    def identifies(cls, entry: dict) -> bool:
        """Native envelope identification; adapters need not have a top-level type."""
        return entry.get("type") in cls.kinds

    @classmethod
    def read(cls, record: Path):
        with _read_lock:
            return reading_scope.read(cls, record, lambda: cls.read_snapshot(record, _parsed(record)))

    @classmethod
    def read_snapshot(cls, record: Path, snapshot: _Snapshot):
        """State/turn facts from these exact bytes; caller holds the reader lock."""
        previous = snapshot.readers.get(cls)
        if previous is None:
            reader = cls(record, snapshot)
        else:
            reader, count, skipped = previous
            if snapshot.skipped[skipped:]:
                reader.tools_known = False
            reader.take_rows(row for _, row in snapshot.rows[count:])
        if _snapshots.get(Path(record)) is snapshot and snapshot.raw.endswith('\n'):
            snapshot.readers[cls] = reader, len(snapshot.rows), len(snapshot.skipped)
        return reader.state(), list(reader.turns)

    def __init__(self, record: Path, snapshot=None):
        self.record = record
        self.turns = []
        self.open = None
        self.completed_turns = []
        self.pending = {}
        self.live_tools = {}
        self.live_tool_starts = {}
        self.tools_known = True
        self.queued = []
        self.queue_error = None
        self.consumed = []
        self.model = self.effort = self.last_input = self.last_input_at = None
        if snapshot is not None:
            for diagnostic in snapshot.skipped:
                self.unknown_tools(*diagnostic)
        rows = [row for _, row in (snapshot.rows if snapshot is not None else _object_lines(record, self.unknown_tools))]
        if rows and not any(self.identifies(row) for row in rows):
            raise RecordError(f"no {self.label} entries in this record: another program's file")
        self.take_rows(rows)

    def take_rows(self, rows):
        for entry in rows:
            try:
                self.take(entry)
            except (TypeError, ValueError, AttributeError, KeyError):
                self.tools_known = False
                continue  # an unknown row does not hide the readable rows beside it

    def unknown_tools(self, *diagnostic) -> None:
        self.tools_known = False

    def open_call(self, call, name, at) -> None:
        """Track outstanding calls beyond accounting boundaries; a new turn cannot end a tool."""
        if not isinstance(call, str) or not call or not isinstance(name, str) or not name or name == '?':
            self.tools_known = False
        if call in self.live_tools and self.live_tools[call] != name:
            self.tools_known = False
        self.live_tools[call] = name
        self.live_tool_starts[call] = at
        self.pending[call] = (name, at)

    def start_turn(self, turn: dict) -> None:
        self.pending.clear()
        self.open = turn

    def close_turn(self) -> dict:
        turn, self.open = self.open, None
        self.pending.clear()  # unfinished calls belong to the turn that ended
        return turn

    def record_turn(self, turn: "Turn", completed_at: datetime | None) -> None:
        self.turns.append(turn)
        if completed_at is not None:
            self.completed_turns.append(turn)  # accounting fallbacks are not completion evidence

    def close_tool(self, call: str, at: datetime | None, failed: bool | None, default: str = "?") -> None:
        from .base import ToolCall

        self.live_tools.pop(call, None)
        self.live_tool_starts.pop(call, None)
        name, started = self.pending.pop(call, (default, None))
        seconds = (at - started).total_seconds() if at and started else None
        if self.open is not None:
            self.open["tools"].append(ToolCall(name, seconds, failed))

    def state(self) -> "State":
        from .base import State
        from .types import ActiveTool

        try:
            last_write = datetime.fromtimestamp(self.record.stat().st_mtime, timezone.utc)
        except FileNotFoundError:
            last_write = None
        tool = next(iter(self.pending.values()), None) if self.open is not None else None
        return State(turn_open=self.open is not None, open_tool=tool[0] if tool else None,
                     open_tool_since=tool[1] if tool else None, last_write=last_write,
                     last_input=self.last_input, last_input_at=self.last_input_at, model=self.model,
                     effort=self.effort, queued=tuple(self.queued), consumed=tuple(self.consumed), turns=len(self.turns),
                     completed_turns=tuple(self.completed_turns),
                     pending_tools=tuple(self.live_tools.values()) if self.tools_known else None,
                     active_tools=tuple(ActiveTool(name, self.live_tool_starts.get(call))
                                        for call, name in self.live_tools.items()),
                     queue_error=self.queue_error,
                     failure=self.turns[-1].failure if self.open is None and self.turns else None)
