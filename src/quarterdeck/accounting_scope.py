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


def globals_of(reading, set_aside=()):
    """Every input but the records, the post folders and the paths set aside (a known seat's own launch files, judged
    seat by seat). The post is no global barrier: each seat's attribution scope (Threads.scope) carries it, seat by
    seat (Partitions.record_scopes)."""
    classes = dict(reading.classes)
    return (tuple((path, stamp) for path, stamp in reading.inputs[0]
                  if classes.get(path) not in RECORD_CLASSES and classes.get(path) != POST_CLASS
                  and path not in set_aside), *reading.inputs[1:])


def post_of(reading):
    """The post folders' stamps in a reading, or None when one is young: a message may still be landing within the
    filesystem's tick, so this reading cannot stand for the post."""
    paths = {path for path, kind in reading.classes if kind == POST_CLASS}
    if paths & set(reading.young):
        return None
    return tuple((path, stamp) for path, stamp in reading.inputs[0] if path in paths)


def post_names(runtime):
    """The message files of the post folders, each with its identity (device, inode) and size. Messages are
    immutable: a file that leaves, or whose identity or size changes under its name, breaks the post's contract
    (post_kept)."""
    names = {}
    for folder in POST_FOLDERS:
        try:
            with os.scandir(Path(runtime) / 'post' / folder) as entries:
                for entry in entries:
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    names[folder + '/' + entry.name] = (info.st_dev, info.st_ino, info.st_size)
        except FileNotFoundError:
            continue
    return names


def post_kept(before, now):
    """Every message file listed before is still here as it was: under its name, with its identity and its size."""
    return all(now.get(name) == identity for name, identity in before.items())


def global_valid(reading, set_aside=()):
    if reading is None:
        return False
    classes = dict(reading.classes)
    return not {path for path in reading.young
                if classes.get(path) != POST_CLASS and path not in set_aside}.difference(records(reading))


REGISTRIES = ('record-readers.jsonl', 'record-launches.jsonl')


def metadata_seat(path, runtime):
    """The seat whose own launch file this is: runtime/trusted/<seat>/... or runtime/seats/<seat>/..., else None."""
    try:
        parts = Path(path).relative_to(Path(runtime)).parts
    except ValueError:
        return None
    return parts[1] if len(parts) >= 3 and parts[0] in ('trusted', 'seats') else None


def seat_files(reading, runtime, seats):
    """{seat: {path: stamp}} of these seats' own launch files in a reading, and the seats with a young one."""
    files, young = {}, set()
    stamps = dict(reading.inputs[0])
    for path, kind in reading.classes:
        if kind != 'launch-metadata':
            continue
        seat = metadata_seat(path, runtime)
        if seat in seats:
            files.setdefault(seat, {})[path] = stamps.get(path)
            if path in reading.young:
                young.add(seat)
    return files, young


def named_records(path, offset=0):
    """The records a registry names, from ``offset`` on: each line's 'record'. Unreadable lines name nothing."""
    import json
    named = set()
    try:
        with open(path, 'rb') as stream:
            stream.seek(offset)
            data = stream.read()
    except OSError:
        return None
    for line in data.splitlines():
        try:
            record = json.loads(line).get('record')
        except (ValueError, AttributeError):
            continue
        if isinstance(record, str):
            named.add(str(Path(os.path.abspath(record))))
    return named


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
    elif written(last) - written(first) != own_changes:
        found.add('other-writers')
    return sorted(found)


def globals_without(reading, paths):
    classes = dict(reading.classes)
    return (tuple((path, stamp) for path, stamp in reading.inputs[0]
                  if classes.get(path) not in RECORD_CLASSES and classes.get(path) != POST_CLASS
                  and path not in paths), *reading.inputs[1:])


def written(reading):
    """The ledger rows a reading saw written, less those of duties proven to touch no table the accounting job reads
    (Service.NON_ACCOUNTING_DUTIES): what the other-writers barrier compares."""
    return reading.sql[2] - getattr(reading, 'foreign', 0)


def global_equal(before, after, own=0, set_aside=()):
    return (global_valid(before, set_aside) and global_valid(after, set_aside)
            and globals_of(before, set_aside) == globals_of(after, set_aside)
            and before.sql[:2] == after.sql[:2] and before.sql[3:] == after.sql[3:]
            and written(after) - written(before) == own)


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
        # Each known seat's own launch files, seat by seat: their stamps at the reading its last whole read began
        # from (None when one was young there), and the records each registry then named.
        self.runtime, self.meta_seen, self.meta_named = None, {}, {}

    def reject(self, *, keep_pending=False):
        self.baseline = None
        self.marks.clear()
        self.certified, self.dirty_seats = {}, set()
        self.post_seen, self.post_names = None, None
        self.meta_seen, self.meta_named = {}, {}
        if not keep_pending:
            self.pending = None

    def set_aside(self, *readings):
        """The known seats' own launch files in these readings: no global barrier, judged seat by seat instead."""
        if self.runtime is None:
            return frozenset()
        return frozenset(path for reading in readings if reading is not None
                         for files in seat_files(reading, self.runtime, self.seats)[0].values() for path in files)

    def record_meta(self, before, read):
        """The seats a committed job read whole are certified at their launch files' stamps in ``before``, the
        reading the job began from; a seat with a young launch file there is certified at none (read again)."""
        if self.runtime is None:
            return
        files, young = seat_files(before, self.runtime, set(read))
        for seat in read:
            self.meta_seen[seat] = None if seat in young else dict(files.get(seat, {}))
            for path in files.get(seat, {}):
                if Path(path).name in REGISTRIES:
                    self.meta_named[path] = named_records(path) or set()

    def meta_dirty(self, reading):
        """The seats whose own launch files moved since their last whole read, and the owners of every record a moved
        registry names: its new lines after the old size under the same file, or, rewritten or shrunk, every record
        it names now and named then. None when a record so named has an owner the partitions do not know."""
        if self.runtime is None:
            return set(self.seats)
        files, dirty = seat_files(reading, self.runtime, self.seats)
        dirty = set(dirty)
        for seat in self.seats:
            seen, now = self.meta_seen.get(seat), files.get(seat, {})
            if seen is None:
                dirty.add(seat)
                continue
            if now == seen:
                continue
            dirty.add(seat)
            for path in seen.keys() | now.keys():
                if Path(path).name not in REGISTRIES or seen.get(path) == now.get(path):
                    continue
                old, new = seen.get(path), now.get(path)
                if old is not None and new is not None and old[:2] == new[:2] and new[3] >= old[3]:
                    named = named_records(path, old[3])
                else:
                    named = (named_records(path) or set()) | self.meta_named.get(path, set())
                if named is None:
                    return None
                for record in named:
                    owners = self.owners.get(record, frozenset())
                    if not owners <= self.seats:
                        return None
                    dirty |= owners
        return dirty

    def record_scopes(self, before, scopes, read, runtime):
        """A committed job's post, seat by seat. ``scopes`` are the attribution scopes of every seat at the job's own
        post (its child's Threads, built after ``before``): the seats it read whole are certified at theirs, and every
        seat whose scope differs from its certified one is dirty, to be read by a scoped job. The post is then
        compared up to ``before``'s stamps, unless a post folder was young there. A message file that has left
        breaks the post's contract: False, and nothing is certified."""
        if scopes is None or runtime is None:
            return False
        names = post_names(runtime)
        if self.post_names is not None and not post_kept(self.post_names, names):
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
        own = seat_metadata(first, runtime, moved) | seat_metadata(last, runtime, moved) | self.set_aside(first, last)
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
        self.meta_seen, self.meta_named = {}, {}
        for before, _, seats, _, _ in readings:
            self.record_meta(before, seats)
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
        self.runtime = getattr(service, 'runtime', None)
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
        if not global_equal(self.baseline, reading, set_aside=self.set_aside(self.baseline, reading)):
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
        selected |= self.dirty_seats
        moved = self.meta_dirty(reading)  # the seats whose own launch files moved, and whom their registries reach
        if moved is None:
            return None
        selected |= moved  # the seats whose attribution scope the post has moved
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
        if (not committed or retry or not global_equal(before, after, own, set_aside=self.set_aside(before, after))
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
            self.meta_seen, self.meta_named = {}, {}
        self.record_meta(before, self.read if scope is None else scope)
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
