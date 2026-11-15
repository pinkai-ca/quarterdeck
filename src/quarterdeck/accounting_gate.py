"""Private, tick-bounded evidence for waiting between completed accounting jobs.

No persistent configuration or native parsing. A missing/young/uncertain reading
grants no reuse. The monotonic backstop also covers inputs metadata cannot see.
"""

from dataclasses import dataclass, field
from collections import Counter
import os
from pathlib import Path
import sqlite3
import time

from . import post, record_owner, record_location
from .harness import reader_stamp

BACKSTOP = 600.0


@dataclass(frozen=True)
class Reading:
    inputs: tuple
    sql: tuple
    checked_at: int = field(compare=False)
    classes: tuple = field(default=(), compare=False, repr=False)


def capture(service, records, *, issues=None):
    """Metadata must precede this read by a whole filesystem timestamp tick.

    Post bodies are immutable: folder identity/times establish publication and
    projection changes. An in-place body rewrite is outside that contract and
    is revisited by the unconditional backstop, not 24,000 stats per duty.
    """
    started = time.time_ns()
    valid = True
    current_class = 'settings'
    def note(reason, kind):
        if issues is not None:
            issues.setdefault(reason, Counter())[kind] += 1
    def stamp(path, kind):
        nonlocal valid
        try:
            info = Path(path).stat()
        except FileNotFoundError:
            return None
        if max(info.st_mtime_ns, info.st_ctime_ns) + post.POST_TIMESTAMP_TICK_NS >= started:
            valid = False
            note('tick-young', kind)
        return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
                info.st_mtime_ns, info.st_ctime_ns)
    try:
        runtime = Path(service.runtime)
        seats = service.resolved_seats()
        paths = {service.ship.folder / 'ship.toml', runtime / 'seats'}
        classes = {path: 'settings' for path in paths}
        folders = {runtime / 'seats' / name for name in service.ship.seats}
        if (runtime / 'seats').is_dir():
            folders.update(path for path in (runtime / 'seats').iterdir() if path.is_dir())
        folders.update(runtime / 'seats' / seat.name for seat in seats)
        for folder in folders:
            paths.update(folder / name for name in ('launch.json', 'launch-price.json', 'record',
                         'pool', record_owner.REGISTRY, 'record-launches.jsonl'))
            paths.add(folder / 'home' / 'conversations.owned')
        declarations = []
        for seat in seats:
            paths.add(seat.home / 'conversations.owned')
            declarations.append((seat.name, str(seat.home), seat.harness.name, repr(seat.spec),
                                 seat.pool.name, seat.session.human))
        for name in ('store', 'imports', 'forwarded', 'addressed'):
            paths.add(runtime / 'post' / name)
            classes[runtime / 'post' / name] = 'post-folder'
        for record in records:
            current_class = 'record'
            path = Path(os.path.abspath(record))
            if not path.is_relative_to(runtime):
                continue  # an external legacy binding grants no new stat authority
            path = record_location.resolve(path)
            paths.update((path, Path(str(path) + '-wal')))
            classes[path], classes[Path(str(path) + '-wal')] = 'record', '-wal'
            if not path.exists():
                paths.add(path.parent)  # date an absence without watching unrelated sibling writes
                classes.setdefault(path.parent, 'record')
        files, file_classes = [], []
        for path in sorted(paths):
            current_class = classes.get(path, 'launch-metadata')
            name = str(path)
            files.append((name, stamp(path, current_class)))
            file_classes.append((name, current_class))
        files = tuple(files)
        current_class = 'sql-revision'
        db = service.ledger.db
        identity = (runtime / 'ledger.sqlite').stat()
        sql = (id(db), db.execute('PRAGMA data_version').fetchone()[0], db.total_changes,
               identity.st_dev, identity.st_ino)
        current_class = 'hashes'
        readers = reader_stamp.attributions()
        if not readers or not all(readers.values()):
            valid = False
            note('unreadable', 'hashes')
        # Keep the full declaration too: unresolved/disabled seats can change
        # even when they are absent from the resolved working-seat list.
        current_class = 'settings'
        inputs = (files, tuple(declarations), repr(service.ship), readers, service.accounting_epoch)
        ended = time.time_ns()
        if started > ended:
            note('unreadable', 'settings')
        return (Reading(inputs, sql, ended, tuple(file_classes))
                if valid and started <= ended else None)
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
        note('unreadable', current_class)
        return None  # the ordinary job owns failures; blindness never certifies idle


def differences(before, after):
    """Reduce private identities to a fixed set of class counts; never log values."""
    counts = Counter()
    old, new = dict(before.inputs[0]), dict(after.inputs[0])
    classes = dict(before.classes)
    classes.update(after.classes)
    for path in old.keys() | new.keys():
        if old.get(path) != new.get(path):
            counts[classes.get(path, 'settings')] += 1
    if before.inputs[1:3] != after.inputs[1:3]:
        counts['settings'] += 1
    if before.inputs[3:] != after.inputs[3:]:
        counts['hashes'] += 1
    return dict(counts)


def sql_changes(before, after, own=0):
    total = after.sql[2] - before.sql[2]
    return dict(total_changes=total, driver_changes=own, other_changes=total - own,
                data_version=after.sql[1] - before.sql[1],
                connection_changed=int(before.sql[0] != after.sql[0]),
                database_changed=int(before.sql[3:] != after.sql[3:]))


def evidence(before, after, issues, *, own=0):
    result = {reason: dict(counts) for reason, counts in issues.items()}
    if before is not None and after is not None:
        changed = differences(before, after)
        if changed:
            result['changed-input'] = changed
        result['sql'] = sql_changes(before, after, own)
        if (result['sql']['other_changes'] or result['sql']['data_version']
                or result['sql']['connection_changed'] or result['sql']['database_changed']):
            result['sql-revision'] = 1
    return result


class Gate:
    def __init__(self):
        self.records = set()
        self.completed = self.started = None
        self.own_changes = 0
        self.due = 0.0
        self.decision = {}
        self.previous = {}
        self.begin_issues = {}

    def reject(self):
        self.previous = dict(completed=0, rejected=1, begin_valid=int(self.started is not None),
                             begin=evidence(None, None, self.begin_issues))
        self.completed = self.started = None
        self.own_changes = 0

    def allowed(self, service, *, retry=False):
        if retry or self.completed is None or time.monotonic() >= self.due:
            reason = ('retry' if retry else 'no-completed-certificate' if self.completed is None
                      else 'backstop')
            self.decision = {reason: 1}
            if self.previous:
                self.decision['previous-job'] = self.previous
            return True
        issues = {}
        reading = capture(service, self.records, issues=issues)
        allowed = reading != self.completed
        self.decision = evidence(self.completed, reading, issues) if allowed else {}
        if allowed and self.previous:
            self.decision['previous-job'] = self.previous
        return allowed

    def begin(self, service, sources):
        self.begin_issues = {}
        self.records.update(sources)
        try:
            self.records.update(row[0] for row in service.ledger.db.execute('SELECT DISTINCT record FROM turns'))
            self.started = capture(service, self.records, issues=self.begin_issues)
        except (AttributeError, sqlite3.Error):
            self.started = None
            self.begin_issues = {'unreadable': {'sql-revision': 1}}
        self.own_changes = 0
        # A partial job cannot leave an older certificate usable.
        self.completed = None

    def own(self, before, after):
        if self.started is not None:
            self.own_changes += after - max(before, self.started.sql[2])

    def finish(self, service, step_before, *, full):
        issues = {}
        before, after = self.started, capture(service, self.records, issues=issues)
        extra = (service.ledger.db.total_changes - max(step_before, before.sql[2])) if before else 0
        if (full and before is not None and after is not None
                and before.inputs == after.inputs
                and before.sql[:2] == after.sql[:2] and before.sql[3:] == after.sql[3:]
                and after.sql[2] - before.sql[2] == self.own_changes + extra):
            self.completed = after
            self.due = time.monotonic() + BACKSTOP
        else:
            self.completed = None
        self.previous = dict(completed=int(self.completed is not None), full=int(full),
                             begin_valid=int(before is not None), end_valid=int(after is not None),
                             begin=evidence(None, None, self.begin_issues),
                             end=evidence(before, after, issues, own=self.own_changes + extra))
        self.started = None
        self.own_changes = 0
