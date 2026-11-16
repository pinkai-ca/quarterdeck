"""A completed retirement sweep may wait; its certificate never permits a move."""

from dataclasses import dataclass, field
import os
from pathlib import Path
import stat
import sqlite3
import time

from . import post, trusted_files


@dataclass(frozen=True)
class Reading:
    inputs: tuple
    at: int = field(compare=False)


def capture(service):
    """Mutable evidence is stamped per file; immutable publication by its containing folder."""
    started = time.time_ns()
    valid = True
    found = []

    def stamp(path):
        nonlocal valid
        try:
            info = path.lstat()
        except FileNotFoundError:
            found.append((str(path), None))
            return None
        if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            valid = False
        if max(info.st_mtime_ns, info.st_ctime_ns) + post.POST_TIMESTAMP_TICK_NS >= started:
            valid = False
        value = (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        found.append((str(path), value))
        return value

    def files(folder, *, nested=False, json_only=False):
        nonlocal valid
        before = stamp(folder)
        if before is None or not stat.S_ISDIR(before[2]):
            return
        with os.scandir(folder) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                path = folder / entry.name
                if nested and entry.is_dir(follow_symlinks=False):
                    files(path, json_only=True)
                elif not entry.name.endswith(('.tmp', '.lock')) and (not json_only or entry.name.endswith('.json')):
                    stamp(path)
        if stamp(folder) != before:
            valid = False

    try:
        runtime = Path(service.runtime)
        stamp(service.ship.file)
        stamp(runtime / 'service.delivery')
        for name in ('store', 'imports', 'forwarded', 'addressed'):
            stamp(runtime / 'post' / name)
        files(runtime / 'post' / 'attempts', json_only=True)
        files(runtime / 'post' / 'pending', nested=True, json_only=True)
        files(runtime / 'requests')
        # Batch indexes and completed markers are immutable publications. Active journals above are mutable.
        for name in ('index', 'completed', 'attempts'):
            stamp(runtime / 'post' / 'retired' / name)
        archived = runtime / 'post' / 'retired' / 'pending'
        archived_before = stamp(archived)
        if archived.is_dir() and not archived.is_symlink():
            for path in sorted(archived.iterdir()):
                stamp(path)  # immutable journal bodies; publication changes the seat folder
            if stamp(archived) != archived_before:
                valid = False
        root = trusted_files.root(runtime)
        root_before = stamp(root)
        names = set(service.ship.seats)
        if root.is_dir() and not root.is_symlink():
            names.update(entry.name for entry in root.iterdir() if entry.is_dir() and not entry.is_symlink())
        for name in sorted(names):
            launch = trusted_files.path(runtime, name, trusted_files.LAUNCH)
            if stamp(launch) is None:
                stamp(launch.parent)  # a young removal cannot date a stable missing launch
        if stamp(root) != root_before:
            valid = False
        catching_up = service.ledger.catching_up()
        ended = time.time_ns()
        return (Reading((tuple(found), repr(service.ship), catching_up), ended)
                if valid and started <= ended else None)
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
        return None


@dataclass
class Sweep:
    reading: Reading
    names: frozenset | None = None
    visited: set = field(default_factory=set)


class Gate:
    def __init__(self):
        self.sweep = None
        self.certificate = None
        self.until = None
        self.wall = None
        self.monotonic = None
        self.before = None

    def reject(self):
        self.sweep = self.certificate = self.until = None

    def begin(self, service, now):
        current = capture(service)
        reversed_clock = (self.monotonic is not None and now < self.monotonic
                          or current is not None and self.wall is not None and current.at < self.wall)
        self.monotonic = now
        if current is not None:
            self.wall = current.at
        self.before = current
        if current is None or reversed_clock:
            self.reject()
            return True
        if self.certificate == current and self.until is not None and now < self.until:
            return False
        if self.certificate is not None or self.sweep is None or self.sweep.reading != current:
            self.reject()
            self.sweep = Sweep(current)
        return True

    def prior(self):
        return frozenset(self.sweep.visited) if self.sweep is not None else frozenset()

    def finish(self, service, result, progress, interval):
        after = capture(service) if self.before is not None else None
        if (self.before is None or after != self.before or after.at < self.before.at
                or time.monotonic() < self.monotonic or self.sweep is None or progress is None
                or progress.blocked or result.get('held') or result.get('failed')
                or result['attempts'] or result['journals']):
            self.reject()
            return
        if progress.names is None:
            return  # a recovery, not a whole eligibility sweep
        if self.sweep.names is not None and self.sweep.names != progress.names:
            self.sweep = Sweep(after)
        self.sweep.names = progress.names
        self.sweep.visited.update(progress.visited)
        if not result.get('paused') and self.sweep.visited >= self.sweep.names:
            self.certificate = after
            self.until = time.monotonic() + interval
            self.sweep = None
