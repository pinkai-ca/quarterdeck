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
            try:
                name = (process / 'comm').read_text().strip()
            except FileNotFoundError:
                continue
            except OSError:
                return True
            if process_names is None or name in process_names:
                return True
    return False


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


def run(service, *, now=None, limit=1):
    """Move bounded records only between completed accounting jobs, under seat ownership.

    A screening launch's record holds an outsider's text (pr_screen), so it goes first: every hand's listed records
    in one walk, outside the limit and through a catch-up, without the week's wait. Each still waits for its own
    accounting checkpoint and every other check. Then, at most ``limit`` ordinary records, as before."""
    book = getattr(service, 'ledger', None)
    if book is None:
        return 0
    driver = getattr(service, 'account_driver', None)
    if book.db.in_transaction or driver is not None and driver.active:
        return 0  # never while an accounting job is open
    listed = _listed(getattr(service, 'runtime', None))
    count = _walk(service, book, now=now, listed=listed, screening=True, limit=None) if listed else 0
    if book.catching_up() or limit == 0:
        return count
    return count + _walk(service, book, now=now, listed=listed, screening=False, limit=limit)


def _walk(service, book, *, now, listed, screening, limit):
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
                    continue  # the current launch/record is not established; a screening record needs only the launch
                launch_at = datetime.fromisoformat(launched.at).astimezone(timezone.utc)
                events = record_owner.events(seat.trusted / trusted_files.LAUNCHES)
                for owned in record_owner.records(seat):
                    record = owned.path
                    if (str(record) in screened) != screening:
                        continue  # each walk takes its own records: the screening ones first, the others after
                    kept = record_location.destination(record)
                    if (owned.reader is None
                            or owned.reason is not None or record == current or kept is None
                            or not record.is_file() or record_location.retired(record)):
                        continue
                    if not record.is_relative_to(seat.home):
                        continue
                    joins = [event for event in events if event['record'] == str(record)]
                    if not joins or any((at := datetime.fromisoformat(e['at']).astimezone(timezone.utc))
                                        >= launch_at or not screening and at.timestamp() >= cutoff for e in joins):
                        continue
                    bundle = owned.reader.retirement_bundle(seat.home, record, kept)
                    if bundle is None:
                        continue
                    unit, target, files = bundle
                    if not screening and any(p.stat().st_mtime >= cutoff for p in files):
                        continue
                    stamp = (owned.name, record_fingerprint(record))
                    checkpoint = book.accounting_evidence(seat.name, stamp, owned.reason,
                        owning_launch_evidence(events, str(record)), keys.get(owned.name))
                    if checkpoint is None or not book.accounted(record, checkpoint):
                        continue
                    state, _ = owned.read()
                    if (state.turn_open is not False or state.turns is None or state.open_tool
                            or state.pending_tools != () or state.queued != () or state.queue_error):
                        continue
                    before = {p: (p.stat().st_dev, p.stat().st_ino, p.stat().st_size,
                                  p.stat().st_mtime_ns, p.stat().st_ctime_ns) for p in files}
                    if (opened(files, owned.reader.retirement_process_names)
                            or stamp != (owned.name, record_fingerprint(record))):
                        continue
                    checked_bundle = owned.reader.retirement_bundle(seat.home, record, kept)
                    if checked_bundle is None:
                        continue
                    checked_unit, checked_target, checked = checked_bundle
                    after = {p: (p.stat().st_dev, p.stat().st_ino, p.stat().st_size,
                                 p.stat().st_mtime_ns, p.stat().st_ctime_ns) for p in checked}
                    if (checked_unit, checked_target) != (unit, target) or before != after:
                        continue
                    if target.exists():
                        continue  # never replace retained evidence
                    target.parent.mkdir(parents=True, exist_ok=True)
                    unit.rename(target)  # same runtime/filesystem; DB, WAL and SHM stay together
                    count += 1
                    service.log('hand-transcript-retired', seat=seat.name, record=str(record),
                                **({'screening': True} if screening else {}))
                    if limit is not None and count >= limit:
                        return count
        except lifecycle.Busy:
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
