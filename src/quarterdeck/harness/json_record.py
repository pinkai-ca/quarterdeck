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
import hashlib
import json
import os
from pathlib import Path
import time
from threading import RLock
from contextvars import ContextVar
from contextlib import contextmanager
from typing import TYPE_CHECKING
from . import reading_scope, record_file
from ..record_location import resolve

if TYPE_CHECKING:
    from .base import State, Turn


from .record_file import RecordError  # noqa: E402  (one class for every reader, re-exported here as before)


def records_matching(home: Path, pattern: str) -> list[Path]:
    """Every record file under a home that matches ``pattern``, oldest written first."""
    return sorted((p for p in home.glob(pattern) if p.is_file()), key=lambda p: p.stat().st_mtime)


@dataclass
class _Snapshot:
    """A record's reading: a cached entry, which a later reading resumes from, or the view one reading returns.

    In the generic service reader a cached entry keeps no text and no lines (``raw`` empty, ``rows`` empty): only the proof of its
    complete-lines prefix (its byte length and a sha256 over it), its counts, its last rows, its skipped lines and its
    readers' and inputs' running state. A view carries one reading's bytes and the lines it decoded, and parses every
    line only for a caller that asks for them all (_Rows). Where rows are kept (keep_rows: the accounting child), the
    cached entry keeps its text and every line and is itself the view, as before. Claude streaming keeps
    bounded native rows and a raw digest in both modes."""
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
    interpreters: dict = field(default_factory=dict)  # each reader's compaction history: (interpreter, rows, refusal)
    prefix_bytes: int = 0  # bytes up to the last newline
    prefix_sha256: object = None  # a sha256 over those bytes: its digest proves a later prefix, its copy extends it
    total_bytes: int = 0
    row_count: int = 0  # rows on complete lines
    last_row: tuple | None = None  # the last row, a final unterminated one included
    last_complete_row: tuple | None = None
    ends_newline: bool = False
    line_count: int = 0  # lines as str.splitlines counts them, a final unterminated one included
    data: bytes | None = None  # a view's bytes, never a cached entry's
    kept: '_Snapshot | None' = None  # a view's cached entry
    source: Path | None = None  # a view's record, read again only for a view served from the cache
    raw_digest: str | None = None
    claude_full: bool = False  # a stable ordinary Claude read after a per-row or shape refusal
    previous_prefix: tuple | None = None
    retained_bytes: int = 0
    projected_bytes: int | None = None
    decoded_bytes: int | None = None  # decoded charge for the complete lines, carried on an exact append
    retained_cost: int | None = None  # Claude streams keep a charged projection


_snapshots = OrderedDict()
# The cache's bound is its COUNT of entries: 96, four times today's 24 seats and the people's records, so the records
# the seats are reading (18 on 17 November 2026) never push each other out. Record bytes are the wrong unit for it: an
# entry keeps no text, and its resident cost per byte of record ran from 0.02 (Codex) to 1.94 (Claude), measured on a
# copy of the crew's records; the two idle Codex records held 109 MB of the old 256 MiB bound and cost 1.7 MB.
_CACHE_COUNT = 96
# Ordinary entries count their record bytes toward the 768 MiB safety ceiling. Claude streams have a separate
# aggregate charge for projections and reader state. For ordinary entries, today's mix of
# 326 MB costs about 140 to 280 MB resident; at Claude's worst 1.94 B per byte the ceiling costs about 1.5 GB.
_CACHE_BYTES_CEILING = 768 * 1024 * 1024
_CACHE_BYTES = _CACHE_BYTES_CEILING
# Keep ordinary records and Claude projections in separate budgets. Two at the source limit fit.
_CLAUDE_CACHE_BYTES = 1536 * record_file.MiB
_CLAUDE_CACHE_COUNT = 16
# A read in passing (passing()) admits no new entry: a sweep of many records, such as the seat-token fallback reading
# every record a seat ever owned, is read whole and dropped, so it never pushes the working set out.
_PASSING = ContextVar('json_record_passing', default=False)
# Where rows are kept (keep_rows), the bound and count the cache had before, since each entry costs about 5 times its
# record's bytes there.
_KEPT_BYTES = 128 * 1024 * 1024
_KEPT_COUNT = 16
_KEEP_ROWS = False
_TIMESTAMP_TICK_NS = 2_000_000_000  # same conservative local metadata window as the post listing
_read_lock = RLock()


def keep_rows() -> None:
    """Keep every record's text and parsed lines in the cache, as before: the accounting child, whose jobs read the same
    record's lines through several readers in turn, sets this at its start."""
    global _KEEP_ROWS
    with _read_lock:
        _KEEP_ROWS = True
        _snapshots.clear()


@contextmanager
def passing():
    """Read records without admitting new ones to the cache (_PASSING): an entry already held is used and kept."""
    token = _PASSING.set(True)
    try:
        yield
    finally:
        _PASSING.reset(token)


def cache_size() -> tuple[int, int]:
    """The cached records and their bytes on disk, for the service's log."""
    with _read_lock:
        return len(_snapshots), sum(entry.stamp[2] for entry in _snapshots.values())


def release(record: Path, *, keep_projection: bool = False) -> None:
    """Release a sweep's parsed text, optionally keeping a charged Claude projection."""
    with _read_lock:
        key = Path(record)
        entry = _snapshots.get(key)
        if not keep_projection or entry is None or entry.retained_cost is None:
            _snapshots.pop(key, None)


def _stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _same_name(source: Path, opened) -> bool:
    """The path still names the file that was read: a record renamed over meanwhile is read again."""
    try:
        named = os.lstat(source)
    except OSError:
        return False
    return (named.st_dev, named.st_ino) == (opened.st_dev, opened.st_ino)


def _parsed(record: Path) -> _Snapshot:
    with _read_lock:
        return _parse(record)


def _parse(record: Path) -> _Snapshot:
    """Private immutable views; every changed/racy source is byte-checked before prefix reuse."""
    source = resolve(record)
    record = Path(record)
    cached = _snapshots.pop(record, None)  # a failed read cannot preserve a certified success
    if cached is not None and cached.raw_digest is not None:
        cached = None  # a projection is never a generic full-record cache entry
    for attempt in range(3):
        started = time.time_ns()
        descriptor, info = record_file.open_record(source)  # never a link, a fifo or a swapped file
        try:
            checked = time.time_ns()
            stamp = _stamp(info)
            if (attempt == 0 and cached is not None and cached.stamp == stamp and cached.certified_at is not None
                    and cached.certified_at <= started <= checked):
                _snapshots[record] = cached
                return cached if _KEEP_ROWS else _view(source, cached, None, _partial(cached), cached.row_count)
            data = record_file.read_from(descriptor, info, source, record_file.LINES_BOUND)
            after = record_file.restat(descriptor)
        finally:
            os.close(descriptor)
        if _stamp(after) == stamp and _same_name(source, after):
            break
        # A live writer can append between stat and read. Re-read fresh bytes,
        # never a cached fallback; a continuously changing source still refuses.
    else:
        raise RecordError('record changed while being read')
    finished = time.time_ns()
    certified = started if max(stamp[-2:]) + _TIMESTAMP_TICK_NS < started <= checked <= finished else None
    if _KEEP_ROWS:
        return _parse_text(record, data, stamp, certified, cached)
    return _parse_bytes(source, record, data, stamp, certified, cached)


def _lines(text: str, number: int) -> tuple[list, list]:
    """Rows and skipped lines of ``text``, numbered after ``number``. Only complete physical lines are retained by a
    cache: an unterminated final object may become invalid when extended, and is parsed again when it is."""
    rows, skipped = [], []
    for number, line in enumerate(text.splitlines(), number + 1):
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
    return rows, skipped


def _decoded(data) -> str:
    try:
        return bytes(data).decode('utf-8')
    except UnicodeError as error:
        raise RecordError("record is not UTF-8 JSON lines") from error


def _store(record: Path, entry: _Snapshot, member: bool) -> None:
    """Keep ``entry`` as the most recent, then let the least recent go past the count or the ceiling. ``member``: the
    record was held before this reading; a read in passing (passing()) admits only members."""
    if _PASSING.get() and not member:
        return
    projected = entry.retained_cost is not None
    if projected:
        bound, count = _CLAUDE_CACHE_BYTES, _CLAUDE_CACHE_COUNT
    else:
        bound = _KEPT_BYTES if _KEEP_ROWS else _CACHE_BYTES
        count = _KEPT_COUNT if _KEEP_ROWS else _CACHE_COUNT
    if _cache_cost(entry) > bound:
        return
    _snapshots[record] = entry
    sizes = [(path, _cache_cost(value)) for path, value in _snapshots.items()
             if projected == (value.retained_cost is not None)]
    total, population = sum(size for _, size in sizes), len(sizes)
    for path, size in sizes:
        if population <= count and total <= bound:
            break
        _snapshots.pop(path)
        population -= 1
        total -= size


def _cache_cost(entry: _Snapshot) -> int:
    return entry.stamp[2] if entry.retained_cost is None else entry.retained_cost


def refresh_stamp(entry, stamp, certified):
    if entry.stamp != stamp:
        entry.compactions.clear()  # its guarded extent proof is tied to the old observation
    entry.stamp, entry.certified_at = stamp, certified


def _parse_bytes(source: Path, record: Path, data: bytes, stamp, certified, cached) -> _Snapshot:
    """A changed record: its old complete-lines prefix proved by its byte length and sha256, and only the lines after
    it decoded; a prefix that does not match is parsed whole. The cached entry keeps no text and no lines."""
    view = memoryview(data)
    reuse = (cached is not None and cached.prefix_sha256 is not None and cached.prefix_bytes <= len(data)
             and hashlib.sha256(view[:cached.prefix_bytes]).digest() == cached.prefix_sha256.digest())
    if reuse:
        offset, number, base = cached.prefix_bytes, cached.complete_lines, cached.row_count
        skipped = [item for item in cached.skipped if item[0] <= number]
        hasher, last_complete = cached.prefix_sha256.copy(), cached.last_complete_row
    else:
        offset, number, base, skipped, hasher, last_complete = 0, 0, 0, [], hashlib.sha256(), None
    text = _decoded(view[offset:])
    tail, more = _lines(text, number)
    if not base and not tail and text.strip():
        raise RecordError("no JSON object lines in this record: not a record this reader can read")
    if reuse and len(data) == cached.total_bytes:  # the same bytes under a new stamp: the same entry, its state kept
        refresh_stamp(cached, stamp, certified)
        _store(record, cached, True)
        return _view(source, cached, data, tail, base)
    complete_end = data.rfind(b'\n') + 1
    complete_chars = text.rfind('\n') + 1
    complete_lines = number + len(text[:complete_chars].splitlines())
    hasher.update(view[offset:complete_end])
    whole = [row for row in tail if row[0] <= complete_lines]
    entry = _Snapshot('', stamp, [], skipped + more, certified, 0, complete_lines,
                      prefix_bytes=complete_end, prefix_sha256=hasher, total_bytes=len(data),
                      row_count=base + len(whole), last_row=tail[-1] if tail else (last_complete if reuse else None),
                      last_complete_row=whole[-1] if whole else last_complete,
                      ends_newline=data.endswith(b'\n'), line_count=number + len(text.splitlines()))
    if reuse and cached.ends_newline and cached.prefix_bytes == cached.total_bytes and entry.ends_newline:
        _carry(cached, entry, whole, more)
    _store(record, entry, cached is not None)
    return _view(source, entry, data, tail, base)


def _carry(cached: _Snapshot, entry: _Snapshot, added: list, skipped: list) -> None:
    """Bring the running state forward over an append, from the rows decoded now: each reader up to date is fed the new
    rows, and each input result is kept while no new row is relevant to it. Anything behind, or touched, is dropped,
    and rebuilt from every row when next asked. Without the lines, a state left behind could only catch up whole."""
    for cls, (reader, count, seen) in cached.readers.items():
        if count != cached.row_count or seen != len(cached.skipped):
            continue
        if skipped:
            reader.tools_known = False  # as read_snapshot does for a newly skipped line
        reader.take_rows(row for _, row in added)
        entry.readers[cls] = reader, entry.row_count, len(entry.skipped)
    for reader, (interpreter, count, refused) in cached.interpreters.items():
        if count != cached.row_count:
            continue
        if refused is None and not skipped:
            try:
                interpreter.feed(row for _, row in added)
            except (ValueError, TypeError, KeyError, AttributeError):
                refused = 'native compaction history unreadable or incomplete'
        entry.interpreters[reader] = interpreter, entry.row_count, refused or ('skipped lines' if skipped else None)
    for read, (result, count, seen, relevant) in cached.inputs.items():
        if (count == cached.row_count and seen == len(cached.skipped) and not skipped
                and not any(relevant(row) for _, row in added)):
            entry.inputs[read] = result, entry.row_count, len(entry.skipped), relevant


def _partial(entry: _Snapshot) -> list:
    """A cached entry's unterminated final row, which its counts leave out."""
    last = entry.last_row
    return [last] if last is not None and last[0] > entry.complete_lines else []


def _view(source: Path, entry: _Snapshot, data: bytes | None, tail: list, base: int) -> _Snapshot:
    from dataclasses import replace
    view = replace(entry, data=data, kept=entry, source=source)  # readers, inputs, compactions: the entry's own dicts
    view.rows = _Rows(base, tail, entry.last_row, lambda: _lines(_decoded(_bytes(view)), 0)[0])
    return view


def _bytes(snapshot: _Snapshot) -> bytes:
    """A reading's bytes: a view's own, or, for a view served from the cache, the record read again at that stamp."""
    if snapshot.data is not None:
        return snapshot.data
    if snapshot.kept is None:
        return snapshot.raw.encode('utf-8')
    source = snapshot.source
    descriptor, info = record_file.open_record(source)
    try:
        if _stamp(info) != snapshot.stamp:
            raise RecordError('record changed while being read')
        data = record_file.read_from(descriptor, info, source, record_file.LINES_BOUND)
        if _stamp(record_file.restat(descriptor)) != snapshot.stamp:
            raise RecordError('record changed while being read')
    finally:
        os.close(descriptor)
    snapshot.data = data
    return data


def snapshot_bytes(snapshot: _Snapshot) -> bytes:
    """The bytes a snapshot was read from, for a reader that digests them whole (harness/compactions)."""
    return _bytes(snapshot)


class _Rows:
    """A view's rows: those its prefix holds are counted, not kept; the lines this reading decoded are in hand. A slice
    from the counted rows onward, the length and the last row are served without parsing; anything else parses the
    whole reading once (``load``)."""

    def __init__(self, base: int, tail: list, last, load):
        self.base, self.tail, self.last, self._load, self._all = base, tail, last, load, None

    def all(self) -> list:
        if not self.base:
            return self.tail
        if self._all is None:
            self._all = self._load()
        return self._all

    def __len__(self):
        return self.base + len(self.tail)

    def __bool__(self):
        return len(self) > 0

    def __iter__(self):
        return iter(self.all())

    def __getitem__(self, index):
        if isinstance(index, slice):
            start, stop, step = index.indices(len(self))
            if step == 1 and start >= self.base:
                return self.tail[start - self.base:max(stop - self.base, 0)]
            return self.all()[index]
        if index in (-1, len(self) - 1) and len(self):
            return self.tail[-1] if self.tail else self.last
        if 0 <= index - self.base < len(self.tail):
            return self.tail[index - self.base]
        return self.all()[index]


def _parse_text(record: Path, data: bytes, stamp, certified, cached) -> _Snapshot:
    """keep_rows: the cache keeps each record's text and every line, and a changed record reuses its text prefix."""
    raw = _decoded(data)
    if cached is not None and raw == cached.raw:
        refresh_stamp(cached, stamp, certified)
        _store(record, cached, True)
        return cached
    rows, skipped, offset, number = [], [], 0, 0
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
    added, more = _lines(raw[offset:], number)
    rows += added
    skipped += more
    if raw.strip() and not rows:
        raise RecordError("no JSON object lines in this record: not a record this reader can read")
    result = _Snapshot(raw, stamp, rows, skipped, certified, complete_end, complete_lines,
                       ends_newline=raw.endswith('\n'), line_count=len(raw.splitlines()),
                       last_row=rows[-1] if rows else None)
    if cached is not None and cached.complete_end == len(cached.raw) and offset == len(cached.raw) and raw.endswith('\n'):
        result.readers, result.inputs = dict(cached.readers), dict(cached.inputs)
        result.interpreters = dict(cached.interpreters)
    _store(record, result, cached is not None)
    return result


def _object_lines(record: Path, skip=None, *, parse=_parsed):
    """Only native readers receive these private rows; they do not mutate source objects. Every row, parsed whole
    where the cache keeps none."""
    with _read_lock:
        snapshot = parse(record)
        if skip is not None:
            for diagnostic in snapshot.skipped:
                skip(*diagnostic)
        return list(snapshot.rows)


def _entry(snapshot: _Snapshot) -> _Snapshot:
    """The cached entry a reading belongs to: a view's, or the snapshot itself where rows are kept."""
    return snapshot.kept if snapshot.kept is not None else snapshot


def _held(record, snapshot):
    return _snapshots.get(Path(record)) is _entry(snapshot)


def entries(record: Path, *, parse=_parsed) -> list[dict]:
    """Every parseable object line of the record, in order; the rest is skipped."""
    return [entry for _, entry in object_lines(record, parse=parse)]


def object_lines(record: Path, skip=None, *, parse=_parsed) -> list[tuple[int, dict]]:
    """Keep original line numbers; optional diagnostics contain no source values."""
    return deepcopy(_object_lines(record, skip, parse=parse))


def input_rows(record: Path, kinds: tuple[str, ...], label: str, evidence, *, parse=_parsed):
    rows = _object_lines(record, evidence.skip, parse=parse)
    if not rows:
        evidence.skip(0, 'record', 'no recognized native history')
    if rows and not any(row.get('type') in kinds for _, row in rows):
        raise RecordError(f"no {label} entries in this record: another program's file")
    return rows


def input_reader(relevant, *, parse=_parsed):
    """An adapter classifies which appended native rows can change its input evidence."""
    def decorate(read):
        @wraps(read)
        def cached(record):
            with _read_lock:
                snapshot = parse(record)
                old = snapshot.inputs.get(read)  # (result, rows, skipped lines, relevant)
                if (old is not None and old[2] == len(snapshot.skipped)
                        and not any(relevant(row) for _, row in snapshot.rows[old[1]:])):
                    result = old[0]
                else:
                    result = read(record) if parse is _parsed else read(record, snapshot)
                if _held(record, snapshot) and snapshot.ends_newline:
                    snapshot.inputs[read] = result, len(snapshot.rows), len(snapshot.skipped), relevant
                result = deepcopy(result)
                last = snapshot.line_count
                if (last and not snapshot.ends_newline
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


def ended_at_limit(reasons, ended) -> bool | None:
    """Whether the latest of ``ended`` turns stopped at the output limit, by its kept end reason; None when the reader
    has no measured form, no turn has ended, or the turn kept no reason."""
    if reasons is None or not ended or ended[-1].end_reason is None:
        return None
    return ended[-1].end_reason in reasons


class JsonLinesReader(RecordReader):
    """The shared walk, tool lifetime and live state of a JSON-lines conversation."""

    kinds: tuple[str, ...] = ()  # format-identification markers, not an exhaustive native row taxonomy
    label: str = "program"
    # The native end reasons that mean the program's output limit, as measured in real records; None: not measured,
    # so whether a turn stopped there is unknown.
    limit_reasons: frozenset[str] | None = None

    @classmethod
    def identifies(cls, entry: dict) -> bool:
        """Native envelope identification; adapters need not have a top-level type."""
        return entry.get("type") in cls.kinds

    parse_snapshot = staticmethod(_parsed)

    @classmethod
    def read(cls, record: Path):
        with _read_lock:
            return reading_scope.read(cls, record, lambda: cls.read_snapshot(record, cls.parse_snapshot(record)))

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
        if _held(record, snapshot) and snapshot.ends_newline:
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
        rows = [row for _, row in (snapshot.rows if snapshot is not None else _object_lines(record, self.unknown_tools, parse=self.parse_snapshot))]
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
            last_write = datetime.fromtimestamp(resolve(self.record).stat().st_mtime, timezone.utc)
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
                     failure=self.turns[-1].failure if self.open is None and self.turns else None,
                     ended_at_limit=ended_at_limit(self.limit_reasons, self.turns if self.open is None else ()))
