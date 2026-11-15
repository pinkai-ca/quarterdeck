"""Retain accounted, closed hand transcripts outside the seat's write allowance."""

from datetime import datetime, timezone
import os
from pathlib import Path
import time

from . import lifecycle, pr_screen, record_location, record_owner, trusted_files
from .harness import reader_stamp
from .harness.base import record_fingerprint
from .provenance import owning_launch_evidence

WEEK = 7 * 24 * 60 * 60
INTERVAL = 300


def opened(paths, process_names=None):
    """An uncertain descriptor inventory cannot authorize moving a native source.

    Inspect this user's processes, including a program surviving an older launch.
    Disappearing processes/descriptors are ordinary. An unreadable native program
    (as identified by its adapter) holds the move; unrelated privileged agents do not.
    """
    identities = {(p.stat().st_dev, p.stat().st_ino) for p in paths}
    for process in Path('/proc').iterdir():
        if not process.name.isdecimal():
            continue
        try:
            if process.stat().st_uid != os.getuid():
                continue
            for descriptor in (process / 'fd').iterdir():
                try:
                    info = descriptor.stat()
                except FileNotFoundError:
                    continue
                if (info.st_dev, info.st_ino) in identities:
                    return True
        except FileNotFoundError:
            continue
        except OSError:
            if _ended(process):
                continue  # every task has exited: it holds no descriptor, though its listing is refused
            try:
                name = (process / 'comm').read_text().strip()
            except FileNotFoundError:
                continue
            except OSError:
                return True
            if process_names is None or name in process_names:
                return True
    return False


ENDED = ('Z', 'X', 'x')  # zombie or dead: a task in these states holds no descriptor


def _ended(process: Path) -> bool:
    """Whether every task of a process has exited (its state Z, X or x), so it holds nothing. A zombie's descriptor
    listing is refused, but a thread-group leader reads Z while its other threads still run and hold the files (a
    node or bun process with worker threads): it has ended only when every task under ``task`` has. A process whose
    tasks cannot all be read has not been shown ended, and holds the move as before."""
    try:
        tasks = [entry.name for entry in os.scandir(process / 'task')]
    except FileNotFoundError:
        return True  # gone while read: nothing of it is left to hold the files
    except OSError:
        return False
    if not tasks:
        return False
    for task in tasks:
        try:
            state = (process / 'task' / task / 'stat').read_text().rsplit(')', 1)[1].split()[0]
        except FileNotFoundError:
            continue  # that task has gone; the others still say
        except (OSError, IndexError, UnicodeError):
            return False
        if state not in ENDED:
            return False
    return True


def _listed(runtime) -> dict[str, set[str]]:
    """Each hand's screening records still in its home (pr_screen); a list that cannot be read lists nothing here:
    launches of that hand stay fresh, and nothing of it moves early."""
    listed = {}
    if runtime is None:
        return listed  # a stand-in service with no runtime lists nothing
    try:
        names = sorted(path.stem for path in (Path(runtime) / pr_screen.ROOT / pr_screen.RECORDS).glob('*.json'))
    except OSError:
        return listed
    for name in names:
        try:
            records = set(pr_screen.screening_records(runtime, name))
        except (OSError, ValueError):
            continue
        if records:
            listed[name] = records
        else:
            pr_screen.drop_spent(runtime, name)  # spent: the next idle pass finds no list to read
    return listed


def run(service, *, now=None, limit=1, declined=None):
    """Move bounded records only between completed accounting jobs, under seat ownership.

    A screening launch's record holds an outsider's text (pr_screen), so it goes first: every hand's listed records
    in one walk, outside the limit and through a catch-up, without the week's wait. Each still waits for its own
    accounting checkpoint and every other check. Then, at most ``limit`` ordinary records, as before.

    Every guard that declines is named: in ``declined``, when given, as (seat, record, guard), and in the service's
    log once for each record and guard, so a record that stays says why."""
    book = getattr(service, 'ledger', None)
    if book is None:
        _declined(service, declined, None, None, 'no ledger')
        return 0
    driver = getattr(service, 'account_driver', None)
    if book.db.in_transaction or driver is not None and driver.active:
        _declined(service, declined, None, None, 'an accounting job is open')
        return 0  # never while an accounting job is open
    listed = _listed(getattr(service, 'runtime', None))
    count = (_walk(service, book, now=now, listed=listed, screening=True, limit=None, declined=declined)
             if listed else 0)
    if book.catching_up() or limit == 0:
        if limit != 0:
            _declined(service, declined, None, None, 'the accounting is catching up')
        return count
    return count + _walk(service, book, now=now, listed=listed, screening=False, limit=limit, declined=declined)


def _declined(service, declined, seat, record, guard) -> None:
    """Name the guard that kept a record where it is: to the caller, and once per record and guard in the log."""
    key = (seat, None if record is None else str(record), guard)
    if declined is not None:
        declined.append(key)
    if not callable(getattr(service, 'log', None)):
        return  # a stand-in service keeps no log, and is left as it was
    said = getattr(service, 'hand_transcripts_declined', None)
    if said is None:
        said = service.hand_transcripts_declined = set()
    if key not in said:
        said.add(key)
        service.log('hand-transcript-held', seat=seat, record=key[1], guard=guard)


def _walk(service, book, *, now, listed, screening, limit, declined=None):
    cutoff = (time.time() if now is None else now) - WEEK
    keys = reader_stamp.readers()
    count = 0
    for seat in service.resolved_seats():
        if seat.spec.role != 'hand' or seat.session.human:
            continue
        screened = listed.get(seat.name, set())
        if screening and not screened:
            continue
        try:
            with lifecycle.claim(service.runtime, seat.name):
                launched, current = seat.launched(), seat.record()
                if launched is None or current is None and not screening:
                    _declined(service, declined, seat.name, None, 'the current launch or record is not established')
                    continue  # the current launch/record is not established; a screening record needs only the launch
                launch_at = datetime.fromisoformat(launched.at).astimezone(timezone.utc)
                events = record_owner.events(seat.trusted / trusted_files.LAUNCHES)
                for owned in record_owner.records(seat):
                    record = owned.path
                    if (str(record) in screened) != screening:
                        continue  # each walk takes its own records: the screening ones first, the others after
                    kept = record_location.destination(record)
                    guard = ('no reader for it' if owned.reader is None else
                             'its ownership is in question' if owned.reason is not None else
                             'it is the current record' if record == current else
                             'no place to retain it' if kept is None else
                             'not a file' if not record.is_file() else
                             'already retired' if record_location.retired(record) else
                             'outside the seat\'s home' if not record.is_relative_to(seat.home) else None)
                    if guard is not None:
                        _declined(service, declined, seat.name, record, guard)
                        continue
                    joins = [event for event in events if event['record'] == str(record)]
                    if not joins or any((at := datetime.fromisoformat(e['at']).astimezone(timezone.utc))
                                        >= launch_at or not screening and at.timestamp() >= cutoff for e in joins):
                        _declined(service, declined, seat.name, record,
                                  'never joined' if not joins else
                                  'joined at or after this launch' if any(
                                      datetime.fromisoformat(e['at']).astimezone(timezone.utc) >= launch_at
                                      for e in joins) else 'joined within the week')
                        continue
                    bundle = owned.reader.retirement_bundle(seat.home, record, kept)
                    if bundle is None:
                        _declined(service, declined, seat.name, record, 'no retirement bundle')
                        continue
                    unit, target, files = bundle
                    if not screening and any(p.stat().st_mtime >= cutoff for p in files):
                        _declined(service, declined, seat.name, record, 'written within the week')
                        continue
                    stamp = (owned.name, record_fingerprint(record))
                    checkpoint = book.accounting_evidence(seat.name, stamp, owned.reason,
                        owning_launch_evidence(events, str(record)), keys.get(owned.name))
                    if checkpoint is None or not book.accounted(record, checkpoint):
                        _declined(service, declined, seat.name, record, 'not accounted at its checkpoint')
                        continue
                    state, _ = owned.read()
                    if (state.turn_open is not False or state.turns is None or state.open_tool
                            or state.pending_tools != () or state.queued != () or state.queue_error):
                        _declined(service, declined, seat.name, record, 'a turn, a tool or a queue is still open')
                        continue
                    before = {p: (p.stat().st_dev, p.stat().st_ino, p.stat().st_size,
                                  p.stat().st_mtime_ns, p.stat().st_ctime_ns) for p in files}
                    if opened(files, owned.reader.retirement_process_names):
                        _declined(service, declined, seat.name, record, 'a process may hold its files')
                        continue
                    if stamp != (owned.name, record_fingerprint(record)):
                        _declined(service, declined, seat.name, record, 'changed since it was accounted')
                        continue
                    checked_bundle = owned.reader.retirement_bundle(seat.home, record, kept)
                    if checked_bundle is None:
                        _declined(service, declined, seat.name, record, 'no retirement bundle')
                        continue
                    checked_unit, checked_target, checked = checked_bundle
                    after = {p: (p.stat().st_dev, p.stat().st_ino, p.stat().st_size,
                                 p.stat().st_mtime_ns, p.stat().st_ctime_ns) for p in checked}
                    if (checked_unit, checked_target) != (unit, target) or before != after:
                        _declined(service, declined, seat.name, record, 'changed while it was checked')
                        continue
                    if target.exists():
                        _declined(service, declined, seat.name, record, 'a retained copy is already there')
                        continue  # never replace retained evidence
                    target.parent.mkdir(parents=True, exist_ok=True)
                    unit.rename(target)  # same runtime/filesystem; DB, WAL and SHM stay together
                    count += 1
                    service.log('hand-transcript-retired', seat=seat.name, record=str(record),
                                **({'screening': True} if screening else {}))
                    if limit is not None and count >= limit:
                        return count
        except lifecycle.Busy:
            _declined(service, declined, seat.name, None, 'the seat is busy')
            continue
    return count


def due(service):
    if getattr(service, 'ledger', None) is None:
        return
    driver = getattr(service, 'account_driver', None)
    if driver is not None and driver.active:
        return  # retry at the next completed-job boundary, without delaying another five minutes
    now = time.monotonic()
    if now < getattr(service, 'hand_transcripts_due', 0):
        if _listed(getattr(service, 'runtime', None)):
            run(service, limit=0)  # a screening record does not wait for the interval: the screening walk alone
        return
    if service.ledger.catching_up():
        if _listed(getattr(service, 'runtime', None)):
            run(service, limit=0)  # through a catch-up, the screening walk alone
        return
    service.hand_transcripts_due = now + INTERVAL
    run(service)
