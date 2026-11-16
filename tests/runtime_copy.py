"""The one rule every tool in tests/ that copies a ship's runtime data keeps: the desk's and every office seat's records
are left out, by the tool that makes the copy. The crew does not read them, so no copy holds them.

The people are read from the ship file, never from a list of names: the seat whose role is desk, and every seat of a
session declared ``human = true``. A record is theirs when it lies in their seat folder (``runtime/seats/<seat>``, its
home included).
"""
from __future__ import annotations

from pathlib import Path

from quarterdeck import ship as shipmod


def people(ship_folder) -> frozenset[str]:
    """The seats whose records no copy takes: the desk's and every seat of a human session."""
    ship = shipmod.load(Path(ship_folder))
    return frozenset(name for name, spec in ship.seats.items() if spec.role == 'desk' or ship.session_of(spec).human)


def theirs(path, runtime, names) -> bool:
    """Whether ``path`` lies in one of ``names``' seat folders under ``runtime``."""
    path, runtime = Path(path), Path(runtime)
    if not path.is_relative_to(runtime):
        return False
    parts = path.relative_to(runtime).parts
    return len(parts) > 1 and parts[0] == 'seats' and parts[1] in names
