"""Logical record names survive retirement; native bytes have one retained location.

Only the two hand transcript layouts have a retired counterpart. Readers keep
the original path as the ledger key. No symlink or edited native record is needed.
"""

from pathlib import Path


def destination(record: Path) -> Path | None:
    record = Path(record)
    for home in record.parents:
        if (home.name == 'home' and home.parent.parent.name == 'seats'
                and home.parent.parent.parent.name == 'runtime'):
            relative = record.relative_to(home)
            parts = relative.parts
            if (len(parts) == 2 and parts[0] == 'sessions' and record.suffix == '.jsonl'
                    or len(parts) == 3 and parts[0].startswith('data-')
                    and parts[0][5:].isdigit() and parts[1:] == ('opencode', 'opencode.db')):
                return home.parents[2] / 'retired' / 'hand-transcripts' / home.parent.name / 'home' / relative
            return None
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
            logical = home.parents[3] / 'seats' / home.parent.name / 'home' / record.relative_to(home)
            if destination(logical) == record:
                return True
    return resolve(record) != record


def records(home: Path, pattern: str) -> list[Path]:
    """Discover retained sources under their original logical names, including after rebuild."""
    # Use a recognized exemplar to locate this home's archive, never another seat's.
    exemplar = home / 'sessions' / 'example.jsonl'
    kept = destination(exemplar)
    if kept is None:
        return []
    root = kept.parent.parent
    return [home / path.relative_to(root) for path in root.glob(pattern) if path.is_file()]
