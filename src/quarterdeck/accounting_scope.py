"""Per-seat record evidence below a full accounting job's global barriers.

Only committed jobs advance stable observations. A young record stays dirty;
scoped jobs never renew the full-scan deadline or claim global completion.
"""

from collections import defaultdict
import os
from pathlib import Path


RECORD_CLASSES = frozenset(('record', '-wal'))


def records(reading):
    classes = dict(reading.classes)
    return {path: stamp for path, stamp in reading.inputs[0] if classes.get(path) in RECORD_CLASSES}


def globals_of(reading):
    classes = dict(reading.classes)
    return (tuple((path, stamp) for path, stamp in reading.inputs[0]
                  if classes.get(path) not in RECORD_CLASSES), *reading.inputs[1:])


def global_valid(reading):
    return reading is not None and not set(reading.young).difference(records(reading))


def global_equal(before, after, own=0):
    return (global_valid(before) and global_valid(after)
            and globals_of(before) == globals_of(after)
            and before.sql[:2] == after.sql[:2] and before.sql[3:] == after.sql[3:]
            and after.sql[2] - before.sql[2] == own)


class Partitions:
    def __init__(self):
        self.baseline = None
        self.marks, self.owners = {}, {}
        self.seats, self.dirty = set(), set()
        self.due = 0.0

    def reject(self):
        self.baseline = None
        self.marks.clear()

    def configure(self, service, inventory, scope):
        owners = defaultdict(set)
        for item in inventory:
            for path, *_ in item['sources']:
                owners[str(Path(os.path.abspath(path)))].add(item['name'])
        for path, seat in service.ledger.db.execute('SELECT DISTINCT record,seat FROM turns'):
            owners[str(Path(os.path.abspath(path)))].add(seat)
        expanded = {name: frozenset(names) for path, names in owners.items()
                    for name in (path, path + '-wal')}
        if scope is None:
            self.owners = expanded
            self.seats = {item['name'] for item in inventory}
        else:
            # Other seats' mapping must remain unchanged under the same barriers.
            self.owners.update(expanded)

    def refresh_dirty(self, reading):
        current = records(reading)
        self.dirty.update(path for path in current.keys() | self.marks.keys()
                          if path not in current or path not in self.marks
                          or current[path] != self.marks[path] or path in reading.young)
        return current

    def choose(self, reading, now, *, catching_up=False):
        """None means full, an empty set waits, otherwise these seats are dirty."""
        if self.baseline is None or catching_up or now >= self.due:
            return None
        if not global_equal(self.baseline, reading):
            return None
        self.refresh_dirty(reading)
        selected = set()
        for path in self.dirty:
            owners = self.owners.get(path, ())
            if len(owners) != 1 or not set(owners) <= self.seats:
                return None  # ambiguous, retired or unknown ownership is a full-job barrier
            selected.update(owners)
        return selected

    def outside_equal(self, before, after, scope):
        old, new = records(before), records(after)
        for path in old.keys() | new.keys():
            if self.owners.get(path, frozenset()).intersection(scope):
                continue
            if (path not in old or path not in new or old[path] != new[path]
                    or path in before.young or path in after.young):
                return False
        return True

    def finish(self, before, after, own, now, backstop, *, scope, full, committed, retry):
        if (not committed or retry or not global_equal(before, after, own)
                or scope is None and not full
                or scope is not None and not self.outside_equal(before, after, scope)):
            self.reject()
            return
        old, current = records(before), records(after)
        if scope is None:
            self.marks.clear()
            self.dirty = set(current)
            self.due = now + backstop
        for path, stamp in current.items():
            if scope is not None and not self.owners.get(path, frozenset()).intersection(scope):
                continue
            if (path in old and old[path] == stamp
                    and path not in before.young and path not in after.young):
                self.marks[path] = stamp
                self.dirty.discard(path)
            else:
                self.dirty.add(path)
        self.baseline = after
