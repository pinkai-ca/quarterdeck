"""Per-seat record evidence below a full accounting job's global barriers.

Only committed jobs advance stable observations. A young record stays dirty;
scoped jobs never renew the full-scan deadline or claim global completion.
"""

from collections import defaultdict
import os
from pathlib import Path


RECORD_CLASSES = frozenset(('record', '-wal'))
POST_CLASS = 'post-folder'
# The post's own reading: Threads.scope at an unbounded end, which holds every message any of the seat's verdicts can see.
SCOPE_END = '9999-12-31T23:59:59+00:00'
POST_FOLDERS = ('store', 'imports', 'forwarded', 'addressed')


def records(reading):
    classes = dict(reading.classes)
    return {path: stamp for path, stamp in reading.inputs[0] if classes.get(path) in RECORD_CLASSES}


def globals_of(reading):
    """Every input but the records and the post folders. The post is no global barrier: each seat's attribution scope
    (Threads.scope) carries it, seat by seat (Partitions.record_scopes)."""
    classes = dict(reading.classes)
    return (tuple((path, stamp) for path, stamp in reading.inputs[0]
                  if classes.get(path) not in RECORD_CLASSES and classes.get(path) != POST_CLASS), *reading.inputs[1:])


def post_of(reading):
    """The post folders' stamps in a reading, or None when one is young: a message may still be landing within the
    filesystem's tick, so this reading cannot stand for the post."""
    paths = {path for path, kind in reading.classes if kind == POST_CLASS}
    if paths & set(reading.young):
        return None
    return tuple((path, stamp) for path, stamp in reading.inputs[0] if path in paths)


def post_names(runtime):
    """The message files of the post folders: a file that leaves breaks the post's contract (messages are immutable)."""
    names = set()
    for folder in POST_FOLDERS:
        path = Path(runtime) / 'post' / folder
        try:
            names.update(folder + '/' + name for name in os.listdir(path))
        except FileNotFoundError:
            continue
    return frozenset(names)


def global_valid(reading):
    if reading is None:
        return False
    classes = dict(reading.classes)
    return not {path for path in reading.young if classes.get(path) != POST_CLASS}.difference(records(reading))


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
                  if classes.get(path) not in RECORD_CLASSES and classes.get(path) != POST_CLASS
                  and path not in paths), *reading.inputs[1:])


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
        # The post, seat by seat: each seat's attribution scope (Threads.scope at SCOPE_END) as its last whole read
        # used it, the seats whose scope has moved since, the post folders' stamps every seat's scope was last
        # compared at, and the message files then present.
        self.certified, self.dirty_seats = {}, set()
        self.post_seen, self.post_names, self.post_due = None, None, False

    def reject(self, *, keep_pending=False):
        self.baseline = None
        self.marks.clear()
        self.certified, self.dirty_seats = {}, set()
        self.post_seen, self.post_names = None, None
        if not keep_pending:
            self.pending = None

    def record_scopes(self, before, scopes, read, runtime):
        """A committed job's post, seat by seat. ``scopes`` are the attribution scopes of every seat at the job's own
        post (its child's Threads, built after ``before``): the seats it read whole are certified at theirs, and every
        seat whose scope differs from its certified one is dirty, to be read by a scoped job. The post is then
        compared up to ``before``'s stamps, unless a post folder was young there. A message file that has left
        breaks the post's contract: False, and nothing is certified."""
        if scopes is None or runtime is None:
            return False
        names = post_names(runtime)
        if self.post_names is not None and not self.post_names <= names:
            return False
        for seat in read:
            self.certified[seat] = scopes.get(seat)
        self.dirty_seats = {seat for seat in self.seats
                            if self.certified.get(seat) is None or scopes.get(seat) != self.certified[seat]}
        self.post_seen, self.post_names = post_of(before), names
        return True

    def hold_full(self, before, after, own, moved, runtime, scopes=None):
        """A full job found these seats moved: what it read whole of every other seat waits for their retries, which
        read the moved seats whole once. Nothing is certified until they have."""
        self.reject()
        if before is None or after is None:
            return
        kept = set(self.seats) - set(moved)
        self.pending = dict(runtime=runtime, readings=[(before, after, kept, own, True)], discarded=0,
                            scopes=[scopes], post_names=post_names(runtime))

    def discard_retry(self, own):
        """A retry that was itself reset: it certifies nothing, but its own ledger writes are the job's, not another
        writer's, so the stitch counts them once."""
        self.reject(keep_pending=True)
        if self.pending is not None:
            self.pending['discarded'] += own

    def add_retry(self, before, after, scope, own, scopes=None):
        """A retry of moved seats read them whole: its readings certify those seats' records. A seat its read failed
        for was not read whole: the retry certifies nothing of it, and its records go dirty at the stitch."""
        if self.pending is not None and before is not None and after is not None:
            self.pending['readings'].append((before, after, set(scope) & self.read, own, False))
            self.pending['scopes'].append(scopes)

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
        posts = {path for reading in (first, last) for path, kind in reading.classes if kind == POST_CLASS}
        young = (set(first.young) | set(last.young)) - set(records(first)) - set(records(last)) - own - posts
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
        # The post, seat by seat: each seat is certified at the scope its own reading's child used, and the newest
        # child's scopes say which seats the post has moved since. The certificate's post stands only at the stamps
        # those digests were taken after; a later message keeps the post due (choose) until a job covers it.
        certified = {}
        for (_, _, seats, _, _), scopes in zip(readings, pending['scopes']):
            for seat in seats:
                certified[seat] = scopes.get(seat) if scopes else None
        self.certified, self.post_names = certified, pending['post_names']
        if not self.record_scopes(readings[-1][0], pending['scopes'][-1], set(), runtime):
            self.reject()
            self.refusal = ['post-unscoped' if pending['scopes'][-1] is None else 'post-removed']
            return False
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
        if not self.dirty_seats <= self.seats:
            return None
        selected |= self.dirty_seats  # the seats whose attribution scope the post has moved
        # The post has changed since the stamps every seat's scope was last compared at, or could not be read: a job
        # must take the seats' scopes again before anything stands on this post. With nothing else to read, that job
        # reads no seat (Gate.allowed).
        self.post_due = self.post_seen is None or post_of(reading) != self.post_seen
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

    def finish(self, before, after, own, now, backstop, *, scope, full, committed, retry, scopes=None, runtime=None):
        if (not committed or retry or not global_equal(before, after, own)
                or scope is None and not full
                or scope is not None and not self.outside_equal(before, after, scope)):
            self.reject()
            return
        old, current = records(before), records(after)
        if scope is not None:
            scope = set(scope) & self.read  # a scoped seat whose read failed is outside the job: nothing of it is marked
        if scope is None:
            # A full job's child built its post afresh: every seat it read is certified at it, and no earlier post
            # listing is its to compare.
            self.certified, self.post_names = {}, None
        if not self.record_scopes(before, scopes, self.read if scope is None else scope, runtime):
            self.reject()
            return
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
