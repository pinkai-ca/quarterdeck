"""The Service: the ship's one long-running process, not a seat.

Started by ``qd up`` and owned by whoever ran it, it renders inboxes, delivers messages
through the doors, writes attempts, answers requests (controls, stops, resends), starts
workers fresh after their orders, re-briefs seats when the orders change, and writes the
heartbeat the status line reads. Every tick is one pass; nothing waits on a clock longer
than the tick (law: events-not-clocks).
"""

from __future__ import annotations

import contextlib
import errno
import json
import hashlib
import os
import signal
import sqlite3
import sys
import time
import traceback
import threading
from copy import deepcopy
from contextlib import ExitStack, contextmanager
from typing import Iterator
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from . import trusted_files
from . import harness as harnesses
from . import service_status, service_timing, service_wait
from . import shutdown, lifecycle
from .harness import held_reading, reader_stamp
from . import pending as pending_delivery, delivery_record
from . import accounting_health, credential, durable, idle, install, machine, move_hold, post, post_dir, presentation, quota, record_owner, recovery, servicectl, ship as shipmod, standing
from .door import CONFIRM_SECONDS, Door
from .harness.base import record_fingerprint
from .harness.capabilities import validate_effort, validate_mode
from . import ledger
from .ledger import Ledger
from .provenance import UNKNOWN
from .post import Attempts, Store
from .seat import SEAT_HOLDS, Seat, SelectionError, UnresolvedSeat, launch_pool_refusal, seats
from .servicectl import (  # noqa: F401  (the process control, reachable as service.* too)
    acquire_lock, alive, heartbeat_path, locked, read_heartbeat, remove_heartbeat, request, stale,
    start, stop, write_heartbeat,
)
from .ship import Ship, ShipError
from .tmux import Tmux, TmuxError, ship_sessions

TYPING_QUIET = 30  # seconds since a client's last key or mouse input before its session may be switched
STATUS_INTERVAL = 0.7  # cheap change checks; deadlines start after the check/collection finishes
FULL_STATUS_INTERVAL = 5.0  # a non-post change collects in full at most this often; post traffic refreshes post facts
ACCOUNT_RETRY = 60.0  # unchanged transient source failures, never provider pacing
QUICK_REQUEST_SECONDS = 0.02
QUICK_REQUEST_OPERATIONS = 128


def empty_record(fingerprint: tuple) -> bool:
    """A record present and empty, with no write-ahead log: it holds no turn, whatever program wrote it."""
    record, wal = fingerprint
    return record[0] and record[2] == 0 and not wal[0]


def settlement_of(stamp: tuple, reason: str | None) -> str:
    """What settles a record of unknown reader: its bytes on disk and why its reader is unknown.

    A changed record, or a reader named for it (its stamp's reader name), is a new attempt; the
    rest of the seat's registry and its launch prices cannot change what an unreadable record gives.
    """
    return "v1:" + hashlib.sha256(json.dumps([stamp, reason], default=str).encode()).hexdigest()

# Door alarms only a person can clear: a sign-in (every adapter names that dialog "login"),
# or a delivery whose paste or submit outcome is uncertain (the pending journal's own reasons).
# This reason category does not determine routing: crew alarms go to systems
# first, with the desk's escalation owned by the durable alarm reading.
PERSON_ONLY = (
    "login",
    "paste/submit outcome uncertain",
    "pending delivery",
    "pending box changed",
    "pending folded box",
    "prior launch has an unconfirmed delivery",
    "human wake",
    "inbox watch",
)


def person_only(reason: str) -> bool:
    from .harness.types import AUTHENTICATION
    return reason.startswith((*PERSON_ONLY, AUTHENTICATION))
BLIND_INTERVAL = 60.0  # how often the blind attempts are read against the records
OWN_TURN_RULE = object()  # handoff_fresh does not decide: the asking turn's own rule does
TELL_HELD_AFTER = 60.0  # seconds one tool-state hold of a waiting relaunch stands before its seat is told; when to tell, never proof


def ship_file_stamp(ship) -> tuple | None:
    """The ship file's identity and change times, or None where it is not on disk."""
    try:
        info = ship.file.stat()
    except (OSError, AttributeError):
        return None
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def watch_cause(error: BaseException) -> str:
    """A failure's kind and a stable reason, never its message: a token or a path's secret part could be in it."""
    if isinstance(error, OSError) and error.errno in errno.errorcode:
        return f"{type(error).__name__} {errno.errorcode[error.errno]}"
    return type(error).__name__


class Service:
    def __init__(self, ship: Ship, tmux: Tmux):
        self.ship, self.tmux = ship, tmux
        self.ship_stamp = None  # the ship file's identity as this pass's load read it; None: not loaded by a pass
        self.runtime = ship.runtime
        self.store = Store(self.runtime)
        from .fleet_post import ExportWorklist
        self.fleet_exports = ExportWorklist()
        self.inbox_certificate = None  # a new service always renders its first inbox pass
        self.attempts = Attempts(self.runtime)
        self.drop_filer = post.FilingSlice()
        self.quick_request_scan = None
        self.quick_request_retry = None
        self.stale_claims_pending = True
        self.log_path = self.runtime / "log" / "service.jsonl"
        self.withheld: dict[tuple[str, str], str] = {}  # (message, seat) -> the screen it was withheld on
        self.told: dict[str, str | None] = {}  # seat -> the obstruction the desk was last told of
        self.wake_errors: set[tuple[str, str]] = set()  # safe seat/reason log deduplication only
        self.status_framework = presentation.framework_stamp()
        self.brief_stamp = self._brief_stamp()
        copy = install.installed(self.runtime) if install.running_from(self.runtime / 'venv' / 'bin') else None
        expected = {key: copy.get(key) for key in ('commit', 'instance', 'package_digest')} if copy else None
        self.copy_identity = install.LOADED_COPY if install.LOADED_COPY == expected else None
        self.framework_commit = (self.copy_identity['commit'] if self.copy_identity is not None
                                 else copy['commit'] if copy else None)
        self.copy_start_token = os.environ.pop('QD_COPY_START', None)
        self.ledger = Ledger(self.runtime / "ledger.sqlite", usage_outcome=self._usage_commit_outcome)
        reader_keys = reader_stamp.attributions()
        self.accounting_epoch = reader_keys if all(reader_keys.values()) else None
        self.ledger.begin_catchup(self.accounting_epoch)
        self.meters: dict[str, machine.Meter] = {}
        self.usage_read: dict[str, float] = {}
        self.usage_hold: dict[str, float] = {}  # a refused pool's Retry-After, by the monotonic clock
        self.usage_logged: dict[str, str] = {}  # each pool's last logged usage failure
        self.read_marks: dict[Path, tuple] = {}  # record -> the fingerprint it was last read at
        # record -> when its mark, made inside a reader's time-bound unknown, is read again though its bytes stand still
        self.read_holds: dict[Path, held_reading.Hold] = {}
        self.account_errors: dict[Path, tuple] = {}  # evidence, error, next transient read time
        self.accounting_health = accounting_health.Health()  # a new process starts unchecked
        self.usage_accounting_health = accounting_health.Health()
        self.accounting_health_error = None
        self.accounting_health_due = 0.0
        self.heartbeat_lock = threading.Lock()
        self.credential_watch: tuple[list, list[Path]] = ([], [])  # (seats, linked names), from the last pass
        self.working: dict[str, bool] = {}  # seat -> whether its record last showed a turn open
        self.work_marks: dict[str, tuple] = {}  # seat -> (record, fingerprint) its working state was read at
        self.blind_checked = -BLIND_INTERVAL  # when the blind attempts were last read against the records
        self.loaded_attempts: list | None = None  # the attempts reconcile loaded this pass; the look reads these
        self.next_status = 0.0
        self.status_checked: float | None = None
        self.status_inputs = None
        self.status_refresh = 0.0
        self.status_full_after = 0.0  # a non-post change waits until then for its full collection
        self.status_kept = None  # the last full collection, which a post refresh reuses (watch.Collection)
        self.status_request = ('full', frozenset())  # what publish_status is asked for: full, or post with door seats
        self.status_done = None  # what publish_status did: 'full' or 'post'
        from .structure_outcome import Passes
        self.structure_passes = Passes()
        self.earlier_installer: tuple[int, str] | None = None  # an earlier-code installer that started this service
        self.legacy_lock: int | None = None  # the earlier service lock, held for that installer until it exits

    def run(self) -> None:
        self.lock = acquire_lock(self.runtime)  # one service per ship, for its whole life
        from . import install
        if install.EARLIER_INSTALLER is not None:
            # Started by an installer of the earlier code, which sees a service only at the lock's earlier place.
            self.earlier_installer = install.EARLIER_INSTALLER
            self.legacy_lock = servicectl.hold_legacy(self.runtime)
        servicectl.acknowledge(self.runtime, self.copy_identity, self.copy_start_token)
        self.log("start", pid=os.getpid())
        self.clean_server_environment()
        self.move_seat_files()
        (self.runtime / SEAT_HOLDS).unlink(missing_ok=True)  # an earlier service's holds are its own; this one observes anew
        self.first_beat()
        from .shutdown import Stop
        beat_stop = threading.Event()
        def keep_heartbeat():
            while not beat_stop.wait(2.0):
                try:
                    with self.heartbeat_lock:
                        self.beat()
                except OSError:
                    pass  # the main step reports storage failures through its usual path
        beat = threading.Thread(target=keep_heartbeat, name='service-heartbeat', daemon=True)
        beat.start()
        watch = threading.Thread(target=self.watch_credentials, args=(beat_stop,), name='service-credentials', daemon=True)
        watch.start()
        from . import jail_end, write_bound  # noqa: F401  (jail_end registers its hold with relaunch_intent)
        bound = write_bound.Alarm(self).start()  # its own thread and period: a hung pass never delays it
        arrivals = service_wait.Arrivals(self.runtime)
        try:
            with Stop(self.runtime, self.lock).installed():
                self.claim_service_start = None
                try:
                    info = servicectl.lock_path(self.runtime).lstat()
                    owner = shutdown.owner(self.runtime, os.getpid())
                    if (owner is not None and owner['start'] == servicectl.process_start(os.getpid())
                            and (owner['device'], owner['inode']) == (info.st_dev, info.st_ino)):
                        self.claim_service_start = dict(ctime_ns=info.st_ctime_ns, device=info.st_dev)
                except OSError:
                    pass  # no published start: legacy claims stay held; the service loop still runs
                self.settle_stale_claims()
                while True:
                    self.loop_stamp('pass')
                    self.tick()
                    self.loop_stamp('drain_accounting')
                    with self.timings().measure('drain_accounting'):
                        self.drain_accounting()
                    self.loop_stamp('arrivals')
                    arrivals.refresh(self.ship.seats)
                    self.loop_stamp('rest')
                    with self.timings().measure('sleep'):
                        arrivals.wait(self.ship.tick)  # arrival or one tick; no full status work in this rest
                    self.timings().publish(self.log)
        finally:
            arrivals.close()
            beat_stop.set()
            beat.join(timeout=0.2)
            watch.join(timeout=0.2)
            bound.close()
            if driver := getattr(self, 'account_driver', None):
                driver.close()

    def rebind(self) -> None:
        """A service that found its ship on tmux's default server follows it to its own once it has moved.

        The move is a ``qd down`` and a ``qd up``; a service left running across it would otherwise keep
        reading a server that no longer holds the ship. A binding to the old server, inherited from a seat
        launched before the move, is obsolete then and is dropped from this process."""
        if not getattr(getattr(self, "tmux", None), "legacy", False):
            return
        try:
            if ship_sessions(self.tmux, self.ship):
                return
            if os.environ.get("QD_TMUX_SOCKET") == self.tmux.socket:
                os.environ.pop("QD_TMUX_SOCKET")
            found = Tmux.for_ship(self.ship)
        except TmuxError as error:
            self.log("error", step="tmux", error=str(error))
            return
        if found.socket != self.tmux.socket:
            self.log("tmux", socket=found.socket, was=self.tmux.socket)
            self.tmux = found

    def release_earlier_installer(self) -> None:
        """Once the earlier-code installer that started this service has exited, its lock's earlier place goes."""
        if getattr(self, 'legacy_lock', None) is None:
            return
        pid, start = self.earlier_installer
        try:
            if servicectl.process_start(pid) == start:
                return
            servicectl.release_legacy(self.runtime, self.legacy_lock)
        except Exception as error:  # outside the steps' guard: a failure here must not stop the service
            self.log("error", step="release_earlier_installer", error="earlier-installer release failed",
                     kind=type(error).__name__, detail=str(error))
            return  # the lock is still held; the next pass tries again
        self.legacy_lock = None
        self.log("earlier-installer-done", pid=pid)

    def timings(self):
        if getattr(self, 'service_timings', None) is None:
            self.service_timings = service_timing.Timings(changes=lambda: self.ledger.db.total_changes)
        return self.service_timings

    def tick(self) -> None:
        with self.timings().measure('pass'):
            self._tick()
        self.timings().publish(self.log)

    def _tick(self) -> None:
        """One pass: the ship file read afresh, then every duty, each failing alone."""
        from . import post_retirement
        self.pass_had_work = False
        self.retirement_inputs = post_retirement.activity_stamp(getattr(self, 'runtime', None))
        self.release_earlier_installer()
        try:
            stamp = ship_file_stamp(self.ship)  # before the read: a write during it reads as a change later
            self.ship = shipmod.load(self.ship.folder)  # law: ship-file
            self.ship_stamp = stamp
        except ShipError as error:
            self.log("error", step="load", error=str(error))
        self.rebind()
        self.structure_passes.begin()
        self._accounting_tick = True
        try:
            with self._track_usage_pass():
                self._tick_steps()
        finally:
            self._accounting_tick = False
            self.loop_stamp('pass_end')
            self.structure_passes.finish()
            self.log_parse_cache()

    def log_parse_cache(self) -> None:
        """The record cache's size, read once a pass, so its bound is seen live (harness/json_record). It is written
        with the minute's service-timing line, not a line of its own: an idle pass writes nothing to the log."""
        from .harness import json_record
        records, held = json_record.cache_size()
        self.timings().cache_reading(records, held)

    def _tick_steps(self):
        # Operational duties keep their order and never wait behind the slice. Every stretch of the loop is stamped
        # under its own name: the drain and the status work between duties too, so neither reads as the duty before.
        self.loop_stamp('drain')
        self._drain_between_duties()
        for step in (self.server_recovery, self.relaunch_intents, self.reconcile, self.file_drops, self.groups, self.cascades, self.fleet_post, self.inboxes, self.views, self.recoveries, self.refusals, self.set_asides, self.answers, self.looks, self.jailed_watches, self.write_bound_floor, self.deliveries, self.follow, self.requests, self.fresh_handoffs, self.compactions, self.output_limits, self.person_quiet, self.fresh_after_order,
                     self.rebrief, self.push_reminders, self.credentials, self.authentications, self.model_differences, self.failover, self.retire_hand_transcripts, self.account, self.refresh_usage, self.pool_forecasts, self.record_growth, self.retire_post):
            self.loop_stamp(step.__name__)  # a slow duty reads as busy, then stuck, never as ok; the beat thread
            self.heartbeat()  # proves only that the process is alive, never that this loop is moving
            try:
                with self.timings().measure(step.__name__), Service.non_accounting(self, step.__name__):
                    step()
            except Exception as error:  # one failing step never stops the others
                self.log("error", step=step.__name__, error=repr(error), trace=traceback.format_exc())
            self.loop_stamp('drain')
            self._drain_between_duties()
            self.loop_stamp('status')
            self.status_due()
            self.timings().publish(self.log)

    # The duties whose ledger rows may be counted apart from the accounting gate's other writers, and the tables
    # those rows may touch: usage refresh writes its cache, pacing and receipt tables. No record accounting reads
    # them. A duty's rows are counted apart only when every table it wrote, triggers included, is one of these.
    NON_ACCOUNTING_DUTIES = frozenset(('refresh_usage',))
    NON_ACCOUNTING_TABLES = frozenset(('pools', 'usage_samples', 'usage_reads', 'usage_commit'))

    @contextmanager
    def non_accounting(self, name):
        if name not in Service.NON_ACCOUNTING_DUTIES:
            yield
            return
        try:
            db = self.ledger.db
            before = db.total_changes
        except Exception:  # a ledger being reopened: nothing is counted apart, and the barrier holds
            yield
            return
        tables = set()
        def authorize(action, table, *_):
            if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
                tables.add(table)
            return sqlite3.SQLITE_OK
        db.set_authorizer(authorize)
        try:
            yield
        finally:
            db.set_authorizer(None)
            after = db.total_changes
            # A write to any other table (an accounting one above all) leaves the whole duty another writer's.
            if after >= before and tables <= Service.NON_ACCOUNTING_TABLES:
                self.non_accounting_writes = getattr(self, 'non_accounting_writes', 0) + after - before

    def _drain_between_duties(self):
        for name in ('drain_drops', 'drain_accounting', 'drain_requests'):
            with self.timings().measure(name):
                getattr(self, name)()

    def drain_drops(self) -> None:
        """Give queued post a bounded turn between duties, independently of pass length."""
        try:
            self.file_drops()
        except Exception as error:
            self.log('error', step='file_drops', error=repr(error), trace=traceback.format_exc())

    def drain_requests(self) -> None:
        """Quick requests between duties, under the existing request owner.

        Like filing, the budget is cooperative: a file inspection or one quick
        request finishes before yielding, within its existing door/lock bounds.
        Lifecycle requests never run here; the full request step still owns them.
        """
        from . import request_queue
        if self.quick_request_scan is None:
            self.quick_request_scan = request_queue.arrival_paths(self.runtime, kinds=request_queue.QUICK_KINDS)
        deadline = time.monotonic() + QUICK_REQUEST_SECONDS
        try:
            for _ in range(QUICK_REQUEST_OPERATIONS):
                path = self.quick_request_retry
                self.quick_request_retry = None
                if path is None:
                    path = next(self.quick_request_scan)
                if path is not None and not self._requests((path,), kinds=request_queue.QUICK_KINDS):
                    self.quick_request_retry = path
                    break  # a busy owner cannot let a later arrival overtake this one
                if time.monotonic() >= deadline:
                    break
        except StopIteration:
            self.quick_request_scan = None
        except Exception as error:
            self.quick_request_scan = None
            self.quick_request_retry = None
            self.log('error', step='quick_requests', error=repr(error), trace=traceback.format_exc())

    @record_owner.reader_pass()
    def drain_accounting(self):
        """Use ready plans between duties; the accounting duty alone starts jobs."""
        if driver := getattr(self, 'account_driver', None):
            driver.drain(self)

    def pool_forecasts(self) -> None:
        from . import usage_forecast
        from .harness import claude_extra_usage as extra_usage
        usage_forecast.notify(self)
        extra_usage.notify(self)

    def record_growth(self) -> None:
        from . import record_growth
        record_growth.notify(self)

    def retire_hand_transcripts(self) -> None:
        if getattr(self, 'ledger', None) is None:
            return
        from . import hand_transcripts
        hand_transcripts.due(self)

    def retire_post(self) -> None:
        """Move only finished, settled delivery files after accounting catch-up."""
        from . import post_retirement
        before = getattr(self, 'retirement_inputs', None)
        driver = getattr(self, 'account_driver', None)
        idle_pass = (getattr(self, 'pass_had_work', True) is False and before is not None
                     and before == post_retirement.activity_stamp(self.runtime)
                     and (driver is None or getattr(driver, 'active', None) is False))
        post_retirement.due(self, idle=idle_pass)

    def status_due(self) -> None:
        self.timings().status_checks += 1
        self._status_due()

    def _status_due(self) -> None:
        now = time.monotonic()
        if self.status_checked is not None and self.status_checked <= now < self.next_status:
            return
        reversed_clock = self.status_checked is not None and now < self.status_checked
        self.status_checked = now
        try:
            sessions = [session.title for session in self.ship.sessions.values()]
            demand = self.tmux.clients_attached(sessions) if hasattr(self.tmux, 'clients_attached') else None
            profile = getattr(self, 'status_profile', None)  # an asked profile collects whether or not anyone watches
            if demand is False and profile is None:
                from . import status_snapshot
                if not status_snapshot.requested(self.ship):
                    return
                # Headless callers need the periodic whole reading only; post
                # traffic does not start extra collections between its periods.
                if (not reversed_clock and self.status_inputs is not None
                        and now < getattr(self, 'status_refresh', 0.0)):
                    return
            files = service_status.inputs(self.ship)
            # The post's traffic (store, attempts, journals, drops) refreshes only the post facts, over the last full
            # collection; any other change collects in full, at most once every FULL_STATUS_INTERVAL; the periodic
            # refresh collects in full. Beside the files: the service's own accounting state, held in memory and
            # read with the service's state at every publish. It changes only on a transition, never on a pass, so
            # a transition is published at once, by a post refresh, and the service's own churn forces nothing.
            accounting = accounting_health.visible(self).shown()
            post_now, other_now = (service_status.split(self.ship, files) if isinstance(files, tuple)
                                   else ((), files))
            last = getattr(self, 'status_inputs', None)
            # (A post refresh with no kept collection collects in full: publish_status falls back.)
            forced = reversed_clock or files is None or last is None or now >= getattr(self, 'status_refresh', 0.0)
            other_due = last is not None and other_now != last[1] and now >= getattr(self, 'status_full_after', 0.0)
            post_changed = last is not None and (post_now != last[0] or accounting != last[2])
            if forced or other_due:
                request = ('full', frozenset())
            elif post_changed:
                request = ('post', service_status.door_seats(self.ship, last[0], post_now))
            else:
                return
            self.heartbeat()
            self.status_request, self.status_done = request, None
            with self.timings().measure('status' if request[0] == 'full' else 'status_post'):
                if profile is not None and request[0] == 'full':
                    from . import status_profile
                    published = status_profile.run(self, profile, self.publish_status)
                else:
                    published = self.publish_status()
            if published is False:
                return
            # Remember the observation BEFORE collection: a change during it remains owed. No read or failed
            # collection renews the rendering. A post refresh keeps the other inputs owed as they were.
            finished = time.monotonic()
            if (self.status_done or request[0]) == 'full':
                self.status_inputs = (post_now, other_now, accounting)
                self.status_refresh = finished + self.ship.status_refresh
                self.status_full_after = finished + FULL_STATUS_INTERVAL
            else:
                self.status_inputs = (post_now, last[1], accounting)
        except Exception as error:
            self.log('error', step='publish_status', error=repr(error), trace=traceback.format_exc())
        finally:
            # A collection can cost more than an interval. Starting the deadline
            # here prevents the next duty/resting loop from being already overdue.
            self.next_status = time.monotonic() + STATUS_INTERVAL

    def publish_status(self) -> bool:
        """Publish what status_request asks: a full collection, or a post refresh over the kept one, which falls back
        to a full collection when it declines. status_done says which ran."""
        if getattr(self, 'journal_reader', None) is None:
            self.journal_reader = pending_delivery.JournalReader()
        mode, door_seats = getattr(self, 'status_request', ('full', frozenset()))
        self.status_request = ('full', frozenset())  # a request is used once; a direct call collects in full
        kept = getattr(self, 'status_kept', None)
        if getattr(self, 'status_readings', None) is None:
            from .watch import HeldReadings
            self.status_readings = HeldReadings()
        from .watch import held_readings
        # An unchanged native record is read once across collections, not again in each (watch.HeldReadings).
        with held_readings(self.status_readings), pending_delivery.journal_reading(self.journal_reader):
            if mode == 'post' and kept is not None:
                written, _ = presentation.publish_collected(self.ship, self.tmux, self.status_framework, self.store,
                                                            self.attempts, kept=kept, door_seats=door_seats)
                if written is not None:
                    self.status_done = 'post'
                    return written
            keep = []
            written = presentation.publish(self.ship, self.tmux, self.status_framework, self.store, self.attempts,
                                           keep)
            self.status_done = 'full'
            self.status_kept = keep[0] if keep else None
            return written

    def push_reminders(self) -> None:
        """A local origin/main change starts a durable reminder, without reading GitHub."""
        from . import push_reminder
        push_reminder.run(self)

    def first_beat(self) -> None:
        """At the start, a beat this service did not write goes, and its own first beat follows at once.

        A service that ended without clearing its beat (killed, or a sync that rolled back) leaves a dead pid in the
        folder, and the copy that moved the beat left one at its earlier place too; no reader is left with either."""
        with getattr(self, 'heartbeat_lock', None) or contextlib.nullcontext():
            remove_heartbeat(self.runtime)
            try:
                self.beat()
            except OSError:
                pass  # the beat thread and the main step try again; storage failures are reported there
        self.loop_stamp('start')

    def loop_stamp(self, duty: str) -> None:
        """The main loop's own stamp, as it begins a pass, a duty or its rest (servicectl.write_pass). Only the main
        loop writes it: the beat thread proves the process alive, this proves the loop is making passes, so a loop
        stuck in one duty reads busy, then stuck, never ok (law: nothing-waits-silently)."""
        runtime = getattr(self, 'runtime', None)
        if runtime is None:
            return  # a partial service built for one step's test has no runtime to stamp
        try:
            servicectl.write_pass(runtime, duty)
        except OSError:
            pass  # the status reading holds what it cannot read as unknown, never as ok

    def beat(self) -> None:
        """The heartbeat, in its folder (servicectl.write_heartbeat)."""
        write_heartbeat(self.runtime)

    def heartbeat(self) -> None:
        with self.heartbeat_lock:
            self.beat()
        self.publish_accounting_health()

    def publish_accounting_health(self, *, force=False) -> None:
        now = time.monotonic()
        if not force and now < self.accounting_health_due:
            return
        self.accounting_health_due = now + self.ship.tick  # once a pass, alongside the heartbeat
        try:
            accounting_health.publish(self.runtime, accounting_health.visible(self))
        except OSError as error:
            reason = accounting_health.cause(error)
            if self.accounting_health_error is None:
                self.log('accounting-health-unavailable', cause=reason)
            self.accounting_health_error = reason
        else:
            if self.accounting_health_error is not None:
                self.log('accounting-health-available')
            self.accounting_health_error = None

    def move_seat_files(self) -> None:
        """Once per seat, at the start: what the service once kept in a seat's own folder, which its jail can write,
        is considered and taken into the seat's trusted folder only where live state confirms it (seat_migration).
        Before any step reads a launch. Names and outcomes are logged, never contents."""
        from . import seat_migration

        server = getattr(self, 'tmux', None)  # the server it reaches its seats on; a seat whose windows it cannot read waits
        try:
            specs = list(self.ship.seats.values())
        except AttributeError as error:  # a step of the start never stops the service: logged, and the pass goes on
            self.log('seat-files-move-unchecked', reason=f'{type(error).__name__}: {str(error)[:200]}')
            return
        for spec in specs:
            try:
                outcome = seat_migration.migrate(self.ship, spec, server)
            except Exception as error:  # one seat's failure is that seat's: logged, and the others go on
                self.log('seat-files-move-failed', seat=spec.name, reason=f'{type(error).__name__}: {str(error)[:200]}')
                continue
            if outcome:
                self.log('seat-files-moved', seat=spec.name, outcome=outcome)

    def clean_server_environment(self) -> None:
        """Bring the ship's own running tmux server's global environment to the unjailed list, at each start.

        A server keeps the environment it started with, and gives it to every new window: one started before the
        declared lists, from a desktop's shell, would hand its secrets to every unjailed seat launched after. So
        every name the unjailed list would not keep is unset there. Only the ship's own server, on its own socket,
        never the operator's default server nor a test's; none running, nothing to do. Names are logged, never values.
        """
        from .jail_environment import unjailed_environment
        from .tmux import Tmux

        try:
            server = Tmux.for_ship(self.ship)
            if not server.ship or server.legacy or not server.running():
                return
            names = [line.split('=', 1)[0] for line in server.run('show-environment', '-g').splitlines()
                     if '=' in line and not line.startswith('-')]
            kept = unjailed_environment(dict.fromkeys(names, ''))
            removed = [name for name in names if name not in kept]
            for name in removed:
                server.run('set-environment', '-g', '-u', name)
        except Exception as error:  # a step of the start never stops the service: logged, and the pass goes on
            self.log('server-environment-unchecked', reason=f'{type(error).__name__}: {str(error)[:200]}')
            return
        if removed:
            self.log('server-environment-cleaned', names=removed)

    def log(self, event: str, **data) -> None:
        if event not in ('post-retired', 'service-timing'):
            self.pass_had_work = True
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a") as f:
            f.write(json.dumps({"at": datetime.now().astimezone().isoformat(timespec="seconds"), "event": event, **data}) + "\n")

    def seat(self, name: str) -> Seat:
        return Seat(self.ship, self.ship.seat(name), self.tmux)

    def refresh_ship(self) -> None:
        """Read the service's own ship again when its file changed since this pass loaded it; else keep it."""
        if self.ship_stamp is None:
            return  # not loaded by a pass from a file: nothing on disk to be newer than
        stamp = ship_file_stamp(self.ship)
        if stamp is None or stamp == self.ship_stamp:
            return
        self.ship = shipmod.load(self.ship.folder)  # law: ship-file; a refusal here is the request's refusal
        self.ship_stamp = stamp
        self.log("ship", reloaded="declaration changed since this pass loaded it")

    def hold_selection(self, error: SelectionError) -> None:
        from . import alarms
        reason = str(error)
        if self.told.get(error.seat) != reason:
            self.told[error.seat] = reason
            self.log('held', seat=error.seat, error=reason)
        spec = self.ship.seat(error.seat)
        alarms.notify(self, error.seat, self.ship.session_of(spec).human, reason,
                      f'{reason}; seat held, restore its declared selection', channel='selection',
                      up=self.systems_recipients(spec))

    @contextmanager
    def seat_guard(self, step: str, seat) -> Iterator[None]:
        """One seat's failure in a step over every seat holds that seat alone: logged once, named on the line.

        A seat's folder is written by its own jail, so a bad file there must never stop the step for the others.
        """
        try:
            yield
        except Exception as error:
            Service.seat_hold(self, step, seat, error)
        else:
            Service.seat_hold(self, step, seat, None)

    def seat_hold(self, step: str, seat, error: Exception | None) -> None:
        """Record (or, with None, clear) one seat's hold in one step: logged once, published for the line."""
        holds = self.__dict__.setdefault('seat_holds', {})
        key = (seat.name, step)
        if error is None:
            holds.pop(key, None)
        else:
            text = f'{type(error).__name__}: {str(error)[:200]}'
            if holds.get(key) != text:
                holds[key] = text
                self.log('error', step=step, seat=seat.name, error=text, held='this seat alone')
        Service._publish_seat_holds(self)

    def _publish_seat_holds(self) -> None:
        """runtime/seat-holds.json, written by the service alone, outside every seat's folder, when it changes."""
        holds = self.__dict__.get('seat_holds', {})
        if self.__dict__.get('seat_holds_written', {}) == holds:
            return  # nothing new to say; the start of the service clears what an earlier one wrote
        shown = {}
        for (name, step), text in sorted(holds.items()):
            shown.setdefault(name, {})[step] = text
        try:
            durable.replace_file(self.runtime / SEAT_HOLDS, json.dumps(shown, indent=2, sort_keys=True) + "\n")
        except OSError as error:
            self.log('error', step='seat-holds', error=repr(error))
            return
        self.seat_holds_written = dict(holds)

    def resolved_seats(self) -> list[Seat]:
        found = []
        for seat in seats(self.ship, self.tmux, False):
            if isinstance(seat, UnresolvedSeat):
                self.hold_selection(seat.error)
            else:
                from . import alarms
                if alarms.path(self.runtime, seat.name, 'selection').exists():
                    alarms.notify(self, seat.name, seat.session.human, None, None, channel='selection')
                found.append(seat)
        return found


    def server_recovery(self) -> None:
        """The ship's own tmux server proved gone: bring back exactly the formerly-up seats, at most twice an hour."""
        from . import server_recovery
        done = server_recovery.pass_once(self)
        if done and not done.startswith('unknown'):
            self.log('server-recovery', outcome=done)

    def relaunch_intents(self) -> None:
        """A relaunch a stopped service left between its down and its up: finished once, here, before anything else
        reads the seat (relaunch_intent)."""
        from . import relaunch_intent
        relaunch_intent.finish(self)

    def reconcile(self) -> None:
        """Reconcile journals first, then unjournaled attempts through the same fresh receipt rule.

        Live owners keep their confirm window; queued inputs need consumption.
        Blind attempts are re-read at their existing minute cadence, never resent.
        Journal recovery owns the first inventory. Reuse is local to this call;
        a metadata-only recheck sees intervening in-place or atomic changes. Settlements
        remain outside read-only views and retain their fresh write-owner checks.
        """
        self.loaded_attempts = None
        self._reconcile_attempt_inventory = None
        tracked = pending_delivery.reconcile(self)
        retry_blind = time.monotonic() - self.blind_checked >= BLIND_INTERVAL
        if retry_blind:
            self.blind_checked = time.monotonic()
        snapshot = self._reconcile_attempt_inventory
        self._reconcile_attempt_inventory = None
        records = self.attempts.recheck_inventory(snapshot) if snapshot is not None else self.attempts.all()
        self.loaded_attempts = records
        latest = self.attempts.latest(attempts=records)
        queued = {message for message, result in latest.items() if result == 'queued'}
        blind = {message for message, result in latest.items() if result == 'blind'}
        from . import watch_broker
        offered = watch_broker.offered(self.runtime)  # a jailed watch's attempts close on the broker's next pass
        for index, attempt in enumerate(records):
            if index % 200 == 0:
                self.heartbeat()
            if attempt.path in tracked or attempt.seat not in self.ship.seats:
                continue
            result = attempt.result
            if result is None:
                from .delivery_boundary import HOLD_DETAIL, held_path
                if held_path(self.runtime, attempt.path):
                    self.attempts.judge(attempt, "uncertain", HOLD_DETAIL)
                    continue
                if time.time() < attempt.when.timestamp() + CONFIRM_SECONDS:
                    continue
                if attempt.pane is None:
                    if (attempt.path.name in offered
                            and time.time() < attempt.when.timestamp() + watch_broker.OFFER_SECONDS):
                        continue  # offered to a jailed watch: its receipt is settled by the broker, which runs later
                    self.attempts.judge(attempt, "blind", "a watch opened it and never reported printing it; qd post resend")
                    continue
            elif not (result == "queued" and attempt.message in queued
                      or result == "blind" and retry_blind and attempt.message in blind):
                continue
            try:
                seat = self.seat(attempt.seat)
            except SelectionError as error:
                self.hold_selection(error)
                continue
            message = self.store.get(attempt.message)
            seen = delivery_record.receipt_anywhere(seat, message, attempt.receipt_since)
            if result is None:
                if self.attempts.judge(attempt, seen or "blind", "found in the record on reconciliation" if seen else
                                      "the service died mid-delivery and the record does not show it; qd post resend"):  # law: one-door
                    self.log("reconciled", message=message.id, seat=seat.name, result=seen or "blind")
            elif result == "queued":
                if seen == "delivered":
                    self.attempts.close(attempt, "delivered", "queued, then read: the record names it")
            elif seen:
                self.attempts.close(attempt, seen, "found in the record later")
                self.log("reconciled", message=message.id, seat=seat.name, result=seen,
                         detail="a blind attempt, found in the record later")
        from . import turn_failure
        turn_failure.settle(self)  # after receipts: a turn that ended in an error unsettles what it took

    def refusals(self) -> None:
        """A crew seat whose provider refused its conversation starts fresh, once, without the refused message."""
        from . import refusal
        refusal.run(self)

    def set_asides(self) -> None:
        """A message whose journal a systems seat set aside: marked and its sender told once, a cut completed."""
        from . import acts
        acts.settle_set_asides(self)

    def jailed_watches(self) -> None:
        """A jailed watch's attempts, opened and closed on its behalf; each seat alone, before the deliveries."""
        from . import watch_broker
        served = []
        for seat in self.resolved_seats():
            if not seat.session.human or seat.name in getattr(self.ship, 'forward', {}):
                continue
            served.append(seat)
        # qd check's human-wake seat is declared nowhere: only its check's live lease admits it, as qd inbox does.
        served += watch_broker.check_seats(self.ship)
        for seat in served:
            try:
                watch_broker.run(self, seat)
            except Exception as error:
                self.log('error', step='watch_broker', seat=seat.name, error=repr(error))

    def deliveries(self) -> None:
        with post.reader_scope(store=self.store, attempts=self.attempts):
            select_results = getattr(self.attempts, 'delivery_results', self.attempts.latest)
            results = select_results()
            for seat in self.resolved_seats():
                with Service.seat_guard(self, 'deliveries', seat):
                    if seat.name in getattr(self.ship, 'forward', {}):
                        continue  # declared forwarding disables dispatch at the old address
                    if seat.session.human:
                        from . import alarms, human_wake, turn_failure
                        from .auth_failure import status as authentication_status, waiting as authentication_waiting
                        phase = 'ownership'
                        try:
                            signin = authentication_status(seat)  # a sign-in failure the waking line may cure reads as waiting
                            if seat.is_up and not (recovery.held(seat) or signin and not authentication_waiting(signin)
                                                   or turn_failure.obstruction(seat, self.store)):
                                phase = 'pass'
                                human_wake.run(seat)
                        except Exception as error:
                            self.human_wake_error(seat, phase, error)
                        try:
                            self.needs_human(seat)  # genuine alarms survive a down seat or failed wake
                        except alarms.JournalError:
                            raise  # the delivery hold is visible even without a human wake launch
                        except Exception as error:
                            self.human_wake_error(seat, 'alarm', error)
                        continue
                    with post.reading_pass(store=self.store, attempts=self.attempts, delivery_candidates=True):
                        pending = [m for m in self.attempts.undelivered(self.store, seat.name, results=results) if self._due(m, seat)]
                    if pending and seat.is_up:
                        self.deliver(seat, pending)
                    self.needs_human(seat)

    def human_wake_error(self, seat: Seat, phase: str, error: Exception, *, publish: bool = True) -> None:
        """One seat's failure cannot stop the crew; no arbitrary exception data escapes."""
        from . import human_wake
        reason = human_wake.service_failure_reason(phase, error)
        key = (seat.name, reason)
        if key not in self.wake_errors:
            self.wake_errors.add(key)
            try:
                self.log('held', step='human_wake', seat=seat.name, reason=reason)
            except Exception:
                pass  # publication below remains independent of an unavailable service log
        if publish:
            try:
                human_wake.observe_failure(seat, reason)
            except Exception as failure:
                self.human_wake_error(seat, 'observation', failure, publish=False)

    def recoveries(self) -> None:
        recovery.run(self)

    def answers(self) -> None:
        """A question the orders answer for the account holder is answered as soon as it shows, message or not.

        Only for adapters that declare such a question, only in seats no human owns, through the door alone.
        """
        for seat in self.resolved_seats():
            if seat.session.human or not seat.harness.answers_questions or not seat.is_up:
                continue
            door = Door(seat)
            screen = door.screen()
            if screen is None or screen.interruption is None or screen.interruption.click is None:
                continue
            before = seat.launched()
            door.clear_known(screen)
            after = seat.launched()
            if after is not None and after != before and after.answered:
                done = after.answered[-1]
                self.log("answered", seat=seat.name, question=done.get("question"), choice=done.get("choice"),
                         pressed_at=done.get("at"), outcome=done.get("outcome"))

    def _due(self, message: post.Message, seat: Seat) -> bool:
        """A withheld message is tried again when the screen has changed since, not on a clock (law: events-not-clocks)."""
        last = self.withheld.get((message.id, seat.name))
        if last is None:
            return True
        screen = Door(seat).screen()
        return screen is None or screen.text != last  # law: events-not-clocks

    def needs_human(self, seat: Seat) -> None:
        """Observe the standing alarm; the shared owner routes it once per reading."""
        _, reason = Door(seat).human_obstruction()
        from .auth_failure import status as authentication_status, waiting as authentication_waiting
        authentication = authentication_status(seat)
        # A pending renewal stays visible in status; call a person only after its bounded retries.
        renewing = authentication_waiting(authentication)
        reason = None if renewing else authentication or reason
        from . import alarms, unconfirmed_alert
        if not reason:
            detached = pending_delivery.detached(seat)
            unconfirmed_alert.step(self, seat, detached)
            reason = next(iter(detached), None)
        # A door obstruction masks the detached observation; it does not clear
        # that standing reading or change the door alarm's ownership.
        if seat.session.human:
            from . import human_wake
            with post.reading_pass(store=getattr(self, 'store', None), attempts=getattr(self, 'attempts', None)):
                wake = human_wake.status(seat)
            reason = reason or (wake if human_wake.action_required(seat, wake) else None)
        if reason != self.told.get(seat.name):
            self.told[seat.name] = reason
        door_reason = reason if reason and not reason.startswith(unconfirmed_alert.PREFIX) else None
        alarms.notify(self, seat.name, seat.session.human, door_reason,
                      f'{seat.name} obstructed, {door_reason}' if door_reason else None,
                      desk_now=person_only(door_reason) if door_reason else False,
                      initial=self.alarm_recipients(seat, door_reason) if door_reason else [],
                      recover_initial=not renewing, ship=seat.ship)

    def alarm_recipients(self, seat: Seat, reason: str) -> list[str]:
        """Immediate recipients; a standing untaken alarm may reach the desk later."""
        from . import alarms
        systems = getattr(self, 'systems_recipients', None)
        up = systems(seat) if systems is not None else Service.systems_recipients(self, seat)
        return alarms.recipients(self, seat.name, seat.session.human, desk_now=person_only(reason),
                                 up=up)

    def write_bound_floor(self) -> None:
        """A seat stopped by its disk's free-space floor is told to the desk and the systems seats at once, by the
        alarm road a held seat's selection takes; the hold's lift clears it. The write-bound thread only writes holds."""
        from . import alarms, write_bound
        for spec in self.ship.seats.values():
            try:
                words = write_bound.floor_reason(self.runtime, spec.name)
                alarms.notify(self, spec.name, self.ship.session_of(spec).human, words,
                              f'{spec.name}: {words}' if words else None, channel='write-bound-floor',
                              desk_now=True, up=self.systems_recipients(spec))
            except Exception as error:  # one seat's alarm never keeps the others'
                self.log('error', step='write-bound-floor', seat=spec.name, error=repr(error))
            self.jail_sweep_alarm(spec)

    def jail_sweep_alarm(self, spec) -> None:
        """A stray sandbox the sweep ended is told to the systems seats, by the same alarm road; a second within a day
        goes to security as well, as a sign that the seats' sandboxes want a scope of their own."""
        from . import alarms, jail_end
        try:
            key, words, second = jail_end.alarm(self.runtime, spec.name)
            told = self.systems_recipients(spec)
            if second:
                told += [other.name for other in self.ship.seats.values()
                         if other.role == 'security' and other.name != spec.name and other.name not in told]
            alarms.notify(self, spec.name, self.ship.session_of(spec).human, key, words, channel='jail-sweep',
                          initial=told)
        except Exception as error:  # one seat's alarm never keeps the others'
            self.log('error', step='jail-sweep-alarm', seat=spec.name, error=repr(error))

    def systems_recipients(self, seat: Seat) -> list[str]:
        """Every up systems seat, excluding the subject of its own alarm."""
        return [spec.name for spec in self.ship.seats.values()
                if spec.role == 'systems' and spec.name != seat.name and self._up(spec.name)]

    def model_differences(self) -> None:
        from . import model_mismatch, request_queue
        if not hasattr(self, 'model_records'):
            self.model_records = model_mismatch.RecordModels()
        for seat in self.resolved_seats():
            try:
                model_mismatch.step(self, seat, request_queue.pending_model)
            except Exception as error:
                self.log('error', step='model-mismatch', seat=seat.name, error=repr(error))

    def _up(self, name: str) -> bool:
        try:
            return self.seat(name).is_up
        except ShipError:
            return False  # an unresolved or unconfirmed launch is not a seat that can take the alarm

    def deliver(self, seat: Seat, messages: list[post.Message], resend: bool = False) -> str:
        from . import lifecycle

        try:
            with lifecycle.claim(self.runtime, seat.name):
                return self._deliver_claimed(seat, messages, resend)
        except lifecycle.Busy as error:
            return str(error)

    def _deliver_claimed(self, seat: Seat, messages: list[post.Message], resend: bool = False,
                         carried_head: str | None = None) -> str:
        """One seat's batch, with its existing lifecycle claim already held. ``carried_head``: the standing-orders
        head an earlier typing of this one message held (the door's one look retypes it with it)."""
        if resend:
            from . import paste_refusal
            door = Door(seat)
            screen = door.screen()
            if reading := door.initial_obstruction(screen):
                return f'{seat.name}: {reading}'
            if len(messages)==1 and (reason:=paste_refusal.refused(seat,post.typed(messages),screen)):
                return f'{messages[0].short} to {seat.name} · withheld: {reason}'
            pending_delivery.explicit_resend(self, seat, messages, screen)
            from .delivery_boundary import message_held
            messages = [message for message in messages if not message_held(self.runtime, seat.name, message.id)]
        latest = self.attempts.last_results(seat.name)
        ordered = sorted(messages, key=lambda message: latest.get(message.id) == "withheld")
        lines = []
        for message in ordered:
            lines.append(self._deliver_current(seat, message, resend, carried_head))
            reading = Door(seat).reading()
            if not reading.open and reading.clears != "dismissable":
                break  # an occupied or uncertain door holds every remaining item
        return "\n".join(lines)

    def _deliver(self, seat: Seat, message: post.Message, resend: bool = False) -> str:
        """One isolated transport, journaled before typing."""
        from . import lifecycle

        try:
            with lifecycle.claim(self.runtime, seat.name):
                return self._deliver_current(seat, message, resend)
        except lifecycle.Busy as error:
            return str(error)

    def _deliver_current(self, seat: Seat, message: post.Message, resend: bool = False,
                         carried_head: str | None = None) -> str:
        """The caller owns the same seat claim as every lifecycle transition."""
        if message.id in self.store.settled_notices(seat.name):
            return f'{message.short} to {seat.name} · settled notice; not delivered'
        from . import pr_screen
        if not pr_screen.admits(self.runtime, seat.name, message.id):
            # A hand's conversation holds an outsider's text while its screen's hold stands: nothing but the screen's
            # own instruction is typed there. The message waits, unread and with no attempt, until the release.
            return f'{message.short} to {seat.name} · waits: {pr_screen.SCREENING}'
        from . import hand_door
        if held := hand_door.held(seat, self.store, getattr(self, 'attempts', None), message):
            # A hand holds one order at a time: what does not belong to its order in hand waits, unread and with
            # no attempt, for the report on it and the fresh start that carries the next (hand_door).
            return f'{message.short} to {seat.name} · {held}'
        door = Door(seat)
        speed = message.speed
        reading = door.reading()
        clearable = reading.clears == "dismissable" or (speed == "interrupt" and reading.clears == "time")
        if not reading.open and not clearable:  # the door itself clears a known notice or interrupts; this only spares an attempt file
            return f"{seat.name}: {reading}"
        with ExitStack() as delivery_scope:
            with post.admission(self.runtime, seat.name):
                if message.id in self.store.settled_notices(seat.name):
                    return f'{message.short} to {seat.name} · settled notice; not delivered'
                from . import compaction, fresh_handoff
                if fresh_handoff.blocks_delivery(self.runtime, message):
                    return f'{seat.name}: handoff waits for the next conversation'
                reserved, transitioning = compaction.withheld(self.runtime, seat.name)
                if transitioning or message.id in reserved:
                    return f"{seat.name}: compaction handoff/transition reserved; held for successor"
                if not pr_screen.admits(self.runtime, seat.name, message.id):  # a hold set since the first look
                    return f'{message.short} to {seat.name} · waits: {pr_screen.SCREENING}'
                if held := hand_door.held(seat, self.store, getattr(self, 'attempts', None), message):  # read again under admission
                    return f'{message.short} to {seat.name} · {held}'
                bound = pending_delivery.current_identity(seat)
                if bound is None:
                    return f'{seat.name}: current pane/process/launch unproved; message waits unread'
                # Read mutable history before the first delivery write, while TERM
                # may still exit immediately. Keep attempt creation through submit together.
                history = self.attempts.for_message(message.id)
                delivery_scope.enter_context(shutdown.transaction_scope(self))
                attempt = self.attempts.open(message, seat.name, bound['pane'], bound['pid'], bound['launch'])
                text = post.typed([message], second=(message.id,) if resend else ())  # a resend says it is one
                # A change to the seat's standing orders since it last read them rides at the head of this message,
                # typed with it as one input: the box check and the record's confirmation both take this whole text,
                # and every matcher reads the message past the head (input_receipt.without_brief_head).
                from . import brief_change
                if carried_head:
                    # The one look types again exactly what the first typing carried, head included; the brief it
                    # told of was marked told then, and a later change waits for the next message.
                    text, changed = post.line_ends(carried_head) + text, None
                elif changed := brief_change.head(self.runtime, seat.name):
                    text = post.line_ends(changed[0]) + text  # typed as the message is: no line ends in a space
                try:
                    proof = pending_delivery.Proof.begin(seat, attempt, text, before_paste=True, attempt_history=history)
                except pending_delivery.NoCurrentLaunch as error:
                    self.attempts.close(attempt, 'withheld', 'launch ended before reservation; no input typed')
                    return str(error)
            delivery = door.deliver(text, speed, proof=proof)
            if proof:
                proof.finish(delivery.result)
            detail = delivery.detail + (" · resend" if resend else "")
            screen = door.screen()
            self.attempts.close(attempt, delivery.result, detail)
            if changed and delivery.result != "withheld":
                told_errors = self.__dict__.setdefault('brief_told_errors', {})  # the last failure per seat, logged once
                try:
                    brief_change.mark(self.runtime, seat.name, changed[1])  # typed: the seat has been told
                except OSError as error:
                    # As the watch and the launch: left untold, the next message carries the head once more, and
                    # the delivery below is still logged. Never lost, never raised out of a delivery.
                    if told_errors.get(seat.name) != str(error):
                        told_errors[seat.name] = str(error)
                        self.log('error', step='brief-told', seat=seat.name, error=str(error))
                else:
                    told_errors.pop(seat.name, None)
            if delivery.result == "withheld":
                self.withheld[(attempt.message, seat.name)] = screen.text if screen else ""
            self.log("delivery", seat=seat.name, messages=[message.id], speed=speed, result=delivery.result, detail=detail)
            return f"{message.short} to {seat.name} · {delivery.result}: {detail}"

    def fleet_post(self) -> None:
        from . import fleet_post
        try:
            errors = fleet_post.run(self.ship, self.store, worklist=self.fleet_exports)
            path = self.runtime / 'post' / 'transport-holds.json'
            if errors:
                fleet_post.prepare_outbound(self.runtime)
                text = json.dumps(errors, sort_keys=True) + '\n'
                if not path.exists() or path.read_text() != text:
                    fleet_post.replace_status(path, text)
            else:
                try:
                    path.lstat()
                except FileNotFoundError:
                    pass
                else:
                    path.unlink(missing_ok=True)
        except BaseException:
            self.fleet_exports.certificate = None
            raise

    def views(self) -> None:
        """Each jailed seat's view of the post, after the inboxes and before any delivery types a message to it, from
        one snapshot of the store for the pass. A view no seat should have is removed: its seat is no longer declared
        and its folder is gone, or is no longer jailed and is not still running in a jail. One seat's failure
        never stops the others'."""
        from . import post_view
        try:
            snapshot = post_view.Snapshot(self.runtime)
        except Exception as error:
            self.log("error", step="views", error=repr(error))
            return
        for name in post_view.seats(self.runtime):
            try:
                if not self._view_kept(name):
                    post_view.remove(self.runtime, name)
                    self.log("view-removed", seat=name)
            except Exception as error:
                self.log("error", step="views", seat=name, error=repr(error))
        self._refresh_views(self._viewed_seats(), snapshot)

    def _viewed_seats(self) -> list[str]:
        """The declared seats whose view of the post the service keeps: a jailed seat, or one whose view stands."""
        from . import post_view
        return [name for name, spec in self.ship.seats.items()
                if self.ship.jailed(spec) or post_view.folder(self.runtime, name).is_dir()]

    def _refresh_views(self, names, snapshot=None) -> None:
        """Refresh these seats' views from one snapshot of the store; one seat's failure never stops the others'."""
        from . import post_view
        if snapshot is None:
            try:
                snapshot = post_view.Snapshot(self.runtime)
            except Exception as error:
                self.log("error", step="views", error=repr(error))
                return
        for name in names:
            try:
                post_view.refresh(self.runtime, name, snapshot)
            except Exception as error:
                self.log("error", step="views", seat=name, error=repr(error))

    def _view_kept(self, name: str) -> bool:
        spec = self.ship.seats.get(name)
        if spec is None:
            # A temporary seat (a check's) is undeclared: its folder stands from before its launch until its teardown
            # proves its program ended, and while it stands the jail may hold the view. No lock is taken to know it.
            return (self.runtime / "seats" / name).is_dir()
        if self.ship.jailed(spec):
            return True
        seat = self.seat(name)
        launched = seat.launched()
        from . import sandbox
        return launched is not None and sandbox.seats_hidden(launched.argv) is not False and seat.is_up

    def inboxes(self) -> None:
        from . import service_inbox
        service_inbox.refresh(self)

    def file_drops(self) -> None:
        """File each seat's drop. The service sets the sender from the folder; a refusal is held and logged once.

        Then the view of each viewed seat whose drop a message left is refreshed at once, in the same step: a jailed
        seat sees the post only through its view, so a message it just posted is in its drop or in its view, never
        in neither until the pass's views step. Only those seats, and only when a message left: a pass that files
        nothing of theirs reads no store listing for it."""
        try:
            before = {name: self._drop_names(name) for name in self._viewed_seats()}
        except Exception as error:  # the views never stop the filing; the pass's views step still refreshes them
            self.log("error", step="views", error=repr(error))
            before = {}
        for folder, reason in post.file_drops(self.store, self.ship, filing=self.drop_filer):
            self.log("drop-held", folder=folder, reason=reason)
        left = [name for name, names in before.items() if names - self._drop_names(name)]
        if left:
            self._refresh_views(left)
        self._take_thread_reads()

    def _take_thread_reads(self) -> None:
        """Receipt what each seat's qd thread claims it printed whole: the service, unjailed, writes the receipt into
        the seat's trusted folder, which no jail sees. A seat with no claim costs one lstat. A claim that cannot be
        taken is logged once while it stands (post_reads.refused); so is an error of the take itself, here."""
        from . import post_reads
        logged = self.__dict__.setdefault("thread_read_errors", {})
        for name in self.ship.seats:
            try:
                post_reads.take(self.runtime, name, lambda name=name: (
                    message.id for message in self.store.all() if name in (message.sender, message.to)))
            except Exception as error:  # one seat's claim never stops the filing of another's; logged once
                if logged.get(name) != repr(error):
                    logged[name] = repr(error)
                    self.log("error", step="thread-reads", seat=name, error=repr(error))
                continue
            logged.pop(name, None)

    def _drop_names(self, seat: str) -> set[str]:
        """The names in a seat's drop, by the post folder's no-follow listing, bounded as the filing's own window is,
        so a flooded drop costs no more here than there; none when it cannot be read."""
        try:
            return {name for name, _ in post_dir.list_children(self.runtime / "post" / "drop" / seat,
                                                               limit=post.DROP_MAX_FILES)}
        except (OSError, ShipError):
            return set()

    def groups(self) -> None:
        """Recover fixed group copies and publish one all-filed or failure notice."""
        from . import group_post, structure_outcome
        changed = group_post.advance(self.ship, self.store,
                                     lambda name, copy: group_post.member_failure(self, name, copy),
                                     outcomes=lambda name, copy: structure_outcome.observe(self, name, copy))
        for request, reason in changed:
            self.log('group-held', request=request, reason=reason)

    def cascades(self) -> None:
        from . import cascade
        changed = cascade.run(self)
        for request, reason in changed:
            self.log('cascade-held', request=request, reason=reason)


    def log_claim_settlement(self, path, data, meta):
        self.log('request-settled', request=path.stem, seat=data.get('seat'),
                 answer=meta['terminal'], claimer=meta.get('claim_owner'),
                 before_service_start=meta.get('claim_before_start'))

    def settle_stale_claims(self) -> bool:
        """On startup, finish dead service claims without ever dispatching them."""
        from . import request_queue
        self.stale_claims_pending = False
        if not (self.runtime / 'requests').exists():
            return True  # an absent queue has no claims; do not create a new arrival-watch target
        with request_queue.coordination(self.runtime, wait=False) as owned:
            if not owned:
                self.stale_claims_pending = True
                return False
            for path in sorted((self.runtime / 'requests').glob('*.json')):
                try:
                    start = getattr(self, 'claim_service_start', None)
                    if not request_queue.stale_claim(path, service_start=start):
                        continue
                    meta = request_queue.metadata(path)
                    data = request_queue.load(path)
                    if data.get('kind') not in request_queue.SEAT_KINDS:
                        continue
                    if 'control_confirmation' in meta:
                        continue  # its command may be typed: the ordinary resume confirms it, never retyping
                    if 'terminal' in meta and meta.get('settlement') != 'service-restarted':
                        continue  # cancellation/supersession keeps its own durable outcome
                    meta['terminal'] = request_queue.RESTART_SETTLEMENT
                    meta['settlement'] = 'service-restarted'
                    if 'claim_owner' not in meta:
                        meta['claim_before_start'] = dict(start,
                            claim_ctime_ns=path.with_suffix('.claimed').lstat().st_ctime_ns,
                            margin_ns=post.POST_TIMESTAMP_TICK_NS)
                    request_queue.save_metadata(path, meta)  # intent precedes log, answer and retirement
                    self.log_claim_settlement(path, data, meta)
                    Service.complete_request_notice(self, path, data, meta, meta['terminal'])
                    request_queue.settle(path, meta['terminal'], clear_claim=True)
                except FileNotFoundError:
                    continue
                except (OSError, ValueError, TypeError, ShipError) as error:
                    self.stale_claims_pending = True
                    self.log('error', step='stale-request-claims', request=path.name, error=repr(error))
        return True  # failed individual settlements retain claims; healthy requests may still run

    def requests(self) -> None:
        """Each request answered alone, in arrival order: none keeps a later one waiting.

        A rebrief persists its intent and posts each notice once, so one that fails stays to be tried
        again; its error is raised once every other request has had its answer, and the tick logs it.
        """
        from . import request_queue
        Service.prune_request_notices(self)
        self._requests(path for path in request_queue.arrival_paths(self.runtime) if path is not None)

    def _requests(self, paths, *, kinds=None) -> bool:
        """The one claim/dispatch/settlement path; False defers a busy quick candidate."""
        if getattr(self, 'stale_claims_pending', False) and not self.settle_stale_claims():
            return False  # no dispatch can overtake a busy startup settlement
        retry = []
        for path in paths:
            from . import request_queue
            with ExitStack() as ownership:
                if not ownership.enter_context(request_queue.coordination(self.runtime, wait=False)):
                    if kinds is not None:
                        return False
                    continue  # owner busy: defer without delaying the remaining service duties
                if not path.exists():
                    continue
                data = None
                try:
                    if kinds is not None and request_queue.load(path).get('kind') not in kinds:
                        continue
                    claim = path.with_suffix(".claimed")
                    meta = request_queue.metadata(path)
                    if 'terminal' in meta:
                        data = request_queue.load(path)
                        if kinds is not None and data.get('kind') not in kinds:
                            continue
                        if not isinstance(meta['terminal'], str) or not meta['terminal'] or data.get('kind') not in request_queue.SEAT_KINDS:
                            raise ShipError('request retirement intent unknown; retained')
                        if meta.get('settlement') == 'service-restarted':
                            self.log_claim_settlement(path, data, meta)
                        Service.complete_request_notice(self, path, data, meta, meta['terminal'])
                        request_queue.settle(path, meta['terminal'], clear_claim=True)
                        continue  # recover cancellation/supersession without ever dispatching it
                    data = request_queue.load(path)
                    if kinds is not None and data.get('kind') not in kinds:
                        continue
                    try:
                        continuation = request_queue.control_continuation(path, data, meta)
                    except (OSError, ValueError, TypeError, ShipError):
                        raise request_queue.RequestHold('control-confirmation',
                                                       'control continuation unknown; retained without retyping') from None
                    if (claim.exists() or claim.is_symlink()) and continuation is None:
                        continue  # an earlier action may have run; never replay an ambiguous claim
                    raw = path.read_text()
                    if continuation is None and not request_queue.supersede(self.runtime, path, data):
                        continue
                    meta = request_queue.metadata(path)  # supersession may add retired identities
                    if continuation is None and data.get("kind") != "rebrief":
                        request_queue.claim(path, raw, meta)  # before dispatch: preserve owner and immutable payload
                    # The durable claim excludes cancellation/supersession while dispatch runs.
                    # A seat transition must never hold the global request coordination lock.
                    ownership.close()
                    try:
                        self.request_wait = None
                        self.request_own = False  # set only by relaunch_request, from the recorded proof
                        self.request_metadata = meta
                        self.request_path = path
                        answer = self.answer(data)
                    except request_queue.RequestHold:
                        raise
                    except SelectionError as error:
                        self.hold_selection(error)
                        request_queue.hold(path, meta, 'selection', 'seat selection unknown or held')
                        if 'control_confirmation' not in meta:
                            self.clear_request_claim(claim)
                        continue  # this request stays held; later healthy-seat requests still run
                    except ShipError as error:
                        if isinstance(data, dict) and data.get("kind") == "rebrief":
                            raise  # an immutable notice conflict must retain the rebrief's durable baseline/notices
                        answer = f"refused: {error}"  # a refusal is an answer: its asker reads why
                    Service.tell_held(self, data, meta, answer)  # by the class: a stand-in service has no method of its own
                    if answer is None:
                        if isinstance(data, dict) and data.get('kind') in request_queue.SEAT_KINDS:
                            dimension, reason = self.request_wait or ('unknown', 'service decision unknown')
                            request_queue.hold(path, meta, dimension, reason)
                        if 'control_confirmation' not in meta:
                            self.clear_request_claim(claim)
                        continue  # not yet: the request waits for the seat's turn to end
                    # The final name is the client's completion signal, never an open writer.
                    retired = meta.get('retired')
                    if retired:
                        answer += f'; retired pending relaunch request(s): {", ".join(retired)}'
                    Service.complete_request_notice(self, path, data, meta, answer, dispatched=True)
                    request_queue.answer(path, answer)
                    self.log("request", **data, answer=answer)
                    request_queue.retire(path)
                    self.clear_request_claim(claim)
                except request_queue.RequestHold as hold:
                    try:
                        request_queue.hold(path, meta, hold.dimension, str(hold))
                    except Exception as error:
                        self.log('error', step='request-hold', request=path.name, error=repr(error))
                    continue  # an unproved order never archives one candidate and admits its sibling
                except Exception as error:
                    if isinstance(data, dict) and data.get("kind") == "rebrief":
                        retry.append(error)
                    else:
                        try:
                            self.failed_request(path, error)
                        except Exception as failure:
                            try:
                                self.log("error", step="requests", request=path.name, failure_handling=True,
                                         error=repr(failure), trace=traceback.format_exc())
                            except Exception:
                                pass  # the surviving claim stays visible in status even when the log cannot be written
                    continue
        if retry:
            raise retry[0]
        return True

    def complete_request_notice(self, path, data, meta, proof, *, dispatched=False):
        """Only the service promotes a told relaunch's terminal intent to trusted evidence."""
        if data.get('kind') != 'relaunch' or not meta.get('told'):
            return
        from . import request_queue
        if dispatched:
            # This argument is supplied only at the service's own dispatch return,
            # never from a request field or a recovered terminal intent.
            state = 'retired' if proof.startswith(('refused:', 'failed:', 'settled without applying:')) else 'applied'
        else:
            state = 'withdrawn' if meta.get('notice_terminal_state') == 'withdrawn' else 'retired'
        meta['terminal'], meta['notice_terminal_state'] = proof, state
        try:
            request_queue.save_metadata(path, meta)  # a receipt retry must never dispatch the act again
            request_queue.record_notice_completion(path, data, meta, state)
        except (OSError, ValueError, TypeError, ShipError):
            raise request_queue.RequestHold('notice-receipt', 'trusted request receipt unavailable; terminal intent retained') from None

    def prune_request_notices(self):
        """After a later canonical launch, terminal receipts no longer carry any evidence."""
        from . import post_notices, request_queue
        store = getattr(self, 'store', None)
        if store is None:
            return
        cache = getattr(store, '_notice_launch_cache', None)
        if cache is None:
            cache = store._notice_launch_cache = post_notices._LaunchCache()
        for name in getattr(getattr(self, 'ship', None), 'seats', {}):
            try:
                if request_queue.notice_stamp(self.runtime, name) == (None,):
                    continue
                records = post_notices.request_records(self.runtime, name, cache=cache)
                launched = post_notices.launched_at(self.runtime, name, cache=cache)
                request_queue.prune_notice_records(self.runtime, name, launched, records=records)
            except (OSError, ValueError, TypeError, ShipError) as error:
                self.log('error', step='request-notice-prune', seat=name, error=type(error).__name__)

    def clear_request_claim(self, claim: Path) -> None:
        claim.unlink(missing_ok=True)

    def failed_request(self, path: Path, error: Exception) -> None:
        """Keep the raw request and trace; an archival/answer failure leaves a visible claim, never a replay."""
        claim = path.with_suffix(".claimed")
        if not claim.exists():
            durable.replace_file(claim, path.read_text())  # malformed input also leaves the active queue on a terminal error
        from . import request_queue
        try:
            data, meta = request_queue.load(path), request_queue.metadata(path)
        except (OSError, ValueError, TypeError, ShipError):
            pass  # unreadable request evidence grants no notice settlement
        else:
            Service.complete_request_notice(self, path, data, meta, 'failed: ' + type(error).__name__)
        kept = path.parent / "failed" / path.name
        self.log("error", step="requests", request=path.name, error=repr(error), trace=traceback.format_exc())
        try:
            kept.parent.mkdir(exist_ok=True)
            if kept.exists():
                raise FileExistsError(f"failed request evidence already exists: {kept.name}")
            os.replace(path, kept)
        except OSError as archive_error:
            kept = claim
            self.log("error", step="requests", request=path.name, archive_unavailable=True,
                     error=repr(archive_error), trace=traceback.format_exc())
        try:
            request_queue.answer(path, f"failed: {error!r}; the request is kept in "
                                 f"requests/{kept.relative_to(path.parent)} and the service log has the trace")
            if kept != claim:
                self.clear_request_claim(claim)
                request_queue.clear_metadata(path)
        except OSError as answer_error:
            self.log("error", step="requests", request=path.name, answer_unavailable=True,
                     error=repr(answer_error), trace=traceback.format_exc())

    def answer(self, data: dict) -> str | None:
        kind = data["kind"]
        if kind == "profile-status":  # one full status collection of its own, profiled (status_profile)
            from . import status_profile
            return status_profile.ask(self, data.get("caller"))
        if kind == "cascade":
            from . import cascade
            return cascade.start(self, data["question"], data["caller"], data.get("efforts"))
        if kind == 'rebrief':
            copy = install.installed(self.runtime)
            if copy is None or copy['commit'] != data['commit']:
                return json.dumps(dict(token=data['token'], status='superseded'))
            if self.framework_commit != data['commit']:
                return None  # only the requested installed/running revision may render and announce it
            return self.upgrade_rebrief(data)
        if kind == "stop":
            seat = self.seat(data['seat'])
            from . import compaction
            if compaction.path(self.runtime, seat.name).exists():
                with seat._lifecycle():
                    return Door(seat).interrupt()
            return Door(seat).interrupt()
        if kind == 'fresh-ready':
            from . import compaction
            return compaction.arm(self.seat(data['seat']), data['revision'], data['report'], data['binding'])
        if kind == "resend":
            message = self.store.get(data["message"])
            seat = self.seat(message.to)
            from . import compaction, fresh_handoff
            if fresh_handoff.blocks_delivery(self.runtime, message):
                return f'{seat.name}: handoff waits for the next conversation'
            reserved, transitioning = compaction.withheld(self.runtime, seat.name)
            if transitioning or message.id in reserved:
                return f"{message.short}: held for the compaction successor"
            from . import refusal
            if message.id in refusal.excluded(self.runtime, seat.name):
                return (f"{message.short}: {seat.name}'s provider refused its conversation at this message; "
                        f"it is not typed again (law: one-door); send a reworded message instead")
            if seat.session.human:  # the Office is reached by its watch alone: the message is offered to it again
                self.attempts.close(self.attempts.open(message, seat.name, None, None, None), "resend", "re-offered to the watch")
                return f"{message.short} re-offered to {seat.name}'s watch; " + ("it prints it now" if post.watch_armed(self.runtime, seat.name) else "no watch is armed yet")
            if not seat.is_up:
                return f"{message.short}: {seat.name} is down; it is delivered at qd up"
            from .delivery_boundary import message_held
            if message_held(self.runtime, seat.name, message.id):
                return f"{message.short}: delivery boundary settled; outcome uncertain; never resent"
            return self.deliver(seat, [message], resend=True)
        if kind == "submit":
            return pending_delivery.submit(self, self.seat(data["seat"]))
        if kind in ("press", "type", "set-aside"):  # Systems' acts on what the door cannot settle (law: one-door)
            from . import acts
            seat = self.seat(data["seat"])
            if kind == "press":
                return acts.press(self, seat, str(data["choice"]), data.get("caller"))
            if kind == "set-aside":
                return acts.set_aside(self, seat, str(data["journal"]), data.get("caller"))
            return acts.type_line(self, seat, str(data["line"]), data.get("caller"))
        if kind == "control":
            # The CLI writes the declaration, then queues the control: when that write came after this pass loaded
            # its ship, the service's own ship is read again first, so the control starts from the current
            # declaration and through the service's own seat, its kept continuation included (law: ship-file).
            self.refresh_ship()
            return self.control(self.seat(data["seat"]), data["name"], str(data["value"]))
        if kind == "relaunch":
            return self.relaunch_request(data)
        if kind == "select":  # the seats failover acts on: a seat whose selection is unknown is held, not guessed
            seat = next((s for s in self.resolved_seats() if s.name == data["seat"]), None)
            if seat is None:
                return f"{data['seat']} not moved: its pool selection is unknown or held; inspect with qd status"
            return self.select(seat, str(data["pool"]))
        return f"unknown request {kind!r}"

    def tell_held(self, data, meta, answer) -> None:
        """Tell a seat once, through the post, that a relaunch of it stops at its tool state, and what its own
        word does. Where neither its record nor its screen settles what it has running, the seat itself is the
        one that knows: it lets that end or stops it, or it releases it. A report from the ship, never an
        obligation; one for each request, whether the request still waits or was retired at its turn's end. It
        ends by naming that request, so that a reader can tell when the request, and the report with it, is over.
        A waiting request is told only once the same hold has stood for TELL_HELD_AFTER seconds, by the request's
        own waiting record: a program draws passing hints on its bottom line (a helper that just ended), and a
        request that applies by itself a moment later needs no report and no turn of the seat's. A seat's own
        request that was retired at its turn's end has no later moment, and is told at once."""
        wait = getattr(self, 'request_wait', None)
        if (not isinstance(data, dict) or data.get('kind') != 'relaunch' or meta.get('told')
                or not isinstance(wait, tuple) or wait[0] not in ('tools-unknown', 'tools-pending')
                or not isinstance(data.get('seat'), str)):
            return
        # The seat's own only by the proof relaunch_request read for this request, never by the caller it names.
        seat, own = data['seat'], getattr(self, 'request_own', False) is True
        if answer is not None and not (own and answer.startswith('refused:')):
            return  # applied, or refused to another asker, who has the answer
        if answer is None:
            standing = meta.get('waiting')
            try:
                since = (datetime.fromisoformat(standing['since'])
                         if isinstance(standing, dict) and (standing.get('dimension'), standing.get('reason')) == wait
                         else None)
            except (KeyError, TypeError, ValueError):
                since = None
            if since is None or since.tzinfo is None or (post.now() - since).total_seconds() < TELL_HELD_AFTER:
                return  # not yet the same hold for long enough: the next pass looks again
        verb, name = ('restart', 'restart') if data.get('resume') else ('fresh', 'fresh start')
        request = getattr(self, 'request_path', None)
        word = (f'give your own word as the last act of a turn: qd {verb} {seat} --release'
                + (f' · request {request.stem}' if request is not None else ''))
        if answer is not None:
            said = (f'your own {name} was not applied: your turn ended with it held · {wait[1]} · ask again once '
                    f'that has ended; or, losing what you have running, {word}')
        else:
            head = (f'your own {name} waits' if own else
                    f"a {name} of this seat, asked by {meta.get('caller') or 'the operator'}, waits")
            said = (f'{head} · {wait[1]} · it applies once that has ended; to apply it without waiting, losing '
                    f'what you have running, {word}')
        try:
            store = getattr(self, 'store', None) or Store(self.runtime)
            meta['told'] = store.new('report', post.SHIP, seat, said).id
        except (OSError, ShipError) as error:
            self.log('error', step='tell-held', seat=seat, error=repr(error))

    def relaunch_request(self, data):
        """The person's later input retires an own restart; framework work waits for the next quiet break.

        Without classified person-input evidence the existing exact-asking-turn rule still decides.
        """
        from . import request_queue
        meta = getattr(self, 'request_metadata', {})
        # Who asked, and the proof that it was the seat itself, are read from the metadata enqueue wrote, never from
        # the body: a body's caller or proof can never stand where the metadata carries none.
        data = {**{key: value for key, value in data.items() if key not in ('caller', 'own')}, **meta}
        seat = self.seat(data['seat'])
        origin = request_queue.launch_binding(seat)
        # Whether the request is the seat's own is decided here, once, by the proof enqueue recorded, for the launch
        # the seat still runs; never by the caller it names. Every branch below, and the report of a held request
        # (tell_held), reads this: a request without the proof is another caller's.
        own = self.request_own = request_queue.proved_own(data['seat'], data, origin)  # the name the seat was found by
        if data.get('origin') is not None and data['origin'] != origin:
            return f'refused: {seat.name} launch changed; request retired without applying'
        released = self.released_at(seat, data, origin)
        if not seat.session.human:
            return self.relaunch(seat, bool(data['resume']), released=released,
                                 caller=data.get('caller'))
        pane = seat.pane()
        if pane is None or pane.dead:
            return self.relaunch(seat, bool(data['resume']))
        proved = idle.reading(seat, explicit=True, relaunch=True)
        state, hold = proved
        if data['resume'] and (state is None or state.conversation is None):
            seat.require_restart_target()
        try:
            asked = datetime.fromisoformat(data['created_at'])
        except (KeyError, TypeError, ValueError):
            return f'refused: {seat.name} asking turn unknown; no request will apply later'
        if state is None and hold is None:
            from .servicectl import ANSWER_SECONDS
            if asked.tzinfo is None or not 0 <= (post.now() - asked).total_seconds() <= ANSWER_SECONDS:
                return f'refused: {seat.name} no-record request expired; no deferred restart'
            return self.relaunch(seat, bool(data['resume']), reading=proved)
        if own and not data['resume'] and asked.tzinfo is not None:
            handed = self.handoff_fresh(seat, asked, proved, released=released)
            if handed is not OWN_TURN_RULE:
                return handed
        if own and asked.tzinfo is not None and asked <= post.now():
            person = self.relaunch_person_input(seat)
            if person is not None and person['at'] is not None:
                if datetime.fromisoformat(person['at']) > asked:
                    return f"refused: {seat.name} superseded by the person's later input; no deferred restart"
                self.request_wait = self.handoff_quiet(seat, state, hold)
                if self.request_wait is not None:
                    return None
                return self.relaunch(seat, bool(data['resume']), reading=proved, released=released)
        if (state is None or state.last_input_at is None or asked.tzinfo is None
                or state.last_input_at.tzinfo is None or state.last_input_at > asked or asked > post.now()):
            return f'refused: {seat.name} asking turn unknown or superseded by later input; no deferred restart'
        if not own:
            from .servicectl import ANSWER_SECONDS
            ending = state.completed_turns[-1] if state.completed_turns else None
            question = seat.harness.waiting_for_human(state)
            if ((post.now() - asked).total_seconds() > ANSWER_SECONDS
                    or not question and (ending is None or ending.ended is None or ending.ended > asked)):
                return f'refused: {seat.name} was not proved at an immediate break when requested; no deferred restart'
            answer = self.relaunch(seat, bool(data['resume']), reading=proved)
            return answer if answer is not None else f'refused: {seat.name} waits for {(self.request_wait or (None, "idle proof unknown"))[1]}; human request not deferred'
        scope = dict(origin=origin, input_at=state.last_input_at.isoformat(),
                     input_id=state.input_id, work_id=state.work_id,
                     conversation=state.conversation)
        bound = data.get('asking_turn')
        if bound is not None and (not isinstance(bound, dict) or any(bound.get(key) != value for key, value in scope.items())):
            return f'refused: {seat.name} asking turn changed; no deferred restart'
        if state.turn_open is True:
            # Accounting row counts can advance inside one native turn. Only
            # an explicit completed turn can establish that this break passed.
            if any(turn.ended is not None and turn.ended >= asked for turn in state.completed_turns):
                return f'refused: {seat.name} asking turn already ended; no deferred restart'
            meta['asking_turn'] = scope
            self.request_wait = ('turn-open', 'own asking turn has not ended')
            return None
        if state.turn_open is not False:
            return f'refused: {seat.name} asking turn state unknown; no deferred restart'
        ending = state.completed_turns[-1] if state.completed_turns else None
        if (ending is None or not ending.timed or ending.started is None or ending.ended is None
                or not ending.started <= asked <= ending.ended):
            return f'refused: {seat.name} exact asking-turn end not proved; no deferred restart'
        answer = self.relaunch(seat, bool(data['resume']), reading=proved, released=released)
        return answer if answer is not None else f'refused: {seat.name} asking turn ended with {(self.request_wait or (None, "idle proof unknown"))[1]}; request retired, never applied at a later turn'

    def relaunch_person_input(self, seat):
        """Classify the person's input, excluding whole known transports, never a matching prefix."""
        from . import person_quiet
        if not callable(getattr(seat.harness, 'person_inputs', None)):
            return None
        store = getattr(self, 'store', None) or Store(self.runtime)
        attempts = getattr(self, 'attempts', None) or Attempts(self.runtime)
        attempted = attempts.latest(seat.name)
        excluded = (*person_quiet.waking_lines(),
                    *(form.strip() for message in store.for_seat(seat.name) if message.id in attempted
                      for form in post.delivered_forms(message)))
        return person_quiet.read(seat, post.now(), excluded=excluded)

    @staticmethod
    def released_at(seat: Seat, data: dict, origin) -> datetime | None:
        """When the seat itself released what it has running, or None. Only the seat's own request can: one that
        names the seat as its caller with the proof enqueue made, for the launch it still runs. The flag of any
        other request, and one whose asking time is unreadable, releases nothing."""
        from . import request_queue
        if data.get('release') is not True or not request_queue.proved_own(seat.name, data, origin):
            return None
        try:
            asked = datetime.fromisoformat(data['created_at'])
        except (KeyError, TypeError, ValueError):
            return None
        return asked if asked.tzinfo is not None else None

    def handoff_fresh(self, seat: Seat, asked: datetime, reading, *, released=None):
        """A human seat's own fresh start, asked once it has an open handoff to itself, applies at its next quiet
        moment. What reaches it later, a delivery or the program's own notice, is carried into the fresh start as
        an open message and never retires the request; the person's own input does. Where no such handoff was
        posted before the request, or the person's input cannot be told from the program's notices, the asking
        turn's own rule decides (OWN_TURN_RULE)."""
        meta = getattr(self, 'request_metadata', {})
        store = Store(self.runtime)
        bound = meta.get('handoff')
        if bound is None:
            posted = [m for m in store.open_for(seat.name)
                      if m.kind == 'order' and m.sender == seat.name and m.at <= asked]
            if not posted:
                return OWN_TURN_RULE
            handoff = max(posted, key=lambda m: m.at)
        else:
            handoff = store.get(bound)
            if not store.is_open(handoff):
                return f'refused: {seat.name} handoff {handoff.short} closed before its fresh start applied; no deferred restart'
        person = self.relaunch_person_input(seat)
        if person is None or person['at'] is None:
            if bound is None:
                return OWN_TURN_RULE
            reason = person['reason'] if person is not None else 'reader unavailable'
            return f"refused: {seat.name} person's input unknown ({reason}); no deferred restart"
        if datetime.fromisoformat(person['at']) > asked:
            return f"refused: {seat.name} superseded by the person's later input; no deferred restart"
        meta['handoff'] = handoff.id
        state, hold = reading
        self.request_wait = self.handoff_quiet(seat, state, hold)
        if self.request_wait is not None:
            return None  # a turn, a queued input, an unconfirmed delivery or the box: the next quiet moment
        return self.relaunch(seat, False, reading=reading, released=released)  # its own hold, if any, also waits

    def handoff_quiet(self, seat: Seat, state, hold) -> tuple[str, str] | None:
        """Why this is not yet a quiet moment for a fresh start that carries every delivery, or None."""
        if state is None:
            return (hold.dimension, hold.reason) if hold is not None else ('record', 'turn state unknown')
        if state.turn_open is not False:
            return ('turn-open', 'own fresh start after its handoff waits for the turn in progress')
        ending = state.completed_turns[-1] if state.completed_turns else None
        # A turn the program gave its own notice may hold no input: the proof is no input after the last proved end.
        if (state.last_input_at is None or ending is None or not ending.timed or ending.ended is None
                or state.last_input_at > ending.ended):
            return ('turn-end', 'no proved turn end after the latest input')
        if state.queue_error or state.queued:
            return ('queued', 'an input is queued in the seat')
        if any(result in (None, 'pending', 'blind', 'queued')
               for result in Attempts(self.runtime).latest(seat.name).values()):
            return ('delivery', 'a delivery to the seat is not confirmed')
        if not idle.at_rest(Door(seat).screen()):
            return ('box', 'the box is not proved empty and idle')
        return None

    def relaunch(self, seat: Seat, resume: bool, *, reading=None, released=None, caller=None) -> str | None:
        from . import fresh_handoff
        from .harness import resume_target
        try:
            return Service._relaunch(self, seat, resume, reading=reading, released=released)
        except resume_target.Unproved as error:
            if self is None or seat.session.human:
                raise
            return fresh_handoff.request(seat, reason=error.reason, caller=caller or seat.ship.operator.lower(),
                                         expected_launch=error.launch, service=self, restart=True).proof

    def _relaunch(self, seat: Seat, resume: bool, *, reading=None, released=None) -> str | None:
        """Explicit recovery alone may interrupt a proven question-only wait. ``released`` is when the seat
        itself released what it has running, by its own proved request: it lifts a hold of the tool state alone,
        at the end of the turn it was given in."""
        if reason := recovery.held(seat):
            if self is not None:
                self.request_wait = ('recovery', reason)
            return None
        pane = seat.pane()
        witness = idle.RECORD
        if pane is not None and not pane.dead:
            proved = reading if reading is not None else idle.reading(seat, explicit=True, relaunch=True)
            state, hold = proved
            if state is None or not seat.harness.waiting_for_human(state):
                witness = getattr(proved, 'witness', idle.RECORD)
                if hold is not None and released is not None:
                    hold = idle.release(seat, state, hold, released)
                    witness = idle.RECORD_AND_WORD if hold is None else witness
                if hold is not None:
                    if resume and self is not None and not seat.session.human and (state is None or state.conversation is None):
                        seat.require_restart_target()
                    if self is not None:
                        self.request_wait = (hold.dimension, hold.reason)
                    return None  # deferred-request answers/lifetime are separate; the reason is shared
        from . import relaunch_intent
        with relaunch_intent.section(self, seat, 'relaunch', resume=resume):
            proof = seat.restart() if resume else seat.fresh()
        # The proof line says which witness settled the tool state where the record alone could not.
        if witness == idle.RECORD_AND_SCREEN:
            return f'{proof}\nat rest by its record and its screen'
        if witness == idle.RECORD_AND_WORD:
            return f'{proof}\nat rest by its record and its own word: what it had running was released'
        return proof

    def control(self, seat: Seat, name: str, value: str) -> str | None:
        """Apply a control by its in-session form, through the door, when the seat is idle."""
        from . import request_queue
        meta = getattr(self, 'request_metadata', {})
        if 'control_confirmation' in meta:
            return self.continue_model_control(seat, name, value)
        if reason := recovery.held(seat):
            return f'{name} held: {reason}'
        if seat.spec.pool_choices and (name in {"harness", "model", "effort", "model_mode", "effort_mode"} or name not in shipmod.SEAT_KEYS):
            return f"{name} not applied: inline pool choices require an explicit edit in ship.toml; queued program controls have no choice scope"
        if name in {'model_mode', 'effort_mode'}:
            key = name.removesuffix('_mode')
            validate_mode(getattr(seat.spec, key), value, key, f'seat {seat.name}')
            return f"{name} written; qd restart {seat.name} applies it"
        if name == 'model':
            validate_mode(value, 'named', 'model', f'seat {seat.name}')
        note = None
        if name == 'effort':
            launched = seat.launched()
            version = launched.version if launched and record_owner.launch_reader(launched) == seat.harness.name else None
            note = validate_effort(seat.harness, value, version=version,
                                   installed=True, context=f'seat {seat.name}')
        unchecked = f" · {note}" if note else ''
        control = seat.harness.control(name)
        if control is None or control.session is None:
            return f"{name} written; qd restart {seat.name} applies it{unchecked}"
        if not seat.is_up:
            return f"{name} written; {seat.name} is down, so qd up {seat.name} applies it{unchecked}"
        if self is not None:
            # A different consumed control owns the seat until its record settles or its deadline
            # expires. It must not be interleaved with a second transport.
            for path in (seat.ship.runtime / 'requests').glob('*.json'):
                if path == getattr(self, 'request_path', None):
                    continue
                try:
                    other = request_queue.load(path)
                    if other.get('seat') == seat.name and request_queue.claimed(path) is not False:
                        self.request_wait = ('control-confirmation', 'another consumed request owns the seat')
                        return None
                except (OSError, ValueError, ShipError):
                    self.request_wait = ('control-confirmation', 'request ownership unknown')
                    return None
        if seat.is_working():
            if self is not None:
                self.request_wait = ('turn-open', 'a turn is open')
            return None  # retain the existing control admission
        state, hold = idle.reading(seat)
        if (state is None and hold is not None) or (state is not None and state.pending_tools != ()):
            if self is not None:
                self.request_wait = (('record', hold.reason) if state is None else
                                     ('tools-unknown' if state.pending_tools is None else 'tools-pending',
                                      idle.tool_reason(state)))
            return None  # controls do not inherit the move's timed/queue predicate
        door = Door(seat)
        command = control.command(value)
        if name == 'model':
            if self is None:
                return 'model held: durable request owner unavailable; command not typed'
            return self.begin_model_control(seat, control, value, door)
        delivery = door.deliver(command, confirm=False)
        if delivery.result != "typed":
            return f"{name}: {delivery}{unchecked}"
        time.sleep(0.5)
        door.clear_known()  # a program that asks "switch?" after the command is answered yes
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            record = seat.record()
            try:
                state = seat.harness.state(record) if record else None
            except OSError:
                state = None
            confirmed = seat.harness.confirms(control, value, door.screen(), state)
            if confirmed:
                return f"{name} = {value} · applied by {command} in {seat.pane().id} · confirmed by {control.confirmed_by}{unchecked}"
            time.sleep(0.2)
        return f"{name} = {value} · typed {command} in {seat.pane().id} · not confirmed by the {control.confirmed_by} yet{unchecked}"

    def begin_model_control(self, seat, control, value, door):
        from . import control_confirmation as confirmation, request_queue
        path = getattr(self, 'request_path', None)
        if path is None:
            return 'model held: durable request owner unavailable; command not typed'
        record = seat.record()
        inputs = confirmation.input_scope(seat, record)
        bound = seat.harness.model_control_origin(seat, record, value)
        launch = request_queue.launch_binding(seat)
        if inputs is None or bound is None or launch is None:
            self.request_wait = ('control-confirmation', 'request-bound model record/display evidence unknown')
            return None
        origin = dict(seat=seat.name, launch=launch, inputs=inputs, record=bound,
                      declaration=[seat.harness.name, seat.spec.model_mode, seat.spec.model])
        state = confirmation.Continuation.begin(control.name, value, origin, post.now())
        meta = self.request_metadata

        def persist(state):
            meta['control_confirmation'] = state.metadata()
            request_queue.save_metadata(path, meta)

        persist(state)  # consumed reservation survives a crash BEFORE the sole command transport
        if (request_queue.launch_binding(seat) != launch
                or confirmation.input_scope(seat, record) != inputs):
            return 'model unconfirmed: source changed before command; not retyped'
        delivery = door.deliver(control.command(value), confirm=False)
        if delivery.result == 'typed':
            state = confirmation.submitted(state)
            persist(state)
        else:
            # A withheld or ambiguous transport is never permission to send this command again.
            self.request_wait = ('control-confirmation', f'control transport {delivery.result}; record confirmation pending')
            return None
        return self.continue_model_control(seat, control.name, value)

    def continue_model_control(self, seat, name, value):
        from . import control_confirmation as confirmation, request_queue
        path, meta = self.request_path, self.request_metadata
        state = request_queue.control_continuation(path, meta=meta)
        control = seat.harness.control(name)
        if state is None or control is None or state.control != 'model' or state.value != value:
            raise request_queue.RequestHold('control-confirmation', 'control continuation unsupported; retained')

        def verify():
            current = request_queue.control_continuation(path)
            return (current == state and state.origin.get('launch') == request_queue.launch_binding(seat)
                    and (state.origin['inputs']['record'] is not None or getattr(seat.harness, 'synthetic', False) is True)
                    and state.origin.get('declaration') == [seat.harness.name, seat.spec.model_mode, seat.spec.model])

        def persist(updated):
            nonlocal state
            meta['control_confirmation'] = updated.metadata()
            request_queue.save_metadata(path, meta)
            state = updated

        record = seat.record()
        if record is not None and state.origin['record']['path'] is None and verify():
            bound = seat.harness.model_control_join(seat, record, state.origin['record'])
            if bound is not None and bound != state.origin['record'] and verify():
                persist(replace(state, origin={**state.origin, 'record': bound}))

        def answer_current():
            if state.origin['inputs']['record'] is None:
                return None  # first-input synthetic continuation observes only; it owns no dialog key
            record = seat.record()
            if not verify() or confirmation.input_scope(seat, record) != state.origin.get('inputs'):
                return False
            return (seat.harness.model_control_current(record, state.origin.get('record', {}))
                    if record is not None else None)

        try:
            decision = confirmation.observe_control(
                seat, control, state, post.now(), persist=persist, verify=verify, answer_current=answer_current,
                confirmed_model=lambda record, current: seat.harness.model_control_result(
                    seat, record, current.value, current.origin.get('record', {})))
        except (OSError, ShipError, TmuxError) as error:
            # A failed observation/gesture keeps the durable reservation. Its next pass observes
            # the record or original deadline, never the command or an uncertain key a second time.
            decision = confirmation.advance(state, post.now(), confirmed=None)
            if decision.action == 'wait':
                self.request_wait = ('control-confirmation',
                                     f'control observation unavailable ({type(error).__name__}); not retyped')
                return None
            decision = replace(decision, reason=f'{decision.reason}; control observation unavailable ({type(error).__name__})')
        if decision.action == 'confirmed':
            pane = decision.state.origin['launch']['pane']
            return (f'model = {value} · applied by {control.command(value)} in {pane} · '
                    f'{confirmation.proof(decision.state)} · confirmed by {control.confirmed_by}')
        if decision.action == 'unconfirmed':
            return f'model = {value} · {confirmation.unconfirmed_proof(decision.state)}: {decision.reason}'
        self.request_wait = ('control-confirmation', decision.reason)
        return None


    def follow(self) -> None:
        """When a seat starts working, the session it sits in shows its window.

        A start is a turn the seat's record opens after the service saw it closed (law: record-first);
        the first reading of a seat is its baseline, never a start. Seats in a human's session are never
        followed, and a switch is refused while a human scrolls or types in the session, or when it would
        resize the window shown.
        """
        for seat in self.resolved_seats():
            with Service.seat_guard(self, 'follow', seat):
                if seat.session.human:
                    continue  # law: one-door; the human's session is theirs
                working = self._working(seat)
                if working is None:
                    continue  # an unreadable or uncertain record is no transition
                before = self.working.get(seat.name)
                self.working[seat.name] = working
                if working and before is False:
                    refused = self.show_window(seat)
                    self.log("followed" if refused is None else "not followed", seat=seat.name,
                             **({"reason": refused} if refused else {}))

    def _working(self, seat: Seat) -> bool | None:
        if seat.launched() is None:
            self.work_marks.pop(seat.name, None)
            return False
        record = seat.record()
        if record is None:
            return False  # a launch that has not written yet has not started working
        mark = (record, record_fingerprint(record))
        if self.work_marks.get(seat.name) == mark:
            return self.working.get(seat.name)
        try:
            state = seat.harness.state(record)
        except OSError:
            return None
        self.work_marks[seat.name] = mark
        return None if state.turn_open is None else bool(state.turn_open)

    def pane_died(self, seat: Seat, pane) -> None:
        """Log death from the pane already read; log again when a pending exit status becomes known."""
        logged = self.__dict__.setdefault("dead_panes", {})
        identity = (pane.id, pane.pid)
        previous = logged.get(seat.name)
        if previous is not None and previous[:2] == identity and (previous[2] or pane.exit_status is None):
            return
        died = (datetime.fromtimestamp(pane.dead_time).astimezone().isoformat(timespec="seconds")
                if pane.dead_time is not None else None)
        self.log("pane-dead", seat=seat.name, pane=pane.id, pid=pane.pid, died=died, status=pane.exit_status)
        logged[seat.name] = (*identity, pane.exit_status is not None)

    def looks(self) -> None:
        """The door's one look at what it could not confirm (law: one-door): record first, then the screen.

        Its candidates come from the attempts reconcile already loaded this pass: a pass with nothing
        unconfirmed resolves no seat and opens no journal."""
        records = self.loaded_attempts if self.loaded_attempts is not None else self.attempts.all()
        waiting = pending_delivery.candidates(records)
        if not waiting:
            return
        for seat in self.resolved_seats():
            if seat.name not in waiting:
                continue
            try:
                for line in pending_delivery.one_look(self, seat, records):
                    self.log("look", seat=seat.name, proof=line)
            except (ShipError, OSError, TmuxError) as error:
                self.log("error", step="looks", seat=seat.name, error=str(error))

    def show_window(self, seat: Seat) -> str | None:
        """Show the seat's window in its session, or say why not; nothing is typed, closed or resized."""
        session = seat.session
        if session.human:
            return "a human's session"
        pane = seat.pane()  # law: identity
        if pane is None or pane.dead:
            return "no live pane of its launch"
        if pane.session != session.title:
            return f"its pane is in session {pane.session!r}, not {session.title!r}"
        if self.tmux.session_environment(session.title, "QD_SHIP") != str(self.ship.folder):
            return f"session {session.title!r} is not this ship's"
        view = self.tmux.session_view(session.title)
        if view is None or pane.id not in view.pane_windows:
            return "session unreadable"
        target = view.pane_windows[pane.id]
        if target == view.active:
            return None
        if view.scrolling:
            return "a human is scrolling in the session"
        if any(time.time() - last < TYPING_QUIET for last in view.inputs):
            return "a human is typing in the session"
        if view.inputs and view.windows[target][1:] != view.windows[view.active][1:]:
            return "showing it would resize its window"
        try:
            self.tmux.select_window(pane.id, expected_pid=pane.pid)
        except TmuxError as error:
            return str(error)
        return None

    def compactions(self) -> None:
        from . import compaction, compaction_alert
        selected = {spec.name for spec in self.ship.seats.values()
                    if spec.fresh_after_compactions or compaction.path(self.runtime, spec.name).exists()
                    or compaction_alert.path(self.runtime, spec.name).exists()}
        if not selected:
            return
        for seat in self.resolved_seats():
            if seat.name not in selected:
                continue
            try:
                compaction.tick(seat)
                compaction_alert.step(self, seat)
            except (OSError, ShipError) as error:
                self.log('error', step='compactions', seat=seat.name, error=repr(error))

    def output_limits(self) -> None:
        """A seat whose last turn stopped at its output limit with its order open: its alarm, once, to the systems seats."""
        from . import output_limit
        for seat in self.resolved_seats():
            try:
                output_limit.step(self, seat)
            except (OSError, ShipError) as error:
                self.log('error', step='output-limits', seat=seat.name, error=repr(error))

    def person_quiet(self) -> None:
        """For each seat in a human session: keep the person's quiet time, and post its one wake when due."""
        from . import person_quiet
        for seat in self.resolved_seats():
            if not seat.session.human:
                continue
            try:
                person_quiet.tick(seat, self.store)
            except (OSError, ShipError) as error:
                self.log('error', step='person_quiet', seat=seat.name, error=repr(error))

    def request_fresh_handoff(self, seat: Seat, *, reason: str, caller: str | None = None,
                              expected_launch=None):
        """Stage a crew handoff for this launch, returning its disposition and proof."""
        from . import fresh_handoff
        return fresh_handoff.request(seat, reason=reason, caller=caller, expected_launch=expected_launch, service=self)

    def fresh_handoffs(self) -> None:
        from . import fresh_handoff
        if not fresh_handoff.folder(self.runtime).is_dir():
            return
        for seat in self.resolved_seats():
            with Service.seat_guard(self, 'fresh_handoffs', seat):
                fresh_handoff.tick(self, seat)

    def fresh_after_order(self) -> None:
        """Read each closing-turn decision once; start fresh outside that read-only view."""
        for seat in self.resolved_seats():
            with Service.seat_guard(self, 'fresh_after_order', seat):
                with post.reading_pass(store=self.store, attempts=getattr(self, 'attempts', None)):
                    ready = Service._fresh_after_order_ready(self, seat)
                if ready:
                    from . import relaunch_intent
                    with relaunch_intent.section(self, seat, 'after-order', resume=False):
                        proof = seat.fresh()  # law: context-is-cache
                    self.log("fresh", seat=seat.name, proof=proof)

    def _fresh_after_order_ready(self, seat) -> bool:
        """The current orders, recovery and recorded completion; never a lifecycle action."""
        from . import compaction, fresh_handoff
        from . import hand_door
        if (seat.session.human or seat.spec.fresh != "after-order" or not seat.is_up
                or fresh_handoff.active(seat.ship.runtime, seat.name)
                or hand_door.blocks_fresh(seat, self.store, getattr(self, 'attempts', None)) or recovery.held(seat)):
            return False
        launched = seat.launched()
        if launched is None:
            return False
        try:
            protocol = compaction.protocol_requests(seat)
        except ShipError:
            return False  # unknown protocol identity cannot authorize another automatic path
        closed = max((report.at for order in self.store.for_seat(seat.name)
                      if order.kind == "order" and order.id not in protocol
                      and not (order.sender == order.to and order.re in protocol)
                      and (report := self.store.closing_report(order))
                      and report.at > launched.when), default=None)
        if seat.spec.role == "hand":
            # A hand's order in hand may be a question: closed by the hand's own answer, it ends that order as a
            # report ends an order, and the fresh start carries the next (hand_door).
            from . import hand_door
            answered = hand_door.closed_by_hand(self.store, seat.name, launched.when, protocol)
            closed = max((at for at in (closed, answered) if at is not None), default=None)
        if closed is None or (record := seat.record()) is None:
            return False
        try:
            state = seat.harness.state(record)
        except OSError:
            return False  # no screen or default idle reading can prove a recorded closing turn
        if (state.turn_open is False and state.queue_error is None and not state.queued
                and any(turn.timed and turn.started is not None and turn.ended is not None
                        and turn.started <= closed <= turn.ended for turn in state.completed_turns)):
            return True
        return False

    def _brief_stamp(self) -> tuple:
        """Fingerprint rendered inputs through the renderer, including session and selection.

        No publication or selection alarm belongs to this reading. An unresolved seat
        keeps a marker until ordinary rebrief can resolve and render it.
        """
        selected = seats(self.ship, self.tmux, False)
        texts = {seat.name: standing.brief(self.ship, seat.spec).text
                 for seat in selected if not isinstance(seat, UnresolvedSeat)}
        self.brief_texts = texts  # reuse this pass's exact text when publication owns it
        return tuple((seat.name, hashlib.sha256(texts[seat.name].encode()).digest()
                      if seat.name in texts else None) for seat in selected)

    def credentials(self) -> None:
        """A seat whose credential link became a file has refreshed the token: reseed the pool and relink.

        A seat whose program outlived its closed pane at down is not reseeded while that program lives;
        once it has ended, its retirement is finished here: reseeded, and its launch removed. A seat whose sandbox
        was held at its stop is taken here too, retiring or not: each pass ends its held processes again, and the
        hold lifts once none of them runs (never by age; an unreadable hold still holds).
        """
        from . import jail_end
        ready = []
        for seat in self.resolved_seats():
            if seat.retiring() is None and jail_end.held(seat.trusted) is None:
                ready.append(seat)
                continue
            try:
                if proof := seat.finish_retirement():
                    self.log("retired", seat=seat.name, proof=proof)
            except lifecycle.Busy:
                continue  # the owner of the transition still owns its retirement
            except ShipError as error:
                self.log("error", step="retire", seat=seat.name, error=str(error))
        for line in credential.reseed(self.ship, ready):
            self.log("credential", proof=line)
        try:
            for line in credential.settle_held(self.ship):  # a retired seat's home kept for its renewal
                self.log("credential", proof=line, by="held")
        except Exception as error:
            self.log("error", step="credential-held", cause=watch_cause(error))
        try:
            names = credential.linked_names(self.ship, ready)
        except Exception as error:
            names = []
            self.log("error", step="credential-watch", error=repr(error))
        self.credential_watch = (ready, names)

    def watch_credentials(self, stop: threading.Event) -> None:
        """Between passes: a linked credential name that became a file is a renewal; reseed the pool at once.

        A renewal retires the token the other seats still hold, so they fail until the pool's file has it; the pass
        alone left that gap for minutes. Only names are looked at, twice a second; a detached file already handled
        is not handled again until it changes. The pass still reseeds as before.
        """
        handled: frozenset = frozenset()
        failing: set[str] = set()  # the causes logged since the watch last ran clean: each once, never per tick
        while not stop.wait(credential.WATCH_SECONDS):
            try:
                seats, names = self.credential_watch
                marks = credential.detached_marks(names)
                skipped = False
                if marks and not marks <= handled:
                    lines = credential.reseed(self.ship, seats)
                    for line in lines:
                        self.log("credential", proof=line, by="watch")
                    skipped = any(line.startswith(credential.SKIPPED) for line in lines)
                    if not skipped:  # the lock was held: nothing is marked handled, the next tick tries again
                        marks = credential.detached_marks(names)  # what is left detached after the reseed: retained ones
                if not skipped:
                    handled = marks
            except Exception as error:  # the watch never stops the service; the pass still reseeds
                cause = watch_cause(error)
                if cause not in failing:
                    failing.add(cause)
                    self.log("error", step="credential-watch", cause=cause)
                continue
            if failing and not skipped:  # a round skipped on the lock did no work: it is not a clean round
                self.log("credential-watch-cleared", causes=sorted(failing))
                failing = set()

    def authentications(self) -> None:
        from . import auth_failure
        auth_failure.run(self)


    @record_owner.reader_pass()
    def account(self) -> None:
        """Read sources first, commit record accounting once, then make due usage attempts.

        Program/screen/provider calls never run under the record write lock.
        Checkpoints and machine/source caches describe only committed accounting.
        """
        if getattr(self, '_accounting_tick', False):
            from .accounting_slices import Driver
            if not hasattr(self, 'account_driver'):
                self.account_driver = Driver()
            # Schedule the independent quota duty when accounting actually runs,
            # including while its child is busy. A disabled duty schedules no work.
            self.usage_pending = True
            return self.account_driver.step(self)
        before = accounting_health.visible(self).shown()
        try:
            observations, notices, human_readings = self.account_records()
        except Exception as error:
            accounting_health.report_record_failure(self, error, post.now())
            self.publish_accounting_health(force=True)
            self.accounting_shown_changed(before)
            return  # no per-pass raw exception log; the bounded operational alarm owns this failure
        accounting_health.report_success(self, self.accounting_health)
        self.publish_accounting_health(force=True)
        self.accounting_shown_changed(before)
        # These duties happen after commit. Their failures retain tick's existing
        # error visibility and cannot be misreported as rolled-back accounting.
        for event, data in notices:
            self.log(event, **data)
        for seat, record, state, turns, stamp in human_readings:
            from . import turn_failure
            turn_failure.publish(seat, record, state, turns, stamp)
        with self._track_usage_pass():
            for seat, _, launched, live, _, _, _, _ in observations:
                quota.refresh(self, seat, live, launched)
            quota.refresh_declared(self)

    def accounting_shown_changed(self, before) -> None:
        """A status snapshot taken before the outcome would disagree with it: the next status check republishes."""
        if accounting_health.visible(self).shown() != before:
            self.status_checked = None

    @contextmanager
    def _track_usage_pass(self):
        """Coalesce confirmed rollbacks across the duty, including an earlier settlement in this tick."""
        if getattr(self, '_usage_pass', None) is not None:
            yield
            return
        started = self._usage_pass = accounting_health.UsagePass()
        try:
            yield
        finally:
            self._usage_pass = None
            if started.rollback is not None or started.committed:
                self._usage_commit_outcome(started.rollback)

    def _usage_commit_outcome(self, error):
        if (active := getattr(self, '_usage_pass', None)) is not None:
            active.note(error)
            return
        health = self.usage_accounting_health
        if error is None and not health.failures:
            return
        before = accounting_health.visible(self).shown()
        if error is None:
            accounting_health.report_success(self, health)
        else:
            accounting_health.report_failure(self, error, post.now(), health=health)
        self.publish_accounting_health(force=True)
        self.accounting_shown_changed(before)

    def refresh_usage(self):
        """Live provider evidence is independent of a historical accounting backlog."""
        if not getattr(self, 'usage_pending', False):
            return
        self.usage_pending = False
        with self._track_usage_pass():
            for seat in self.resolved_seats():
                with Service.seat_guard(self, 'refresh_usage', seat):
                    pane = seat.pane()
                    quota.refresh(self, seat, pane is not None and not pane.dead, seat.launched())
            try:
                quota.refresh_declared(self)
            except ledger.UsageCommitRolledBack:
                pass  # the ledger reported the rollback to this pass

    def account_records(self, *, source_records=None, source_screens=None, source_walls=None,
                        replay=True, process_table=None, source_reader_keys=None):
        """Prepare and commit the pass; callers handle only its accounting outcome."""
        # Settle a standalone usage write before reading records. A failed settlement counts as a failed pass.
        self.ledger.prepare_usage()
        table = machine.processes() if process_table is None else process_table
        observations, notices, human_readings = [], [], []
        marks, errors = dict(self.read_marks), dict(self.account_errors)
        holds, held = dict(getattr(self, 'read_holds', {})), {}
        spans = {}  # record -> (raw size, limit, resource, unit): read stopped at a bound
        projections = {}  # scalar budget evidence from guarded native streams
        meters = deepcopy(self.meters)
        checkpoints = {}
        reader_keys = reader_stamp.readers() if source_reader_keys is None else source_reader_keys
        for seat in self.resolved_seats():
            try:
                pane, launched = seat.pane(), seat.launched()
            except ShipError as error:  # its own launch record refused: that seat alone is held, never the pass
                Service.seat_hold(self, 'account_records', seat, error)
                continue
            Service.seat_hold(self, 'account_records', seat, None)
            live = pane is not None and not pane.dead
            if pane is not None and pane.dead:
                self.pane_died(seat, pane)
            current = seat.record()
            prepared = self.ledger.provenance.prepare(seat, current, launched)
            meter = meters.get(seat.name)
            if live:
                if meter is None or meter.root != pane.pid:
                    meter = meters[seat.name] = machine.Meter(pane.pid)
                meter.sample(table)
            reads = []
            for owned in (source_records[seat.name] if source_records is not None else record_owner.records(seat)):  # law: every-turn-accounted
                record = owned.path
                # law: every-turn-accounted -- a pool not declared for the seat is never charged; the turn counts unattributed
                pool = (launched.pool if (record == current and launched and launch_pool_refusal(seat, launched) is None)
                        else UNKNOWN)
                stamp, evidence = None, None
                if owned.reader is None and not owned.transient:
                    # law: every-turn-accounted -- a record whose reader is unknown is settled once
                    try:
                        stamp = (owned.name, record_fingerprint(record))
                    except OSError:
                        stamp = None  # unreadable metadata: the ordinary transient path below
                    if stamp is not None:
                        errors.pop(record, None)
                        if empty_record(stamp[1]):
                            reads.append((record, pool, stamp, None, (), None, None, None))  # no turns, whatever reads it
                            continue
                        settlement = settlement_of(stamp, owned.reason)
                        if self.ledger.settled(record) != (owned.reason, settlement):
                            notices.append(("error", dict(step="account", seat=seat.name, record=record.name,
                                                          error=owned.reason, settled=True)))
                            reads.append((record, pool, stamp, None, (), owned.reason, None, settlement))
                        continue
                try:
                    stamp = ledger.record_stamp(owned)
                    # The launches joined to this record. A down seat or a newer launch of another record does not move it.
                    launch = self.ledger.provenance.owning_launch(prepared[0], record)
                    evidence = (stamp, owned.reason, record_fingerprint(record_owner.registry(seat.trusted)), launch)
                    # New launch/price evidence for this record must be reconciled even if its bytes stand still.
                    # A mark made inside a time-bound unknown is read again once that unknown has passed: once, since
                    # the read that follows sets a new hold or none.
                    if marks.get(record) == stamp and prepared[2] is None:
                        if not held_reading.due(holds.get(record)):
                            continue
                        holds.pop(record)
                    checkpoint = self.ledger.accounting_evidence(
                        seat.name, stamp, owned.reason, launch, reader_keys.get(owned.name))
                    if checkpoint is not None and self.ledger.accounted(record, checkpoint):
                        marks[record] = stamp
                        continue
                    prior = errors.get(record)
                    if prior is not None and prior[0] == evidence and time.monotonic() < prior[2]:
                        continue
                    state, turns = owned.read()
                    # Reading concurrently appended bytes must not certify the later file.
                    try:
                        stable = stamp == ledger.record_stamp(owned)
                    except OSError:
                        stable = False
                    if (stable and getattr(owned.reader, 'native_status_stream', False)
                            and (usage := getattr(state, 'projection_usage', None)) is not None):
                        projections[record] = usage
                    # A stable record read whole is accounted: one with turns and a known turn state, or one that reads
                    # as no turns with no turn open, a complete reading of nothing. A record whose turn state is
                    # unknown keeps its ordinary read/error checks, empty or not.
                    complete_empty = not turns and state.turns == 0 and state.turn_open is False
                    if stable and state.turns is not None and state.turn_open is not None and (turns or complete_empty):
                        checkpoints[record] = checkpoint
                    if stable and state.turn_open is None and (hold := held_reading.unknown_until(owned)) is not None:
                        held[record] = hold
                except OSError as error:
                    message = str(error)
                    if (getattr(owned.reader, 'native_status_stream', False)
                            and (usage := getattr(error, 'projection_usage', None)) is not None):
                        projections[record] = usage
                        if usage.exhausted is not None and getattr(error, 'limit', None) is not None:
                            spans[record] = usage.stamp[2], error.limit, usage.exhausted, error.unit
                    if getattr(error, 'size', None) is not None and getattr(error, 'bound', None) is not None:
                        spans[record] = error.size, error.bound, None, None
                    prior = errors.get(record)
                    if prior is None or prior[:2] != (evidence, message):
                        notices.append(("error", dict(step="account", seat=seat.name, record=record.name, error=message)))
                    errors[record] = evidence, message, time.monotonic() + ACCOUNT_RETRY
                    reads.append((record, pool, stamp, None, (), message, owned.reader, None))
                    continue
                errors.pop(record, None)
                reads.append((record, pool, stamp, state, turns, None, owned.reader, None))
            screen = (source_screens.get(seat.name) if source_screens is not None else
                      Door(seat).screen() if live and seat.harness.screen_walls else None)
            observations.append((seat, pane, launched, live, current, prepared, reads, screen))

        with self.ledger.record_pass() as book:
            for seat, pane, launched, live, current, prepared, reads, screen in observations:
                for changed in book.provenance.sync(seat, current, launched, commit=False, prepared=prepared):
                    marks.pop(changed, None)
                meter = meters.get(seat.name)
                for record, pool, stamp, state, turns, error, reader, settlement in reads:
                    if record in projections:
                        book.save_projection_usage(record, projections[record])
                    if error is not None:
                        book.mark_accounted(record, None)
                        book.mark_record(record, seat.name, pool, error, commit=False, evidence=settlement)
                        if record in spans:  # law: every-turn-accounted: what was never read is shown, not guessed
                            size, limit, resource, unit = spans[record]
                            book.mark_unread_span(record, seat.name, pool, size, limit, post.now().isoformat(),
                                                  resource=resource, unit=unit, commit=False)
                        marks.pop(record, None)
                        continue
                    complete = record in checkpoints
                    span_remains = not complete and book.has_unread_span(record)
                    if state is None:  # an empty record of unknown reader: settled, with nothing to account
                        if not span_remains:
                            book.mark_record(record, seat.name, pool, None, commit=False)
                        continue
                    extra = getattr(state, 'extra_usage', ())
                    if source_walls is None:
                        book.quota.record(record, seat.name, state.walls, book.provenance, post.now(),
                                          commit=False, extra_readings=extra)
                    else:
                        source_walls(record, seat.name, state.walls, post.now(), extra)
                    incomplete = "incomplete turn state in record" if (state.turns is None or state.turn_open is None) else None
                    if turns:
                        qualify = getattr(reader, "price_turn", lambda turn, price: turn)
                        turns = [qualify(t, book.provenance.price(record, t.started, t.ended)) for t in turns]
                        figures = {turns[-1].index: meter.result()} if live and record == current and meter and state.turn_open is False else {}
                        written = book.ingest(seat.name, UNKNOWN, record, turns, self.store, figures, commit=False)
                        if written:
                            if live and record == current:
                                meters[seat.name] = machine.Meter(pane.pid)
                            notices.append(("ledger", dict(seat=seat.name, record=record.name, turns=written)))
                    if incomplete or not span_remains:
                        book.mark_record(record, seat.name, pool, incomplete, commit=False)
                    if complete:
                        book.clear_unread_span(record, commit=False)
                    if not span_remains or record in held:
                        marks[record] = stamp
                    else:
                        marks.pop(record, None)
                    if record in held:
                        holds[record] = held[record]
                    else:
                        holds.pop(record, None)
                    if complete:
                        book.mark_accounted(record, checkpoints[record])
                    if record == current and seat.session.human:
                        human_readings.append((seat, record, state, turns, stamp[1]))
                quota.observe_screen(book, seat, screen, launched, commit=False)
            if replay:
                book.attributions.replay(self.store, self.runtime, commit=False, retry_interval=ACCOUNT_RETRY,
                                         framework=getattr(self, 'status_framework', None))
                book.finish_catchup(getattr(self, 'accounting_epoch', None))

        self.read_marks, self.account_errors, self.meters = marks, errors, meters
        self.read_holds = {record: hold for record, hold in holds.items() if record in marks}
        return observations, notices, human_readings

    @staticmethod
    def resumes_on(seat: Seat, pool: str) -> bool:
        """Whether a move to ``pool`` keeps the conversation, as Seat.failover_to decides."""
        return seat.spec.for_pool(pool).harness == seat.spec.harness and seat.resumable()

    def failover(self) -> None:
        """At a wall, try eligible later choices first, then earlier choices in declared order."""
        for seat in self.resolved_seats():
            if recovery.held(seat) or len(seat.spec.pools) < 2 or not seat.is_up:
                continue
            if (getattr(seat.harness, 'screen_walls', False) and self.ledger.catching_up()
                    and not quota.current_walled(seat, self.ledger)):
                screen = Door(seat).screen()
                if screen is not None and screen.quota:
                    with self.ledger.record_pass() as book:
                        quota.observe_screen(book, seat, screen, seat.launched(), commit=False)
            if not quota.current_walled(seat, self.ledger):
                continue
            binding, came_from = move_hold.generation(seat), seat.pool.name
            if hold := self._wall_idle_hold(seat):
                category = 'record-missing' if hold.reason == 'it has no joined record' else hold.dimension
                move_hold.publish_wall(seat, binding, came_from, category, detail=hold.reason)
                continue
            ordered = quota.candidate_pools(seat)
            target = next((p for p in ordered if self.known_open(seat, p)), None)
            if target is None:
                move_hold.publish_wall(seat, binding, came_from)
                continue  # every pool is walled or unread: a fact the status line shows, not a move
            from . import relaunch_intent
            from .harness import resume_target
            try:
                with relaunch_intent.section(self, seat, 'failover', resume=lambda: self.resumes_on(seat, target)):
                    proof = seat.failover_to(target)
            except resume_target.Unproved as error:
                move_hold.publish_wall(seat, binding, came_from, 'resume-unproved', target=target)
                self.log("failover", seat=seat.name, pool=target, skipped=str(error))
                continue
            except lifecycle.Busy:
                move_hold.publish_wall(seat, binding, came_from, 'lifecycle-busy', target=target)
                raise
            except (OSError, ShipError):
                move_hold.publish_wall(seat, binding, came_from, 'outcome-unknown', target=target)
                raise
            notice = f"moved to pool {target} from {came_from} at its wall"
            self.report_pool_move(seat, notice)
            self.log("failover", seat=seat.name, pool=target, came_from=came_from, proof=proof)

    def not_idle(self, seat: Seat) -> str | None:
        """The ordinary live proof shared by failover, select and relaunch."""
        _, hold = idle.reading(seat)
        return hold.reason if hold is not None else None

    def not_idle_at_wall(self, seat: Seat) -> str | None:
        hold = self._wall_idle_hold(seat)
        return hold.reason if hold is not None else None

    def _wall_idle_hold(self, seat: Seat) -> idle.Hold | None:
        """The live proof for a move off a pool, with one allowance at its wall: input queued in the program.

        A walled program never takes its queue, so a seat with input queued there would be held at its wall for
        good (its own notices of background commands are queued so). When its only hold is that queue, its turn
        is ended and nothing else is pending, and its pool is walled, it may move: the framework's own queued
        deliveries are carried into the new launch, as every relaunch carries them. Never in a person's session,
        where a queued input may be the person's own typing, which no carriage brings back.
        """
        state, hold = idle.reading(seat)
        if hold is None:
            return None
        if (hold.dimension == 'queue-pending' and state is not None and not seat.session.human
                and idle.check(replace(state, queued=())) is None
                and quota.current_walled(seat, self.ledger)):
            return None
        return hold

    def idle_reading(self, seat: Seat):
        """Compatibility wrapper for callers that already hold the service."""
        return idle.reading(seat)

    def select(self, seat: Seat, pool: str) -> str:
        """An explicit move to one of a seat's declared choices, by failover's path and refusals.

        The operator may choose a destination while the current pool still has room. Selection
        requires the same idle proof and refuses a walled destination. An unknown destination
        moves on the explicit act, and the answer says so.
        """
        if not seat.spec.pool_choices or pool not in seat.spec.pools:
            return f"{seat.name}: {pool} is not one of its declared choices ({', '.join(seat.spec.pools)}); nothing moved"
        if pool == seat.pool.name:
            return f"{seat.name} is already on {pool}"
        if reason := recovery.held(seat):
            return f"{seat.name} not moved: {reason}"
        if not seat.is_up:
            return f"{seat.name} not moved: it is down; qd up {seat.name} first"
        if reason := self.not_idle_at_wall(seat):
            if reason in (idle.REASONS['record'], 'its record is unreadable', 'it has no joined record') and self.resumes_on(seat, pool):
                seat._require_resume(seat.spec.for_pool(pool))
            return f"{seat.name} not moved: {reason}; ask again when its turn has ended"
        # The destination is read as failover would read it. Only a wall refuses the explicit act: a pool
        # whose usage is unknown, such as a background provider with no seat on it to read, is the human's call.
        unknown = not self.known_open(seat, pool)
        if unknown and self.ledger.walled(pool, self.ship, scope=quota.model_scope(seat, pool)):
            return f"{seat.name} not moved: {pool} is walled"
        came_from = seat.pool.name
        from . import relaunch_intent
        with relaunch_intent.section(self, seat, 'select', resume=lambda: self.resumes_on(seat, pool)):
            proof = seat.failover_to(pool)
        self.report_pool_move(seat, f"moved to pool {pool} from {came_from} by qd set")
        self.log("select", seat=seat.name, pool=pool, came_from=came_from, usage_unknown=unknown, proof=proof)
        note = f"; {pool} usage unknown; moved on the explicit act" if unknown else ""
        return f"{seat.name} moved to {pool} from {came_from}{note}\n{proof}"

    def report_pool_move(self, seat: Seat, notice: str) -> None:
        from .harness import resume_target

        launched = seat.launched()
        if getattr(launched, 'resume', '') is None:
            notice += f' and {resume_target.NEW_CONVERSATION}'
        self.store.new("report", post.SHIP, seat.name, notice)

    def known_open(self, seat: Seat, pool_name: str) -> bool:
        """A destination pool, judged by its own adapter's existing usage rules.

        A background provider uses its paced cache; other unread pools are read before the move; a program that reads no usage
        has only the Ledger's walls to go by, and the move says so."""
        pool = self.ship.pools[pool_name]
        if pool.kind in ("key", "endpoint"):  # no usage windows to read: a key is measured by spend, an endpoint has no quota
            return quota.cached_open(self.ship, self.ledger, pool_name, seat)
        reader = harnesses.native_for(pool.provider)
        if reader.background_usage or not reader.reads_usage:
            return quota.cached_open(self.ship, self.ledger, pool_name, seat)
        if not self.ledger.usage_known(pool_name, scope=quota.model_scope(seat, pool_name)):
            if quota.held(self, pool_name) or not self.ledger.quota.due(pool_name, post.now(), interval=reader.usage_interval):
                return False  # retained failures retry by attempt time, including after a restart, and a refusal's hold
            usage = quota.read_usage(self, pool, reader)
            if usage is None or usage.error:
                from .harness.limits import Usage
                usage = usage or Usage((), post.now(), "provider usage unavailable")
                quota.record_read(self, pool_name, usage, reader.usage_interval, post.now())
                self.log("failover", seat=seat.name, pool=pool_name, skipped=f"its usage could not be read: {usage.error}")
                return False
            quota.record_read(self, pool_name, usage, reader.usage_interval, post.now())
        return quota.cached_open(self.ship, self.ledger, pool_name, seat)

    def rebrief(self) -> None:
        """When rendered content changes, refresh briefs and tell affected running seats."""
        if self.framework_commit:
            from . import receipt_refresh
            try:
                receipt_refresh.run(self, self.framework_commit)
            except Exception as error:
                key = (type(error).__name__, str(error))
                if getattr(self, 'receipt_refresh_error', None) != key:
                    self.receipt_refresh_error = key
                    try:
                        self.log('error', step='receipt-refresh', error=str(error))
                    except OSError:
                        pass  # a refresh or logging failure cannot suppress brief publication
            else:
                self.receipt_refresh_error = None
        if self.framework_commit and (self.runtime / 'requests' / f'upgrade-rebrief-{self.framework_commit}.json').exists():
            return  # the durable upgrade publisher owns these edits until its request completes
        from . import brief_change
        stamp = self._brief_stamp()
        if stamp == self.brief_stamp:
            # Nothing new to publish; a git that has not answered is still asked, at most once a minute, so its
            # alarm counts and clears (law: nothing-waits-silently).
            if (waiting := brief_change.unread(self.runtime)) is not None and (
                    time.time() - float(waiting.get('at') or 0) >= brief_change.UNREAD_RETRY):
                self._brief_editor()
            return
        # A changed brief wakes no seat: nothing is posted. The change reaches each seat at the head of its next
        # message (brief_change); the seat whose own commit made it is not told of it. Where git does not answer,
        # the change is no one's own: every seat it changed is told, its editor included.
        own = self._brief_editor()
        selected = self.resolved_seats()
        for seat in selected:
            if (text := self.brief_texts.get(seat.name)) is None:
                continue  # a selection resolved after the stamp; retry it next pass
            path = self.runtime / 'brief' / f'{seat.name}.md'
            before = path.read_text() if path.exists() else None
            brief_change.seed(self.runtime, seat.name, before if before is not None else text)
            path = standing.render(self.ship, seat.spec, text=text)
            if seat.name == own and brief_change.told(self.runtime, seat.name) == before:
                brief_change.mark(self.runtime, seat.name, path.read_text())
        if len(selected) == len(self.brief_texts) == len(self.ship.seats):
            self.brief_stamp = stamp
        self.log("rebrief", seats=[seat.name for seat in selected])

    def _brief_editor(self) -> str | None:
        """The seat whose own commit made the ship folder's change, or None. A git that does not answer gives None,
        leaves the ship-head reading where it was (brief_change.editor) and counts one failing pass."""
        from . import brief_change
        if not getattr(self.ship, 'is_repository', False):
            return None
        try:
            own = brief_change.editor(self.ship.folder, self.runtime)
        except brief_change.Unread as error:
            brief_change.note_unread(self.runtime, str(error), time.time())
            if getattr(self, 'brief_change_error', None) != str(error):
                self.brief_change_error = str(error)
                self.log('error', step='brief-change', error=str(error))
            return None
        if self.__dict__.pop('brief_change_error', None) is not None or brief_change.unread(self.runtime) is not None:
            brief_change.answered(self.runtime)
        return own

    def upgrade_rebrief(self, data: dict) -> str | None:
        """A durable baseline survives rendering and retries. A changed brief wakes no seat: nothing is posted, and
        each change reaches its seat at the head of that seat's next message (brief_change)."""
        from . import brief_change
        stamp = self._brief_stamp()
        changed = []
        selected = self.resolved_seats()
        for seat in selected:
            if (text := self.brief_texts.get(seat.name)) is None:
                continue  # a selection resolved after the stamp; retry it next pass
            launch = seat.trusted / trusted_files.LAUNCH
            same = (data['launches'].get(seat.name) is not None
                    and install.launch_stamp(launch) == data['launches'][seat.name] and seat.is_up)
            if same and data['before'].get(seat.name) is None:
                return None  # no known prior brief to compare; never overwrite that unknown baseline
            digest = hashlib.sha256(text.encode()).hexdigest()
            path = self.runtime / 'brief' / f'{seat.name}.md'
            before = path.read_text() if path.exists() else None
            brief_change.seed(self.runtime, seat.name, before if before is not None else text)
            standing.render(self.ship, seat.spec, text=text)
            if digest != data['before'].get(seat.name):
                changed.append(seat.name)
        if len(selected) != len(self.ship.seats) or len(self.brief_texts) != len(self.ship.seats):
            return None  # healthy briefs progressed; the unresolved seat keeps this operation held
        self.brief_stamp = stamp  # edits arriving during this pass still belong to ordinary rebrief
        if changed:
            proof = ('rebrief complete · changed briefs: ' + ', '.join(changed)
                     + " · each carried at the head of its seat's next message; no notice posted")
        else:
            proof = 'rebrief complete · no brief changed'
        return json.dumps(dict(token=data['token'], status='complete', proof=proof))
