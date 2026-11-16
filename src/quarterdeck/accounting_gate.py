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

from . import trusted_files
from . import post, record_owner, record_location
from .harness import held_reading, reader_stamp
from .accounting_scope import Partitions

BACKSTOP = 600.0


@dataclass(frozen=True)
class Reading:
    inputs: tuple
    sql: tuple
    checked_at: int = field(compare=False)
    classes: tuple = field(default=(), compare=False, repr=False)
    young: tuple = field(default=(), compare=False, repr=False)
    began: int = field(default=0, compare=False, repr=False)


def capture(service, records, *, issues=None, retain=False):
    """Metadata must precede this read by a whole filesystem timestamp tick.

    Post bodies are immutable: folder identity/times establish publication and
    projection changes. An in-place body rewrite is outside that contract and
    is revisited by the unconditional backstop, not 24,000 stats per duty.
    """
    started = time.time_ns()
    valid = True
    current_class = 'settings'
    young = []
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
            young.append(str(path))
            note('tick-young', kind)
        return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
                info.st_mtime_ns, info.st_ctime_ns)
    try:
        runtime = Path(service.runtime)
        seats = service.resolved_seats()
        paths = {service.ship.folder / 'ship.toml', runtime / 'seats', trusted_files.root(runtime)}
        classes = {path: 'settings' for path in paths}
        folders = {runtime / 'seats' / name for name in service.ship.seats}
        for parent in (runtime / 'seats', trusted_files.root(runtime)):  # a seat known by either of its folders
            if parent.is_dir():
                folders.update(runtime / 'seats' / path.name for path in parent.iterdir() if path.is_dir())
        folders.update(runtime / 'seats' / seat.name for seat in seats)
        for folder in folders:
            trusted = trusted_files.of_seat_folder(folder)
            paths.update(trusted / name for name in (trusted_files.LAUNCH, trusted_files.PRICE, trusted_files.RECORD,
                         trusted_files.POOL, trusted_files.READERS, trusted_files.LAUNCHES))
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
            note('unreadable', 'hashes')
            return None
        # Keep the full declaration too: unresolved/disabled seats can change
        # even when they are absent from the resolved working-seat list.
        current_class = 'settings'
        inputs = (files, tuple(declarations), repr(service.ship), readers, service.accounting_epoch)
        ended = time.time_ns()
        if started > ended:
            note('unreadable', 'settings')
        return (Reading(inputs, sql, ended, tuple(file_classes), tuple(young), started)
                if (valid or retain) and started <= ended else None)
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
        note('unreadable', current_class)
        return None  # the ordinary job owns failures; blindness never certifies idle


def admit(service, before, records):
    """Give 'before' the paths first watched after it, when every one predates it by a tick.

    A path whose stamp is older than the reading was unchanged when it was taken;
    one created or touched since leaves 'before' as it was, so it differs at finish.
    """
    fresh = capture(service, records, retain=True)
    if before is None or fresh is None:
        return before
    known = dict(before.inputs[0])
    added = [(path, stamp) for path, stamp in fresh.inputs[0] if path not in known]
    if not added or any(stamp is not None and max(stamp[4], stamp[5]) + post.POST_TIMESTAMP_TICK_NS >= before.began
                        for path, stamp in added):
        return before
    names = {path for path, _ in added}
    files = tuple(sorted((*before.inputs[0], *added), key=lambda item: Path(item[0])))  # capture's own order
    classes = (*before.classes, *(item for item in fresh.classes if item[0] in names))
    return Reading((files, *before.inputs[1:]), before.sql, before.checked_at, classes, before.young, before.began)


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
        self.partitions = None
        self.scope = self.job_scope = None
        self.job_retry = False
        self.force_full = False
        # seat -> the holds its records were left with by the job that last read them: a record whose unknown lasts
        # only for a time is read again once that time has passed, though no input the certificate sees has changed.
        self.holds = {}
        self.held = set()  # the seats whose due holds the last allowed() added to its scope

    def hold(self, seat, hold):
        """The child left one of 'seat''s records held until 'hold' (held_reading.Hold) is due."""
        self.holds.setdefault(seat, []).append(hold)

    def taken(self, scope):
        """A job that reads these seats (all of them if None) starts: their holds are its to read and report again."""
        if scope is None:
            self.holds.clear()
        else:
            for seat in scope:
                self.holds.pop(seat, None)

    def due_holds(self):
        """The seats with a due hold, and how many holds are due; judged on each hold's own reader clock."""
        seats, count = set(), 0
        for seat, holds in self.holds.items():
            due = sum(1 for hold in holds if held_reading.due(hold))
            if due:
                seats.add(seat)
                count += due
        return seats, count

    def configure(self, service, inventory, scope):
        if self.partitions is None:
            self.partitions = Partitions()
        self.partitions.configure(service, inventory, scope)
        self.job_scope = None if scope is None else set(scope)
        self.job_retry = bool(self.decision.get('retry'))

    @staticmethod
    def catching_up(service):
        return getattr(service.ledger, 'catching_up', lambda: False)()

    def full_due(self):
        return (self.partitions is not None and self.partitions.baseline is not None
                and time.monotonic() >= self.partitions.due)

    def reject(self):
        self.previous = dict(completed=0, rejected=1, begin_valid=int(self.started is not None),
                             begin=evidence(None, None, self.begin_issues))
        self.completed = self.started = None
        self.own_changes = 0
        if self.partitions is not None:
            self.partitions.reject()

    def allowed(self, service, *, retry=False):
        self.scope = None
        self.force_full = False
        self.held = set()
        if self.partitions is not None and not retry and (self.catching_up(service) or self.full_due()):
            self.force_full = True
            self.decision = {'catch-up' if self.catching_up(service) else 'backstop': 1}
            if self.previous:
                self.decision['previous-job'] = self.previous
            return True
        if self.partitions is not None and not retry:
            p = self.partitions
            now = time.monotonic()
            if p.baseline is None or self.catching_up(service) or now >= p.due:
                reason = ('no-completed-certificate' if p.baseline is None else
                          'catch-up' if self.catching_up(service) else 'backstop')
                self.decision = {reason: 1}
                if self.previous:
                    self.decision['previous-job'] = self.previous
                return True
            issues = {}
            reading = capture(service, self.records, issues=issues, retain=True)
            self.scope = p.choose(reading, now)
            held, due = self.due_holds()
            if held and self.scope is not None:
                # A held seat the partitions no longer know cannot be read alone: the job is full.
                self.held = held
                self.scope = self.scope | held if held <= p.seats else None
            # Only the post moved: a job that reads no seat takes every seat's scope again (Partitions.record_scopes).
            post_only = self.scope is not None and not self.scope and p.post_due
            allowed = self.scope is None or bool(self.scope) or post_only
            self.decision = evidence(p.baseline, reading, issues) if allowed else {}
            if post_only:
                self.decision['post-scopes'] = 1
            if due and allowed:
                self.decision['held-reading'] = due  # a class and a count: never a seat, path or record
            if self.scope:
                self.decision['scoped-seats'] = len(self.scope)
            if allowed and self.previous:
                self.decision['previous-job'] = self.previous
            return allowed
        if retry or self.completed is None or time.monotonic() >= self.due:
            reason = ('retry' if retry else 'no-completed-certificate' if self.completed is None
                      else 'backstop')
            self.decision = {reason: 1}
            if self.previous:
                self.decision['previous-job'] = self.previous
            return True
        held, due = self.due_holds()
        if due:
            self.decision = {'held-reading': due}
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

    def begin(self, service, sources=()):
        """The certificate's 'before', read before the job collects its inventory.

        A change made while collecting is then newer than the certificate and
        starts the next job. A record first found while collecting joins it in
        confirm() only if it predates this reading.
        """
        self.begin_issues = {}
        self.records.update(sources)
        try:
            self.records.update(row[0] for row in service.ledger.db.execute('SELECT DISTINCT record FROM turns'))
            self.started = capture(service, self.records, issues=self.begin_issues, retain=True)
        except (AttributeError, sqlite3.Error):
            self.started = None
            self.begin_issues = {'unreadable': {'sql-revision': 1}}
        self.own_changes = 0
        # A partial job cannot leave an older certificate usable.
        self.completed = None
        return True

    def confirm(self, service, sources):
        """After collection and configure(): watch what it found; a raced scope is refused."""
        known = len(self.records)
        self.records.update(sources)
        if len(self.records) != known:
            self.started = admit(service, self.started, self.records)
        if self.partitions is not None and self.job_scope is not None and not self.job_retry:
            chosen = self.partitions.choose(self.started, time.monotonic(), catching_up=self.catching_up(service))
            if chosen is not None and self.held:
                chosen = chosen | self.held  # the seats allowed() added for their due holds
            if chosen != self.job_scope:
                self.reject()
                return False
        return True

    def own(self, before, after):
        if self.started is not None:
            self.own_changes += after - max(before, self.started.sql[2])

    def finish(self, service, step_before, *, full, committed=True, moved=(), emptied=False, scopes=None):
        """``moved``: the seats a full job found moved, whose continuation retries them; ``emptied``: this retry
        emptied that continuation, so every seat has now been read whole once (Partitions.stitch); ``scopes``: every
        seat's attribution scope at the post the job's child read (Partitions.record_scopes)."""
        issues = {}
        before = self.started
        after = (capture(service, self.records, issues=issues) if self.partitions is None else
                 capture(service, self.records, issues=issues, retain=True))
        extra = (service.ledger.db.total_changes - max(step_before, before.sql[2])) if before else 0
        if (full and before is not None and after is not None and not before.young and not after.young
                and before.inputs == after.inputs
                and before.sql[:2] == after.sql[:2] and before.sql[3:] == after.sql[3:]
                and after.sql[2] - before.sql[2] == self.own_changes + extra):
            self.completed = after
            self.due = time.monotonic() + BACKSTOP
        else:
            self.completed = None
        stitched = False
        if self.partitions is not None:
            own = self.own_changes + extra
            if self.job_scope is None and moved:
                self.partitions.hold_full(before, after, own, moved, service.runtime, scopes)
            elif self.job_retry and self.partitions.pending is not None:
                if committed:
                    self.partitions.add_retry(before, after, self.job_scope, own, scopes)
                    if emptied:
                        stitched = self.partitions.stitch(time.monotonic(), BACKSTOP)
                        if not stitched and self.partitions.refusal:
                            log = getattr(service, 'log', None)
                            if log is not None:
                                log('accounting-stitch-refused', classes=list(self.partitions.refusal))
                else:
                    # A retry that moved again: the continuation retries its seats, and the stitch waits for it.
                    self.partitions.discard_retry(own)
            else:
                self.partitions.finish(before, after, own, time.monotonic(), BACKSTOP,
                                       scope=self.job_scope, full=full, committed=committed, retry=self.job_retry,
                                       scopes=scopes, runtime=service.runtime)
            self.due = self.partitions.due
        self.previous = dict(completed=int(self.completed is not None), full=int(full), stitched=int(stitched),
                             begin_valid=int(before is not None and not before.young),
                             end_valid=int(after is not None and not after.young),
                             begin=evidence(None, None, self.begin_issues),
                             end=evidence(before, after, issues, own=self.own_changes + extra))
        if self.partitions is not None:
            self.previous['dirty-records'] = len(self.partitions.dirty)
            if self.partitions.refusal and not stitched and emptied:
                self.previous['stitch-refused'] = list(self.partitions.refusal)
        self.started = None
        self.own_changes = 0
