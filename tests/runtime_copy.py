"""The one rule every tool in tests/ that copies a ship's runtime data keeps: the desk's and every office seat's records
are left out, by the tool that makes the copy. The crew does not read them, so no copy holds them.

The people are read from the ship file, never from a list of names: the seat whose role is desk, and every seat of a
session declared ``human = true``. A record is theirs when it lies in their seat folder (``runtime/seats/<seat>``, its
home included).
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil

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


def seat_named(relative, names) -> bool:
    """A path a tool copies by seat: one of its folders is a person's seat name, or its file is (brief/<seat>.md,
    post/inbox/<seat>.md, trusted/<seat>/...). A message id that holds a seat name is no such name."""
    parts = Path(relative).parts
    return bool(parts) and (any(part in names for part in parts[:-1]) or Path(parts[-1]).stem in names)


def office_internal(store, ship_folder, names) -> frozenset[str]:
    """The ids of the messages in ``store`` whose sender and every recipient are people: not the crew's to read. A
    group's recipients are its members. Each message file is read for its sender and recipient alone, never a body."""
    groups = {name: set(group.members) for name, group in shipmod.load(Path(ship_folder)).groups.items()}
    found = set()
    for path in Path(store).glob('*.json'):
        try:
            data = json.loads(path.read_text())
            sender, to = data.get('from'), data.get('to')
        except (OSError, ValueError, AttributeError):
            continue  # an unreadable message is kept, as before: nothing of it is read
        recipients = groups.get(to, {to})
        if sender in names and recipients and recipients <= set(names):
            found.add(path.stem)
    return frozenset(found)


def of_message(name, ids) -> bool:
    """A post file named for one of these messages: the message itself, or an attempt or import of it."""
    stem = Path(name).stem
    return stem in ids or any(stem.startswith(identifier + '-') for identifier in ids)


def copy_post(old, new, names, internal):
    """Copy one post family (store, imports, forwarded, addressed), leaving out the people's seat-named files and
    every file of an office-internal message. The number of files left out."""
    left = 0
    for path in sorted(Path(old).rglob('*')):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(old)
        if seat_named(relative, names) or of_message(path.name, internal):
            left += 1
            continue
        target = Path(new) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    return left
