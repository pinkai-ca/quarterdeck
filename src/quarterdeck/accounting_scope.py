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


def seat_metadata(reading, runtime, seats):
    """These seats' own launch metadata in a reading: the files of their trusted folders and their homes'
    conversations.owned. A seat's identity is these files, so the retry that re-reads the seat whole reads them too."""
    roots = [Path(runtime) / 'trusted' / seat for seat in seats] + [Path(runtime) / 'seats' / seat for seat in seats]
    return {path for path, kind in reading.classes
            if kind == 'launch-metadata' and any(Path(path).is_relative_to(root) for root in roots)}


def barriers(first, last, set_aside, young, own_changes):
    """Which global barriers moved between two readings, as classes: the class of each changed or young path (post
    folder, launch metadata, settings), the declarations, the ship's settings, the reader hashes, the epoch, the
    ledger's identity, and other writers' ledger rows. Never a path or a seat."""
    classes = dict(first.classes) | dict(last.classes)
    found = {classes.get(path, 'launch-metadata') + '-young' for path in young}
    files = [dict(globals_without(reading, set_aside)[0]) for reading in (first, last)]
    found.update(classes.get(path, 'launch-metadata') for path in files[0].keys() | files[1].keys()
                 if files[0].get(path) != files[1].get(path))
    for index, name in ((1, 'declarations'), (2, 'settings'), (3, 'hashes'), (4, 'epoch')):
        if first.inputs[index] != last.inputs[index]:
            found.add(name)
    if first.sql[:2] != last.sql[:2] or first.sql[3:] != last.sql[3:]:
        found.add('ledger-identity')
    elif last.sql[2] - first.sql[2] != own_changes:
        found.add('other-writers')
    return sorted(found)


def globals_without(reading, paths):
    classes = dict(reading.classes)
    return (tuple((path, stamp) for path, stamp in reading.inputs[0]
                  if classes.get(path) not in RECORD_CLASSES and path not in paths), *reading.inputs[1:])


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
        self.read = set()  # the seats the current job read whole: those its collect did not skip
        self.due = 0.0
        self.pending = None  # a full job some seats moved under, waiting for its retries (stitch)
        self.refusal = []  # the barrier classes that refused the last stitch: classes only, never a path or seat

    def reject(self, *, keep_pending=False):
        self.baseline = None
        self.marks.clear()
        if not keep_pending:
            self.pending = None

    def hold_full(self, before, after, own, moved, runtime):
        """A full job found these seats moved: what it read whole of every other seat waits for their retries, which
        read the moved seats whole once. Nothing is certified until they have."""
        self.reject()
        if before is None or after is None:
            return
        kept = set(self.seats) - set(moved)
        self.pending = dict(runtime=runtime, readings=[(before, after, kept, own, True)], discarded=0)

    def discard_retry(self, own):
        """A retry that was itself reset: it certifies nothing, but its own ledger writes are the job's, not another
        writer's, so the stitch counts them once."""
        self.reject(keep_pending=True)
        if self.pending is not None:
            self.pending['discarded'] += own

    def add_retry(self, before, after, scope, own):
        """A retry of moved seats read them whole: its readings certify those seats' records. A seat its read failed
        for was not read whole: the retry certifies nothing of it, and its records go dirty at the stitch."""
        if self.pending is not None and before is not None and after is not None:
            self.pending['readings'].append((before, after, set(scope) & self.read, own, False))

    def stitch(self, now, backstop):
        """The full job's continuation has emptied: every seat was read whole once, by the full job or by a retry.
        Each record is marked only where its readings in the job that read it are equal and settled; every other
        record goes dirty. The global barriers must hold across the whole span, with every job's own ledger changes
        counted, or nothing is certified and the next job is full. The backstop runs from the stitch, as a completed
        full job's runs from its end: a span longer than the backstop still gives a certificate that holds."""
        pending, self.pending = self.pending, None
        self.refusal = []
        if pending is None:
            return False
        readings = pending['readings']
        first, last = readings[0][0], readings[-1][1]
        # The global barriers hold across the whole span: the post folder, settings, the declarations, every other
        # seat's launch metadata and every other writer's ledger rows. A moved seat's own launch metadata (its
        # identity, which moved) is judged instead by the retry that re-read it: equal and settled within that retry.
        moved = set().union(*(seats for _, _, seats, _, full in readings if not full))
        runtime = pending['runtime']
        own = seat_metadata(first, runtime, moved) | seat_metadata(last, runtime, moved)
        young = (set(first.young) | set(last.young)) - set(records(first)) - set(records(last)) - own
        own_changes = sum(reading[3] for reading in readings) + pending.get('discarded', 0)
        refusal = barriers(first, last, own, young, own_changes)
        if refusal:
            self.reject()
            self.refusal = refusal
            return False
        for before, after, seats, _, full in readings:
            if full:
                continue
            theirs = seat_metadata(before, runtime, seats) | seat_metadata(after, runtime, seats)
            stamps_before, stamps_after = dict(before.inputs[0]), dict(after.inputs[0])
            if any(stamps_before.get(path) != stamps_after.get(path) or path in before.young or path in after.young
                   for path in theirs):
                self.reject()  # a retried seat's identity moved again within its own retry: nothing certified
                self.refusal = ['retried-seat-moved']
                return False
        marks = {}
        for before, after, seats, _, full in readings:
            old = records(before)
            for path, stamp in records(after).items():
                owners = self.owners.get(path, frozenset())
                # A record is the job's to certify that read every one of its owners; one no seat lists is the full
                # job's, which read the whole inventory and the ledger.
                if not (owners <= seats if owners else full):
                    continue
                if (path in old and old[path] == stamp
                        and path not in before.young and path not in after.young):
                    marks[path] = stamp
        current = records(last)
        self.marks = marks
        self.dirty = {path for path in current if path not in marks}
        self.baseline = last
        self.due = now + backstop
        return True

    def configure(self, service, inventory, scope):
        owners = defaultdict(set)
        for item in inventory:
            for path, *_ in item['sources']:
                owners[str(Path(os.path.abspath(path)))].add(item['name'])
        for path, seat in service.ledger.db.execute('SELECT DISTINCT record,seat FROM turns'):
            owners[str(Path(os.path.abspath(path)))].add(seat)
        expanded = {name: frozenset(names) for path, names in owners.items()
                    for name in (path, path + '-wal')}
        self.read = {item['name'] for item in inventory}
        if scope is None:
            self.owners = expanded
            self.seats = {item['name'] for item in inventory}
            self.pending = None  # a new full job: an earlier one's stitch is no longer its to complete
        else:
            # A scoped job read only the inventory of the scoped seats it collected: it replaces their own entries, and
            # keeps every other seat's as it stands, a scoped seat whose read failed included. An owner it did not read
            # is never removed, so a record another seat also lists stays ambiguous, and a change to it still calls
            # for the full job.
            scoped = set(scope) & self.read
            merged = {path: names - scoped for path, names in self.owners.items()}
            for path, names in expanded.items():
                merged[path] = merged.get(path, frozenset()) | names
            self.owners = {path: frozenset(names) for path, names in merged.items() if names}

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
        if scope is not None:
            scope = set(scope) & self.read  # a scoped seat whose read failed is outside the job: nothing of it is marked
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
