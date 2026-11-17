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


def copy_trusted(source, destination, names) -> int:
    """Each crew seat's launch files from runtime/trusted/<seat>, where the service keeps them out of every jail's reach
    (trusted_files), into the copy's own trusted folder: the launch record, its price, the pool, the record pointer and
    both registries, the files the accounting gate reads. A person's folder is left out (seat_named), and nothing else
    of a trusted folder is copied (its alarms, holds and launch script). Every record path the pointer and the
    registries name is moved into the copy, so no copy names a file of the source ship; a row naming a person's record
    is left out. Returns how many seats' files were copied."""
    from quarterdeck import trusted_files
    source, destination = Path(source), Path(destination)
    root = trusted_files.root(source)
    def moved(value):
        path = Path(value)
        return str(destination / path.relative_to(source)) if path.is_relative_to(source) else value
    seats = 0
    for folder in sorted(root.iterdir()) if root.is_dir() else ():
        if not folder.is_dir() or folder.is_symlink() or seat_named(Path(folder.name) / trusted_files.LAUNCH, names):
            continue
        target = trusted_files.folder(destination, folder.name)
        copied = False
        for name in (trusted_files.LAUNCH, trusted_files.PRICE, trusted_files.POOL):
            path = folder / name
            if path.is_file() and not path.is_symlink():
                target.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target / name)
                copied = True
        path = folder / trusted_files.RECORD
        if path.is_file() and not path.is_symlink():
            named = path.read_text().strip()
            if named and not theirs(Path(named), source, names):
                target.mkdir(parents=True, exist_ok=True)
                (target / trusted_files.RECORD).write_text(moved(named) + '\n')
                copied = True
        for name in (trusted_files.READERS, trusted_files.LAUNCHES):
            path = folder / name
            if not path.is_file() or path.is_symlink():
                continue
            rows = []
            for line in path.read_text().splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                if isinstance(row.get('record'), str):
                    if theirs(Path(row['record']), source, names):
                        continue
                    row['record'] = moved(row['record'])
                rows.append(json.dumps(row))
            target.mkdir(parents=True, exist_ok=True)
            (target / name).write_text(''.join(row + '\n' for row in rows))
            copied = True
        seats += copied
    return seats


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


# The post's families a copy takes, attempts included: the delivery attempts are what tells a waiting message from a
# delivered one, so a copy without them reads every message as undelivered.
POST_FAMILIES = ('store', 'imports', 'forwarded', 'addressed', 'attempts')


def copy_posts(source, destination, names, internal) -> int:
    """Every family of POST_FAMILIES from runtime ``source`` to runtime ``destination``, by copy_post's rule: the
    people's seat-named files and every file of an office-internal message left out, an attempt of one included
    (of_message). The number of files left out."""
    left = 0
    for name in POST_FAMILIES:
        old = Path(source) / 'post' / name
        if old.exists():
            left += copy_post(old, Path(destination) / 'post' / name, names, internal)
    return left


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
