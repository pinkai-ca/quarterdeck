"""Retain accounted, closed hand transcripts outside the seat's write allowance."""

from datetime import datetime, timezone
import os
from pathlib import Path
import time

from . import lifecycle, record_location, record_owner, trusted_files
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


def run(service, *, now=None, limit=1):
    """Move bounded records only between completed accounting jobs, under seat ownership."""
    book = getattr(service, 'ledger', None)
    if book is None:
        return 0
    driver = getattr(service, 'account_driver', None)
    if book.catching_up() or book.db.in_transaction or driver is not None and driver.active:
        return 0
    cutoff = (time.time() if now is None else now) - WEEK
    keys = reader_stamp.readers()
    count = 0
    for seat in service.resolved_seats():
        if seat.spec.role != 'hand' or seat.session.human:
            continue
        try:
            with lifecycle.claim(service.runtime, seat.name):
                launched, current = seat.launched(), seat.record()
                if launched is None or current is None:
                    continue  # the current launch/record is not established
                launch_at = datetime.fromisoformat(launched.at).astimezone(timezone.utc)
                events = record_owner.events(seat.trusted / trusted_files.LAUNCHES)
                for owned in record_owner.records(seat):
                    record = owned.path
                    kept = record_location.destination(record)
                    if (owned.reader is None
                            or owned.reason is not None or record == current or kept is None
                            or not record.is_file() or record_location.retired(record)):
                        continue
                    if not record.is_relative_to(seat.home):
                        continue
                    joins = [event for event in events if event['record'] == str(record)]
                    if not joins or any((at := datetime.fromisoformat(e['at']).astimezone(timezone.utc))
                                        >= launch_at or at.timestamp() >= cutoff for e in joins):
                        continue
                    bundle = owned.reader.retirement_bundle(seat.home, record, kept)
                    if bundle is None:
                        continue
                    unit, target, files = bundle
                    if any(p.stat().st_mtime >= cutoff for p in files):
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
                    service.log('hand-transcript-retired', seat=seat.name, record=str(record))
                    if count >= limit:
                        return count
        except lifecycle.Busy:
            continue
    return count


def due(service):
    if getattr(service, 'ledger', None) is None:
        return
    now = time.monotonic()
    if now < getattr(service, 'hand_transcripts_due', 0):
        return
    driver = getattr(service, 'account_driver', None)
    if service.ledger.catching_up() or driver is not None and driver.active:
        return  # retry at the next completed-job boundary, without delaying another five minutes
    service.hand_transcripts_due = now + INTERVAL
    run(service)
