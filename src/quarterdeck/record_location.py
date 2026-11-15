"""Logical record names survive retirement without rewriting their ledger keys.

Adapters select native transcript bundles. This module knows only the framework's
seat home and retained home locations; it does not interpret a program's layout.
"""

from pathlib import Path


def retained_home(home: Path) -> Path | None:
    if (home.name == 'home' and home.parent.parent.name == 'seats'
            and home.parents[2].name == 'runtime'):
        return home.parents[2] / 'retired' / 'hand-transcripts' / home.parent.name / 'home'
    return None


def destination(record: Path) -> Path | None:
    record = Path(record)
    for home in record.parents:
        kept = retained_home(home)
        if kept is not None:
            return kept / record.relative_to(home)
    return None


def resolve(record: Path) -> Path:
    """Prefer a present original; an absent source may have been atomically retired."""
    record = Path(record)
    if record.exists():
        return record
    kept = destination(record)
    return kept if kept is not None and kept.is_file() else record


def retired(record: Path) -> bool:
    record = Path(record)
    for home in record.parents:
        if (home.name == 'home' and home.parent.parent.name == 'hand-transcripts'
                and home.parents[2].name == 'retired' and home.parents[3].name == 'runtime'):
            return True
    return resolve(record) != record


def records(home: Path, pattern: str) -> list[Path]:
    """An adapter supplies its glob; return retained sources under their logical names."""
    root = retained_home(home)
    if root is None:
        return []
    return [home / path.relative_to(root) for path in root.glob(pattern) if path.is_file()]
