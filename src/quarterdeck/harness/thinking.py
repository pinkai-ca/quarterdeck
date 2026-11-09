"""Values and source checks shared by the two proved native thinking controls.

The declaration and native files remain configuration. A launch saves only the
hashes and effective request facts needed to refuse a changed/unknown control.
"""

import hashlib
import json
from pathlib import Path


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def declaration(seat, pool) -> str:
    return digest([seat.model, seat.effort, seat.settings, seat.human_session,
                   pool.name, pool.endpoint, pool.context, pool.output])


def sources(home: Path, names: tuple[str, ...]) -> dict[str, str] | None:
    try:
        if home.resolve() != home.absolute():
            return None
        found = {}
        for name in names:
            path = home / name
            if any(p.is_symlink() for p in (path, *path.parents) if p != home.parent):
                return None
            found[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        return found
    except OSError:
        return None


def json_file(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def validate_launch(evidence: dict) -> None:
    """A malformed optional launch proof is unknown, never an absent proof."""
    if (not isinstance(evidence, dict) or set(evidence) != {'format', 'configuration', 'settings', 'environment'}
            or type(evidence['format']) is not int or evidence['format'] != 1
            or not isinstance(evidence['configuration'], dict) or not evidence['configuration']
            or not isinstance(evidence['settings'], dict) or not isinstance(evidence['environment'], dict)):
        raise ValueError('invalid native thinking-recovery launch evidence')
    for name, sha in evidence['configuration'].items():
        if (not isinstance(name, str) or Path(name).is_absolute() or '..' in Path(name).parts
                or not isinstance(sha, str) or len(sha) != 64 or any(c not in '0123456789abcdef' for c in sha)):
            raise ValueError('invalid native thinking-recovery configuration identity')


def unchanged(home: Path, saved: dict, names: tuple[str, ...]) -> bool:
    validate_launch(saved)
    return set(saved['configuration']) == set(names) and sources(home, names) == saved['configuration']
