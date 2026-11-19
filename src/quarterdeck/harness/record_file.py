"""One way to open and read a seat's program record: never through a link, never another file, never without end.

A record lies in a seat's home, which a jailed seat can write: a name there may be swapped for a link to a file of the
operator's, or for a fifo, between the moment a reader checks it and the moment it opens it; and a record may grow
without bound, or grow fast, while it is read. So every reader of a record opens it here:

- with O_NOFOLLOW, so a link is refused, and O_NONBLOCK, so a fifo never holds the reader;
- then checks the open descriptor with fstat: a regular file, owned by this user, and the very file the path named a
  moment before (device and inode), so a file swapped in between is refused;
- and reads at most a bound. A record past it is not read: the reading says so (``PastBound``), and never reports a
  state taken from part of the record, or a default, as the seat's.

The bounds (proposed 15 November, from the largest real records then; La'an's word decides them):
JSON-lines records were at most 74.1 MiB (a Codex rollout) and 61.0 MiB (a Claude conversation), and one parse costs
about 7.4 ms and 11.6 MB of memory per MiB; SQLite records at most 198.6 MiB with a 4.0 MiB log (an OpenCode
database), and one copy about 3.1 times the file in memory.
"""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
import os
from pathlib import Path
import stat
import threading

from .. import seat_files

MiB = 1024 * 1024
LINES_BOUND = 128 * MiB  # one read of a JSON-lines record: 1.7 times the largest, about 1 s and 1.5 GB at the bound
CLAUDE_STREAM_BOUND = 1024 * MiB  # the Claude stream holds a bounded projection of these raw bytes
CLAUDE_STREAM_ROWS = 700_000
CLAUDE_STREAM_BYTES = 256 * MiB
CLAUDE_STREAM_MEMORY = 768 * MiB  # per-source decoded projection, reader and index allowance
CLAUDE_DENSE_ROWS_PER_MIB = 630
CLAUDE_DENSE_RETAINED_PER_MIB = 890_500
CLAUDE_DENSE_MEMORY_PER_MIB = 2_650_000
DATABASE_BOUND = 512 * MiB  # one read of a SQLite record and its log together: 2.5 times the largest
_CHUNK = MiB
# A read past this waits for any other such read in the process: a read near the bound costs some 1.5 GB, and the
# service reads every seat. JSON-lines reads are one at a time already (json_record's lock); SQLite reads were not.
LARGE = 32 * MiB
_large = threading.Lock()


class RecordError(OSError):
    """A record file that cannot be read or decoded at all."""


class Refused(RecordError):
    """A record name that is not this user's own regular file: a link, a fifo, another's file, or a swapped file."""


class PastBound(RecordError):
    """A record larger than one read may take: not read. ``size`` and ``bound`` carry the figures."""

    size: int | None = None
    bound: int | None = None


def _size(count: int) -> str:
    return f'{count / MiB:.1f} MiB'


def open_record(path: Path) -> tuple[int, os.stat_result]:
    """A descriptor for ``path`` and its fstat, once proven this user's own regular file, the one the path named, with
    no other name. In a seat's folder every folder on the way is opened without following a link, from the seat
    folder down (``seat_files``' walk), so a folder the jail swapped for a link is refused, not followed; elsewhere
    (a retired record, the ship's own) the last name is. The caller closes the descriptor."""
    path = Path(path)
    found = seat_files.seat_root(path)
    folder = None
    try:
        if found is not None and found[1]:
            root, parts = found
            try:
                folder = seat_files._walk(root, parts[:-1], create=False)
            except seat_files.LinkRefused:
                raise Refused(f'{path.name}: a link on the way to the record; not read') from None
            except NotADirectoryError:
                raise Refused(f'{path.name}: not a folder on the way to the record; not read') from None
            name = parts[-1]
        else:
            name = str(path)
        try:
            named = os.stat(name, dir_fd=folder, follow_symlinks=False)
        except FileNotFoundError:
            raise
        except OSError as error:
            raise RecordError(f'{path.name}: cannot be read ({error.strerror})') from None
        if stat.S_ISLNK(named.st_mode):
            raise Refused(f'{path.name}: a link, not a record; not read')
        if not stat.S_ISREG(named.st_mode):
            raise Refused(f'{path.name}: not a regular file; not read')  # never opened: a fifo, a socket, a device
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=folder)
        except FileNotFoundError:
            raise
        except OSError as error:
            raise Refused(f'{path.name}: cannot be opened as a record ({error.strerror}); not read') from None
    finally:
        if folder is not None:
            os.close(folder)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise Refused(f'{path.name}: not a regular file; not read')
        if opened.st_uid != os.getuid():
            raise Refused(f'{path.name}: not this user\'s file; not read')
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise Refused(f'{path.name}: replaced while being opened; not read')
        if opened.st_nlink > 1:
            raise Refused(f'{path.name}: a file with another name elsewhere ({opened.st_nlink} links); not read')
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, opened


def restat(descriptor: int) -> os.stat_result:
    """The open record's fstat again, after a read: a reader compares it with the one it opened with."""
    return os.fstat(descriptor)


def past_bound(path: Path, size: int, bound: int) -> PastBound:
    error = PastBound(f'{Path(path).name}: record past the read bound ({_size(size)}, bound {_size(bound)}); not read')
    error.size, error.bound = size, bound  # as figures, for the ledger's unread span
    return error


def read_from(descriptor: int, opened: os.stat_result, path: Path, bound: int | None, offset: int = 0) -> bytes:
    """The bytes of an open record from ``offset`` to its end, at most ``bound`` of them: a record whose remainder is
    larger, by its fstat or by what the read finds as it grows, is ``PastBound`` and the rest is never read. A read
    larger than LARGE waits for any other such read of this process: one at a time near the bound. ``None``: no bound,
    for the ship's own files, never a seat's."""
    if bound is not None and opened.st_size - offset > bound:
        raise past_bound(path, opened.st_size, bound)
    with (_large if opened.st_size - offset > LARGE else nullcontext()):
        parts, taken = [], 0
        os.lseek(descriptor, offset, os.SEEK_SET)
        while True:
            part = os.read(descriptor, _CHUNK if bound is None else min(_CHUNK, bound + 1 - taken))
            if not part:
                return b''.join(parts)
            parts.append(part)
            taken += len(part)
            if bound is not None and taken > bound:
                raise past_bound(path, offset + taken, bound)


def read_bytes(path: Path, bound: int | None) -> tuple[bytes, os.stat_result]:
    """A whole record, and the fstat it was opened with: opened as ``open_record`` does, read as ``read_from`` does."""
    descriptor, opened = open_record(path)
    try:
        return read_from(descriptor, opened, path, bound), opened
    finally:
        os.close(descriptor)


class Opened:
    """An open record for a reader that reads it in parts (a tail, a span from an offset): every read counts against
    one bound, and a reader that would take more is ``PastBound``. Use as a context manager."""

    def __init__(self, path: Path, bound: int):
        self.path = Path(path)
        self.bound = bound
        self.descriptor, self.stat = open_record(self.path)
        self.taken = 0

    def __enter__(self) -> Opened:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1

    def seek(self, offset: int) -> None:
        os.lseek(self.descriptor, offset, os.SEEK_SET)

    def read(self, count: int) -> bytes:
        if self.taken + count > self.bound:
            count = self.bound + 1 - self.taken
        data = os.read(self.descriptor, max(count, 0)) if count > 0 else b''
        self.taken += len(data)
        if self.taken > self.bound:
            raise past_bound(self.path, self.stat.st_size, self.bound)
        return data


WARN_AT = 0.75  # a record past three quarters of its bound is named, so its seat is started fresh at a natural break


@dataclass(frozen=True)
class ProjectionUsage:
    """Scalar evidence from a guarded stream. No record text is retained here."""

    stamp: tuple[int, ...]
    lines: int | None = None
    retained: int | None = None
    memory: int | None = None
    exhausted: str | None = None


@dataclass(frozen=True)
class Pressure:
    size: int
    raw_bound: int
    first_bound: int
    resource: str = 'raw size'
    exhausted: str | None = None
    label: str | None = None


def _first_projection_limit(sample: ProjectionUsage | None, info) -> tuple[int, str, str | None]:
    """Project the first limit by density. Only a matching observation proves an exhausted resource."""
    current = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    matching_file = (sample is not None and sample.stamp[:2] == current[:2]
                     and 0 < sample.stamp[2] <= info.st_size)
    if matching_file and sample.stamp == current and sample.exhausted is not None:
        return info.st_size, sample.exhausted, sample.exhausted
    candidates = [(CLAUDE_STREAM_BOUND, 'raw size')]
    defaults = (CLAUDE_DENSE_ROWS_PER_MIB, CLAUDE_DENSE_RETAINED_PER_MIB, CLAUDE_DENSE_MEMORY_PER_MIB)
    resources = ('physical lines', 'retained bytes', 'retained memory')
    budgets = (CLAUDE_STREAM_ROWS, CLAUDE_STREAM_BYTES, CLAUDE_STREAM_MEMORY)
    for name, limit, default, field in zip(resources, budgets, defaults, ('lines', 'retained', 'memory')):
        observed = getattr(sample, field) if matching_file and sample.exhausted is None else None
        if observed is None:
            input_bytes, consumed = MiB, default
        else:
            input_bytes, consumed = sample.stamp[2], observed
        if consumed:
            # The compatibility reader remains eligible through the ordinary JSON-lines limit.
            candidates.append((max(LINES_BOUND, limit * input_bytes // consumed), name))
    first, resource = min(candidates, key=lambda item: item[0])
    return first, resource, None


def bound_of(path: Path, reader: str | None = None) -> int:
    """The raw bound for the established reader, or the ordinary bound for the file format."""
    if reader == 'claude':
        return CLAUDE_STREAM_BOUND
    return DATABASE_BOUND if Path(path).suffix == '.db' else LINES_BOUND


def _claude_pointer(seat, path: Path) -> bool:
    """An owned pointer gets the Claude limit only when its retained reader is Claude."""
    if getattr(getattr(seat, 'harness', None), 'name', None) == 'claude':
        from .. import record_owner

        try:
            return record_owner.lookup_reader(seat, path) == 'claude'
        except (OSError, ValueError, seat_files.LinkRefused):
            pass
    return False


def _named_stat(path: Path):
    """One file's stat by its name, found by the same walk as the read: no link on the way from the seat folder, and
    a link, fifo, socket or device at the name gives no figure (None), never a followed one. Nothing there at all
    raises FileNotFoundError."""
    found = seat_files.seat_root(path)
    folder = None
    try:
        if found is not None and found[1]:
            folder = seat_files._walk(found[0], found[1][:-1], create=False)
            name = found[1][-1]
        else:
            name = str(path)
        named = os.stat(name, dir_fd=folder, follow_symlinks=False)
    except FileNotFoundError:
        raise
    except (OSError, seat_files.LinkRefused):  # a link on the way: no figure
        return None
    finally:
        if folder is not None:
            os.close(folder)
    return named if stat.S_ISREG(named.st_mode) else None


def _named_size(path: Path) -> int | None:
    info = _named_stat(path)
    return None if info is None else info.st_size


def size_of(path: Path) -> int | None:
    """What one read of the record would take, by the read's own walk and never a read: a database counts with its
    log. None when it cannot be told without following a link, or nothing is there."""
    path = Path(path)
    try:
        size = _named_size(path)
    except FileNotFoundError:
        return None
    if size is None or path.suffix != '.db':
        return size
    try:
        log = _named_size(Path(f'{path}-wal'))
    except FileNotFoundError:
        return size
    return None if log is None else size + log


def alarm(seat: str, path: Path) -> str | None:
    """The status line's word on a seat whose record nears or passes its read bound; None while it is well within."""
    return _raw_alarm(seat, size_of(path), bound_of(path))


def _raw_alarm(seat: str, size: int | None, bound: int) -> str | None:
    if size is None or size < WARN_AT * bound:
        return None
    if size > bound:
        return f'{seat}: record past the read bound ({_size(size)}, bound {_size(bound)}); not read; start it fresh'
    return f'{seat}: record {_size(size)} of its {_size(bound)} read bound; start it fresh at a natural break'


def pressure(seat, path: Path, observation=None) -> Pressure | None:
    """Read status from the owned file's metadata and the last accounted Claude observation."""
    if not _claude_pointer(seat, path):
        raw = bound_of(path)
        size = size_of(path)
        return None if size is None else Pressure(size, raw, raw)
    try:
        info = _named_stat(path)
    except FileNotFoundError:
        return None
    if info is None:
        return None
    sample = observation(path) if observation is not None else None
    first, resource, exhausted = _first_projection_limit(sample, info)
    return Pressure(info.st_size, bound_of(path, 'claude'), first, resource, exhausted, label='Claude')


def alarms(seats, owned=None, observation=None) -> list[str]:  # law: nothing-waits-silently
    """One line for each seat whose current record nears or passes its read bound. A seat the operator owns (the
    desk, the office) is named from its owned record pointer (``owned(seat)``), never by reading its conversation:
    those are the longest conversations on the ship, and so the likeliest to reach the bound first."""
    lines = []
    for seat in seats:
        try:
            human = seat.session.human
        except (AttributeError, KeyError, ValueError):
            human = getattr(getattr(seat, 'spec', None), 'human_session', False)
        try:
            if human:
                if owned is None:
                    continue
                record = owned(seat)
            elif callable(getattr(seat, 'record', None)):
                record = seat.record()
            else:
                continue
        except (OSError, ValueError):
            continue  # the seat's own reading says why its record cannot be named
        if record is None or (reading := pressure(seat, record, observation)) is None:
            continue
        if reading.exhausted is not None:
            lines.append(f'{seat.name}: record past the read bound ({reading.label} {reading.exhausted} limit exceeded). '
                         'Reading unknown. Start it fresh.')
        elif reading.resource != 'raw size' and reading.size >= WARN_AT * reading.first_bound:
            lines.append(f'{seat.name}: Claude {reading.resource} may reach its limit at about '
                         f'{_size(reading.first_bound)}; start it fresh at a natural break')
        if (line := _raw_alarm(seat.name, reading.size, reading.raw_bound)) is not None:
            lines.append(line)
    return lines
