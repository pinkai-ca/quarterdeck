"""Private, tick-bounded evidence for waiting between completed accounting jobs.

No persistent configuration or native parsing. A missing/young/uncertain reading
grants no reuse. The monotonic backstop also covers inputs metadata cannot see.
"""

from dataclasses import dataclass, field
import os
from pathlib import Path
import sqlite3
import time

from . import post, record_owner
from .harness import reader_stamp

BACKSTOP = 600.0


@dataclass(frozen=True)
class Reading:
    inputs: tuple
    sql: tuple
    checked_at: int = field(compare=False)


def capture(service, records):
    """Metadata must precede this read by a whole filesystem timestamp tick.

    Post bodies are immutable: folder identity/times establish publication and
    projection changes. An in-place body rewrite is outside that contract and
    is revisited by the unconditional backstop, not 24,000 stats per duty.
    """
    started = time.time_ns()
    valid = True
    def stamp(path):
        nonlocal valid
        try:
            info = Path(path).stat()
        except FileNotFoundError:
            return None
        if max(info.st_mtime_ns, info.st_ctime_ns) + post.POST_TIMESTAMP_TICK_NS >= started:
            valid = False
        return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
                info.st_mtime_ns, info.st_ctime_ns)
    try:
        runtime = Path(service.runtime)
        seats = service.resolved_seats()
        paths = {service.ship.folder / 'ship.toml', runtime / 'seats'}
        declarations = []
        for seat in seats:
            folder = runtime / 'seats' / seat.name
            paths.update(folder / name for name in ('launch.json', 'launch-price.json', 'record',
                         'pool', record_owner.REGISTRY, 'record-launches.jsonl'))
            paths.add(seat.home / 'conversations.owned')
            declarations.append((seat.name, str(seat.home), seat.harness.name, repr(seat.spec),
                                 seat.pool.name, seat.session.human))
        for name in ('store', 'imports', 'forwarded', 'addressed'):
            paths.add(runtime / 'post' / name)
        for record in records:
            path = Path(os.path.abspath(record))
            if not path.is_relative_to(runtime):
                continue  # an external legacy binding grants no new stat authority
            paths.update((path, Path(str(path) + '-wal')))
            if not path.exists():
                paths.add(path.parent)  # date an absence without watching unrelated sibling writes
        files = tuple((str(path), stamp(path)) for path in sorted(paths))
        db = service.ledger.db
        identity = (runtime / 'ledger.sqlite').stat()
        sql = (id(db), db.execute('PRAGMA data_version').fetchone()[0], db.total_changes,
               identity.st_dev, identity.st_ino)
        readers = reader_stamp.attributions()
        if not readers or not all(readers.values()):
            valid = False
        # Keep the full declaration too: unresolved/disabled seats can change
        # even when they are absent from the resolved working-seat list.
        inputs = (files, tuple(declarations), repr(service.ship), readers, service.accounting_epoch)
        ended = time.time_ns()
        return Reading(inputs, sql, ended) if valid and started <= ended else None
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
        return None  # the ordinary job owns failures; blindness never certifies idle


class Gate:
    def __init__(self):
        self.records = set()
        self.completed = self.started = None
        self.own_changes = 0
        self.due = 0.0

    def reject(self):
        self.completed = self.started = None
        self.own_changes = 0

    def allowed(self, service, *, retry=False):
        if retry or self.completed is None or time.monotonic() >= self.due:
            return True
        return capture(service, self.records) != self.completed

    def begin(self, service, sources):
        self.records.update(sources)
        try:
            self.records.update(row[0] for row in service.ledger.db.execute('SELECT DISTINCT record FROM turns'))
            self.started = capture(service, self.records)
        except (AttributeError, sqlite3.Error):
            self.started = None
        self.own_changes = 0
        # A partial job cannot leave an older certificate usable.
        self.completed = None

    def own(self, before, after):
        if self.started is not None:
            self.own_changes += after - max(before, self.started.sql[2])

    def finish(self, service, step_before, *, full):
        before, after = self.started, capture(service, self.records)
        extra = (service.ledger.db.total_changes - max(step_before, before.sql[2])) if before else 0
        if (full and before is not None and after is not None
                and before.inputs == after.inputs
                and before.sql[:2] == after.sql[:2] and before.sql[3:] == after.sql[3:]
                and after.sql[2] - before.sql[2] == self.own_changes + extra):
            self.completed = after
            self.due = time.monotonic() + BACKSTOP
        else:
            self.completed = None
        self.started = None
        self.own_changes = 0
