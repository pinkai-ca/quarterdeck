"""Retained record readers, independent of a seat's currently selected program.

Only a proven join, saved launch command or saved reader binding establishes a reader.
An untyped pin is preserved as unknown; record contents never select an adapter.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import fcntl
import json
from pathlib import Path
from typing import Any, Iterator, TYPE_CHECKING

from . import harness as harnesses
from .harness.base import Harness, RecordError
from .harness.types import State, Turn

if TYPE_CHECKING:
    from .seat import Launched, Seat

REGISTRY = "record-readers.jsonl"


def registry(folder: Path) -> Path:
    """Reader registry in a seat's runtime folder."""
    return folder / REGISTRY


def launch_reader(launched: Launched | None) -> str | None:
    if launched is None:
        return None
    named = getattr(launched, "harness", None)
    if named is not None:
        return named if named in harnesses.NAMES else None
    argv = getattr(launched, "argv", ())
    if not argv:
        return None
    matches = [name for name in harnesses.NAMES if harnesses.get(name).owns_launch(argv)]
    return matches[0] if len(matches) == 1 else None


def events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        if any(not isinstance(row, dict) or not isinstance(row.get("record"), str) for row in rows):
            raise ValueError("invalid record ownership entry")
        return rows
    except (UnicodeError, ValueError) as error:
        raise RecordError(f"unreadable record ownership: {path.name}") from error


class _RegistryRows:
    def __init__(self, rows: list[dict[str, Any]], stamp: tuple[int, ...] | None = None):
        self.rows, self.stamp = rows, stamp
        self.by_record: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            self.by_record.setdefault(row["record"], []).append(row)

    def contains(self, row: dict[str, Any]) -> bool:
        # Compare whole rows: extra fields and untyped evidence keep their meaning.
        return row in self.by_record.get(row["record"], ())


_pass_rows: ContextVar[dict[Path, _RegistryRows] | None] = ContextVar("reader_pass_rows", default=None)


@contextmanager
def reader_pass() -> Iterator[None]:
    """Share registry membership only within one account invocation, even on failure."""
    token = _pass_rows.set({})
    try:
        yield
    finally:
        _pass_rows.reset(token)


def _registry_stamp(path: Path) -> tuple[int, ...] | None:
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _registry_rows(path: Path) -> _RegistryRows:
    cache = _pass_rows.get()
    if cache is None:
        return _RegistryRows(events(path))
    previous = cache.pop(path, None)
    try:
        before = _registry_stamp(path)
    except OSError:
        # A failed metadata check grants no reuse; keep the ordinary reader path.
        return _RegistryRows(events(path))
    if previous is not None and previous.stamp == before:
        cache[path] = previous
        return previous
    rows = _RegistryRows(events(path), before)
    try:
        after = _registry_stamp(path)
    except OSError:
        return rows
    # An unstable read keeps its ordinary result/error but cannot seed the cache.
    if before == after:
        cache[path] = rows
    return rows


def registry_events(folder: Path) -> list[dict[str, Any]]:
    """Read registry rows without modifying them; share only within an account pass."""
    return _registry_rows(registry(folder)).rows


def register(folder: Path, record: Path, name: str | None) -> None:
    row = {"record": str(record), "harness": name}
    path = registry(folder)
    if _registry_rows(path).contains(row):
        return
    cache = _pass_rows.get()
    if cache is not None:
        cache.pop(path, None)
    folder.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        # Another process may have appended since the read-only membership check.
        if row not in events(path):
            stream.write(json.dumps(row) + "\n")


def bindings(seat: Seat, *, publish=True) -> dict[Path, set[str]]:
    folder = seat.home.parent
    found: dict[Path, set[str]] = {}
    saved = registry_events(folder)
    registered = {Path(row["record"]) for row in saved}
    for row in [*saved, *events(folder / "record-launches.jsonl")]:
        names = found.setdefault(Path(row["record"]), set())
        if row.get("harness"):
            names.add(row["harness"])
    pinned = folder / "record"
    if pinned.is_file():
        path = Path(pinned.read_text().strip())
        names = found.setdefault(path, set())
        if path not in registered and not names and (name := launch_reader(seat.launched())):
            names.add(name)
        if publish:
            for name in names or {None}:
                register(folder, path, name)
    # The owned-path list proves seat ownership, never a program's file format.
    owned = seat.home / "conversations.owned"
    if owned.is_file():
        for line in owned.read_text().splitlines():
            if line.strip():
                found.setdefault(Path(line.strip()), set())
    return found


def pinned_reader(seat: Seat, record: Path) -> str | None:
    names = bindings(seat).get(record, set())
    return next(iter(names)) if len(names) == 1 and names <= set(harnesses.NAMES) else None


@dataclass(frozen=True)
class Owned:
    path: Path
    name: str | None
    reader: Harness | None
    reason: str | None = None
    transient: bool = False  # the ownership evidence itself could not be read; never settled, read again

    def read(self) -> tuple[State, list[Turn]]:
        if self.reader is None:
            raise RecordError(self.reason or f"record reader unknown: {self.path.name}; inspect with qd ledger")
        return self.reader.read(self.path)


def records(seat: Seat, *, publish=True) -> list[Owned]:
    """Known owned files, including missing/unknown ones, with their original adapters."""
    try:
        found = bindings(seat) if publish else bindings(seat, publish=False)
    except OSError as error:
        return [Owned(registry(seat.home.parent), None, None, f"{error}; inspect ownership for {seat.name} with qd watch {seat.name}",
                      transient=True)]
    known = {name for names in found.values() for name in names if name in harnesses.NAMES}
    if name := launch_reader(seat.launched()):
        known.add(name)
    readers = {name: seat.harness if name == seat.spec.harness else harnesses.get(name) for name in known}
    candidates: dict[Path, set[str]] = {}
    for name, reader in readers.items():
        # Shared global record lists are untyped: never claim foreign candidates.
        if reader.home_variable is None:
            continue
        for path in reader.records(seat.home):
            if path not in found:
                candidates.setdefault(path, set()).add(name)
    found.update(candidates)
    result = []
    for path, names in found.items():
        name = next(iter(names)) if len(names) == 1 and names <= set(harnesses.NAMES) else None
        if publish:
            register(seat.home.parent, path, name)
        result.append(Owned(path, name, readers.get(name),
                            f"record reader unknown or conflicting for {seat.name}, {path.name}; historical harness evidence required; inspect with qd watch {seat.name}" if name is None else None))
    return sorted(result, key=lambda item: str(item.path))
