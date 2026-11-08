"""Retained record readers, independent of a seat's currently selected program.

Only a proven join, saved launch command or saved reader binding establishes a reader.
An untyped legacy pin is preserved as unknown; record contents never select an adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import json
from pathlib import Path

from . import harness as harnesses
from .harness.base import RecordError

REGISTRY = 'record-readers.jsonl'


def launch_reader(launched) -> str | None:
    if launched is None:
        return None
    named = getattr(launched, 'harness', None)
    if named is not None:
        return named if named in harnesses.NAMES else None
    argv = getattr(launched, 'argv', ())
    if not argv:
        return None
    matches = [name for name in harnesses.NAMES if harnesses.get(name).owns_launch(argv)]
    return matches[0] if len(matches) == 1 else None


def events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        if any(not isinstance(row, dict) or not isinstance(row.get('record'), str) for row in rows):
            raise ValueError('invalid record ownership entry')
        return rows
    except (UnicodeError, ValueError) as error:
        raise RecordError(f'unreadable record ownership: {path.name}') from error


def register(folder: Path, record: Path, name: str | None) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    row = {'record': str(record), 'harness': name}
    with (folder / REGISTRY).open('a+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        if row not in events(folder / REGISTRY):
            stream.write(json.dumps(row) + '\n')


def bindings(seat) -> dict[Path, set[str]]:
    folder = seat.home.parent
    found: dict[Path, set[str]] = {}
    saved = events(folder / REGISTRY)
    registered = {Path(row['record']) for row in saved}
    for row in [*saved, *events(folder / 'record-launches.jsonl')]:
        names = found.setdefault(Path(row['record']), set())
        if row.get('harness'):
            names.add(row['harness'])
    pinned = folder / 'record'
    if pinned.is_file():
        path = Path(pinned.read_text().strip())
        names = found.setdefault(path, set())
        if path not in registered and not names and (name := launch_reader(seat.launched())):
            names.add(name)
        for name in names or {None}:
            register(folder, path, name)
    # The old generic list proves seat ownership, never a program's file format.
    legacy = seat.home / 'conversations.owned'
    if legacy.is_file():
        for line in legacy.read_text().splitlines():
            if line.strip():
                found.setdefault(Path(line.strip()), set())
    return found


def pinned_reader(seat, record: Path) -> str | None:
    names = bindings(seat).get(record, set())
    return next(iter(names)) if len(names) == 1 and names <= set(harnesses.NAMES) else None


@dataclass(frozen=True)
class Owned:
    path: Path
    name: str | None
    reader: object | None
    reason: str | None = None

    def read(self):
        if self.reader is None:
            raise RecordError(self.reason or 'record reader unknown')
        return self.reader.read(self.path)


def records(seat) -> list[Owned]:
    """Known owned files, including missing/unknown ones, with their original adapters."""
    try:
        found = bindings(seat)
    except OSError as error:
        return [Owned(seat.home.parent / REGISTRY, None, None, str(error))]
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
        register(seat.home.parent, path, name)
        result.append(Owned(path, name, readers.get(name),
                            'record reader unknown or conflicting; historical harness evidence required' if name is None else None))
    return sorted(result, key=lambda item: str(item.path))
