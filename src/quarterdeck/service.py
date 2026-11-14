"""The Service: the ship's one long-running process, not a seat.

Started by ``qd up`` and owned by whoever ran it, it renders inboxes, delivers messages
through the doors, writes attempts, answers requests (controls, stops, resends), starts
workers fresh after their orders, re-briefs seats when the orders change, and writes the
heartbeat the status line reads. Every tick is one pass; nothing waits on a clock longer
than the tick (law: events-not-clocks).
"""

from __future__ import annotations

import json
import hashlib
import os
import signal
import sys
import time
import traceback
import threading
from copy import deepcopy
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from . import harness as harnesses
from . import shutdown, lifecycle
from .harness import reader_stamp
from . import pending as pending_delivery, delivery_record
from . import accounting_health, credential, durable, idle, install, machine, move_hold, post, post_dir, presentation, quota, record_owner, recovery, servicectl, ship as shipmod, standing
from .door import CONFIRM_SECONDS, Door
from .harness.base import record_fingerprint
from .harness.capabilities import validate_effort, validate_mode
from .ledger import Ledger
from .provenance import UNKNOWN
from .post import Attempts, Store
from .seat import Seat, SelectionError, UnresolvedSeat, seats
from .servicectl import (  # noqa: F401  (the process control, reachable as service.* too)
    acquire_lock, alive, heartbeat_path, locked, read_heartbeat, request, stale, start, stop, write_heartbeat,
)
from .ship import Ship, ShipError
from .tmux import Tmux, TmuxError, ship_sessions

TYPING_QUIET = 30  # seconds since a client's last key or mouse input before its session may be switched
STATUS_INTERVAL = 0.7  # presentation only; rendering age and owner are checked independently
ACCOUNT_RETRY = 60.0  # unchanged transient source failures, never provider pacing


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


def ship_file_stamp(ship) -> tuple | None:
    """The ship file's identity and change times, or None where it is not on disk."""
    try:
        info = ship.file.stat()
    except (OSError, AttributeError):
        return None
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


class Service:
    def __init__(self, ship: Ship, tmux: Tmux):
        self.ship, self.tmux = ship, tmux
        self.ship_stamp = None  # the ship file's identity as this pass's load read it; None: not loaded by a pass
        self.runtime = ship.runtime
        self.store = Store(self.runtime)
        self.attempts = Attempts(self.runtime)
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
        self.ledger = Ledger(self.runtime / "ledger.sqlite")
        reader_keys = reader_stamp.attributions()
        self.accounting_epoch = reader_keys if all(reader_keys.values()) else None
        self.ledger.begin_catchup(self.accounting_epoch)
        self.meters: dict[str, machine.Meter] = {}
        self.usage_read: dict[str, float] = {}
        self.usage_hold: dict[str, float] = {}  # a refused pool's Retry-After, by the monotonic clock
        self.usage_logged: dict[str, str] = {}  # each pool's last logged usage failure
        self.read_marks: dict[Path, tuple] = {}  # record -> the fingerprint it was last read at
        self.account_errors: dict[Path, tuple] = {}  # evidence, error, next transient read time
        self.accounting_health = accounting_health.Health()  # a new process starts unchecked
        self.accounting_health_error = None
        self.accounting_health_due = 0.0
        self.heartbeat_lock = threading.Lock()
        self.working: dict[str, bool] = {}  # seat -> whether its record last showed a turn open
        self.work_marks: dict[str, tuple] = {}  # seat -> (record, fingerprint) its working state was read at
        self.blind_checked = -BLIND_INTERVAL  # when the blind attempts were last read against the records
        self.loaded_attempts: list | None = None  # the attempts reconcile loaded this pass; the look reads these
        self.next_status = 0.0
        self.status_checked: float | None = None
        from .structure_outcome import Passes
        self.structure_passes = Passes()

    def run(self) -> None:
        self.lock = acquire_lock(self.runtime)  # one service per ship, for its whole life
        servicectl.acknowledge(self.runtime, self.copy_identity, self.copy_start_token)
        self.log("start", pid=os.getpid())
        from .shutdown import Stop
        beat_stop = threading.Event()
        def keep_heartbeat():
            while not beat_stop.wait(2.0):
                try:
                    with self.heartbeat_lock:
                        write_heartbeat(self.runtime)
                except OSError:
                    pass  # the main step reports storage failures through its usual path
        beat = threading.Thread(target=keep_heartbeat, name='service-heartbeat', daemon=True)
        beat.start()
        try:
            with Stop(self.runtime, self.lock).installed():
                while True:
                    self.tick()
                    until = time.monotonic() + self.ship.tick  # read after tick() reloads the ship file
                    while time.monotonic() < until:
                        self.status_due()
                        remaining = until - time.monotonic()
                        if remaining > 0:
                            time.sleep(min(remaining, max(0.001, self.next_status - time.monotonic())))
        finally:
            beat_stop.set()
            beat.join(timeout=0.2)
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

    def tick(self) -> None:
        """One pass: the ship file read afresh, then every duty, each failing alone."""
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
            self._tick_steps()
        finally:
            self._accounting_tick = False
            self.structure_passes.finish()

    def _tick_steps(self):
        # Operational duties keep their order and never wait behind the slice.
        for step in (self.server_recovery, self.reconcile, self.file_drops, self.groups, self.cascades, self.fleet_post, self.inboxes, self.recoveries, self.refusals, self.answers, self.looks, self.deliveries, self.follow, self.requests, self.compactions, self.person_quiet, self.fresh_after_order,
                     self.rebrief, self.push_reminders, self.credentials, self.authentications, self.model_differences, self.failover, self.account, self.refresh_usage, self.pool_forecasts, self.retire_post):
            self.heartbeat()  # before every step: a slow delivery or usage read reads as busy, never as dead
            try:
                step()
            except Exception as error:  # one failing step never stops the others
                self.log("error", step=step.__name__, error=repr(error), trace=traceback.format_exc())
            self.status_due()

    def pool_forecasts(self) -> None:
        from . import usage_forecast
        usage_forecast.notify(self)

    def retire_post(self) -> None:
        """Move only finished, settled delivery files after accounting catch-up."""
        from . import post_retirement
        post_retirement.due(self)

    def status_due(self) -> None:
        now = time.monotonic()
        if self.status_checked is not None and self.status_checked <= now < self.next_status:
            return
        self.status_checked = now
        self.next_status = now + STATUS_INTERVAL
        try:
            sessions = [session.title for session in self.ship.sessions.values()]
            demand = self.tmux.clients_attached(sessions) if hasattr(self.tmux, 'clients_attached') else None
            if demand is not False:
                self.heartbeat()
                self.publish_status()
        except Exception as error:
            self.log('error', step='publish_status', error=repr(error), trace=traceback.format_exc())

    def publish_status(self) -> None:
        presentation.publish(self.ship, self.tmux, self.status_framework, self.store, self.attempts)

    def push_reminders(self) -> None:
        """A local origin/main change starts a durable reminder, without reading GitHub."""
        from . import push_reminder
        push_reminder.run(self)

    def heartbeat(self) -> None:
        with self.heartbeat_lock:
            write_heartbeat(self.runtime)
        self.publish_accounting_health()

    def publish_accounting_health(self, *, force=False) -> None:
        now = time.monotonic()
        if not force and now < self.accounting_health_due:
            return
        self.accounting_health_due = now + self.ship.tick  # once a pass, alongside the heartbeat
        try:
            accounting_health.publish(self.runtime, self.accounting_health)
        except OSError as error:
            reason = accounting_health.cause(error)
            if self.accounting_health_error is None:
                self.log('accounting-health-unavailable', cause=reason)
            self.accounting_health_error = reason
        else:
            if self.accounting_health_error is not None:
                self.log('accounting-health-available')
            self.accounting_health_error = None

    def log(self, event: str, **data) -> None:
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

    def reconcile(self) -> None:
        """Reconcile journals first, then unjournaled attempts through the same fresh receipt rule.

        Live owners keep their confirm window; queued inputs need consumption.
        Blind attempts are re-read at their existing minute cadence, never resent.
        """
        tracked = pending_delivery.reconcile(self)
        retry_blind = time.monotonic() - self.blind_checked >= BLIND_INTERVAL
        if retry_blind:
            self.blind_checked = time.monotonic()
        records = self.attempts.all()
        self.loaded_attempts = records
        latest = self.attempts.latest(attempts=records)
        queued = {message for message, result in latest.items() if result == 'queued'}
        blind = {message for message, result in latest.items() if result == 'blind'}
        for index, attempt in enumerate(records):
            if index % 200 == 0:
                self.heartbeat()
            if attempt.path in tracked or attempt.seat not in self.ship.seats:
                continue
            result = attempt.result
            if result is None:
                if time.time() < attempt.when.timestamp() + CONFIRM_SECONDS:
                    continue
                if attempt.pane is None:
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

    def deliveries(self) -> None:
        results = self.attempts.latest()
        for seat in self.resolved_seats():
            if seat.name in getattr(self.ship, 'forward', {}):
                continue  # declared forwarding disables dispatch at the old address
            if seat.session.human:
                from . import human_wake, turn_failure
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
                except Exception as error:
                    self.human_wake_error(seat, 'alarm', error)
                continue
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

    def systems_recipients(self, seat: Seat) -> list[str]:
        """Every up systems seat, excluding the subject of its own alarm."""
        return [spec.name for spec in self.ship.seats.values()
                if spec.role == 'systems' and spec.name != seat.name and self._up(spec.name)]

    def model_differences(self) -> None:
        from . import model_mismatch, request_queue
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

    def _deliver_claimed(self, seat: Seat, messages: list[post.Message], resend: bool = False) -> str:
        """One seat's batch, with its existing lifecycle claim already held."""
        if resend:
            from . import paste_refusal
            door = Door(seat)
            screen = door.screen()
            if reading := door.initial_obstruction(screen):
                return f'{seat.name}: {reading}'
            if len(messages)==1 and (reason:=paste_refusal.refused(seat,post.typed(messages),screen)):
                return f'{messages[0].short} to {seat.name} · withheld: {reason}'
            pending_delivery.explicit_resend(self, seat, messages, screen)
        latest = self.attempts.last_results(seat.name)
        ordered = sorted(messages, key=lambda message: latest.get(message.id) == "withheld")
        lines = []
        for message in ordered:
            lines.append(self._deliver_current(seat, message, resend))
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

    @shutdown.transaction
    def _deliver_current(self, seat: Seat, message: post.Message, resend: bool = False) -> str:
        """The caller owns the same seat claim as every lifecycle transition."""
        door = Door(seat)
        speed = message.speed
        reading = door.reading()
        clearable = reading.clears == "dismissable" or (speed == "interrupt" and reading.clears == "time")
        if not reading.open and not clearable:  # the door itself clears a known notice or interrupts; this only spares an attempt file
            return f"{seat.name}: {reading}"
        with post.admission(self.runtime, seat.name):
            from . import compaction
            reserved, transitioning = compaction.withheld(self.runtime, seat.name)
            if transitioning or message.id in reserved:
                return f"{seat.name}: compaction handoff/transition reserved; held for successor"
            bound = pending_delivery.current_identity(seat)
            if bound is None:
                return f'{seat.name}: current pane/process/launch unproved; message waits unread'
            attempt = self.attempts.open(message, seat.name, bound['pane'], bound['pid'], bound['launch'])
            text = post.typed([message], second=(message.id,) if resend else ())  # a resend says it is one
            try:
                proof = pending_delivery.Proof.begin(seat, attempt, text, before_paste=True)
            except pending_delivery.NoCurrentLaunch as error:
                self.attempts.close(attempt, 'withheld', 'launch ended before reservation; no input typed')
                return str(error)
        delivery = door.deliver(text, speed, proof=proof)
        if proof:
            proof.finish(delivery.result)
        detail = delivery.detail + (" · resend" if resend else "")
        screen = door.screen()
        self.attempts.close(attempt, delivery.result, detail)
        if delivery.result == "withheld":
            self.withheld[(attempt.message, seat.name)] = screen.text if screen else ""
        self.log("delivery", seat=seat.name, messages=[message.id], speed=speed, result=delivery.result, detail=detail)
        return f"{message.short} to {seat.name} · {delivery.result}: {detail}"

    def fleet_post(self) -> None:
        from . import fleet_post
        errors = fleet_post.run(self.ship, self.store)
        path = self.runtime / 'post' / 'transport-holds.json'
        if errors:
            fleet_post.prepare_outbound(self.runtime)
            text = json.dumps(errors, sort_keys=True) + '\n'
            if not path.exists() or path.read_text() != text:
                fleet_post.replace_status(path, text)
        else:
            path.unlink(missing_ok=True)

    def inboxes(self) -> None:
        for name in self.ship.seats:
            path = self.runtime / "post" / "inbox" / f"{name}.md"
            try:
                text = post.inbox_text(self.store, name)
                try:
                    current = post_dir.read_text(path)
                except FileNotFoundError:
                    current = None
                if current != text:
                    post.render_inbox(self.runtime, self.store, name)
            except ShipError as error:
                self.log("error", step="inbox", seat=name, error=str(error))

    def file_drops(self) -> None:
        """File each seat's drop. The service sets the sender from the folder; a refusal is held and logged once."""
        for folder, reason in post.file_drops(self.store, self.ship):
            self.log("drop-held", folder=folder, reason=reason)

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


    def requests(self) -> None:
        """Each request answered alone, in name order: none keeps a later one waiting.

        A rebrief persists its intent and posts each notice once, so one that fails stays to be tried
        again; its error is raised once every other request has had its answer, and the tick logs it.
        """
        folder = self.runtime / "requests"
        retry = []
        for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
            from . import request_queue
            with ExitStack() as ownership:
                if not ownership.enter_context(request_queue.coordination(self.runtime, wait=False)):
                    continue  # owner busy: defer without delaying the remaining service duties
                if not path.exists():
                    continue
                data = None
                try:
                    claim = path.with_suffix(".claimed")
                    meta = request_queue.metadata(path)
                    if 'terminal' in meta:
                        data = request_queue.load(path)
                        if not isinstance(meta['terminal'], str) or not meta['terminal'] or data.get('kind') not in request_queue.SEAT_KINDS:
                            raise ShipError('request retirement intent unknown; retained')
                        request_queue.settle(path, meta['terminal'], clear_claim=True)
                        continue  # recover cancellation/supersession without ever dispatching it
                    data = request_queue.load(path)
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
                        durable.replace_file(claim, raw)  # before dispatch: failed archival cannot requeue an executed action
                    # The durable claim excludes cancellation/supersession while dispatch runs.
                    # A seat transition must never hold the global request coordination lock.
                    ownership.close()
                    try:
                        self.request_wait = None
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

    def clear_request_claim(self, claim: Path) -> None:
        claim.unlink(missing_ok=True)

    def failed_request(self, path: Path, error: Exception) -> None:
        """Keep the raw request and trace; an archival/answer failure leaves a visible claim, never a replay."""
        claim = path.with_suffix(".claimed")
        if not claim.exists():
            durable.replace_file(claim, path.read_text())  # malformed input also leaves the active queue on a terminal error
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
            durable.replace_file(path.with_suffix(".done"), f"failed: {error!r}; the request is kept in "
                                 f"requests/{kept.relative_to(path.parent)} and the service log has the trace\n")
            if kept != claim:
                self.clear_request_claim(claim)
        except OSError as answer_error:
            self.log("error", step="requests", request=path.name, answer_unavailable=True,
                     error=repr(answer_error), trace=traceback.format_exc())

    def answer(self, data: dict) -> str | None:
        kind = data["kind"]
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
            from . import compaction
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
            return self.deliver(seat, [message], resend=True)
        if kind == "submit":
            return pending_delivery.submit(self, self.seat(data["seat"]))
        if kind in ("press", "type"):  # Systems' act on a screen the door does not know (law: one-door)
            from . import acts
            seat = self.seat(data["seat"])
            if kind == "press":
                return acts.press(self, seat, str(data["choice"]), data.get("caller"))
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

    def relaunch_request(self, data):
        """A human's own asking turn is its only deferred break and later speech retires it, but for a fresh start
        asked with an open handoff, which waits for any quiet moment and is retired by the person alone."""
        from . import request_queue
        meta = getattr(self, 'request_metadata', {})
        data = {**data, **meta}
        seat = self.seat(data['seat'])
        origin = request_queue.launch_binding(seat)
        if data.get('origin') is not None and data['origin'] != origin:
            return f'refused: {seat.name} launch changed; request retired without applying'
        if not seat.session.human:
            return self.relaunch(seat, bool(data['resume']))
        pane = seat.pane()
        if pane is None or pane.dead:
            return self.relaunch(seat, bool(data['resume']))
        state, hold = idle.reading(seat, explicit=True, relaunch=True)
        try:
            asked = datetime.fromisoformat(data['created_at'])
        except (KeyError, TypeError, ValueError):
            return f'refused: {seat.name} asking turn unknown; no request will apply later'
        if state is None and hold is None:
            from .servicectl import ANSWER_SECONDS
            if asked.tzinfo is None or not 0 <= (post.now() - asked).total_seconds() <= ANSWER_SECONDS:
                return f'refused: {seat.name} no-record request expired; no deferred restart'
            return self.relaunch(seat, bool(data['resume']), reading=(state, hold))
        if data.get('caller') == seat.name and not data['resume'] and asked.tzinfo is not None:
            handed = self.handoff_fresh(seat, asked, (state, hold))
            if handed is not OWN_TURN_RULE:
                return handed
        if (state is None or state.last_input_at is None or asked.tzinfo is None
                or state.last_input_at.tzinfo is None or state.last_input_at > asked or asked > post.now()):
            return f'refused: {seat.name} asking turn unknown or superseded by later input; no deferred restart'
        own = data.get('caller') == seat.name
        if not own:
            from .servicectl import ANSWER_SECONDS
            ending = state.completed_turns[-1] if state.completed_turns else None
            question = seat.harness.waiting_for_human(state)
            if ((post.now() - asked).total_seconds() > ANSWER_SECONDS
                    or not question and (ending is None or ending.ended is None or ending.ended > asked)):
                return f'refused: {seat.name} was not proved at an immediate break when requested; no deferred restart'
            answer = self.relaunch(seat, bool(data['resume']), reading=(state, hold))
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
        answer = self.relaunch(seat, bool(data['resume']), reading=(state, hold))
        return answer if answer is not None else f'refused: {seat.name} asking turn ended with {(self.request_wait or (None, "idle proof unknown"))[1]}; request retired, never applied at a later turn'

    def handoff_fresh(self, seat: Seat, asked: datetime, reading):
        """A human seat's own fresh start, asked once it has an open handoff to itself, applies at its next quiet
        moment. What reaches it later, a delivery or the program's own notice, is carried into the fresh start as
        an open message and never retires the request; the person's own input does. Where no such handoff was
        posted before the request, or the person's input cannot be told from the program's notices, the asking
        turn's own rule decides (OWN_TURN_RULE)."""
        from . import person_quiet
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
        person = person_quiet.read(seat, post.now())
        if person['at'] is None:
            if bound is None:
                return OWN_TURN_RULE
            return f"refused: {seat.name} person's input unknown ({person['reason']}); no deferred restart"
        if datetime.fromisoformat(person['at']) > asked:
            return f"refused: {seat.name} superseded by the person's later input; no deferred restart"
        meta['handoff'] = handoff.id
        state, hold = reading
        self.request_wait = self.handoff_quiet(seat, state, hold)
        if self.request_wait is not None:
            return None  # a turn, a queued input, an unconfirmed delivery or the box: the next quiet moment
        return self.relaunch(seat, False, reading=reading)  # its own hold, if any, also waits

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
        screen = Door(seat).screen()
        if (screen is None or screen.box != '' or screen.working is not False or screen.queued
                or screen.unknown or screen.interruption or screen.starting):
            return ('box', 'the box is not proved empty and idle')
        return None

    def relaunch(self, seat: Seat, resume: bool, *, reading=None) -> str | None:
        """Explicit recovery alone may interrupt a proven question-only wait."""
        if reason := recovery.held(seat):
            if self is not None:
                self.request_wait = ('recovery', reason)
            return None
        pane = seat.pane()
        if pane is not None and not pane.dead:
            state, hold = reading if reading is not None else idle.reading(seat, explicit=True, relaunch=True)
            if state is None or not seat.harness.waiting_for_human(state):
                if hold is not None:
                    if self is not None:
                        self.request_wait = (hold.dimension, hold.reason)
                    return None  # deferred-request answers/lifetime are separate; the reason is shared
        return seat.restart() if resume else seat.fresh()

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

    def fresh_after_order(self) -> None:
        """A worker whose last open order closed, and whose closing turn ended, starts fresh."""
        from . import compaction
        for seat in self.resolved_seats():
            if seat.session.human or recovery.held(seat) or seat.spec.fresh != "after-order" or not seat.is_up or self.store.open_for(seat.name):
                continue
            launched = seat.launched()
            if launched is None:
                continue
            try:
                protocol = compaction.protocol_requests(seat)
            except ShipError:
                continue  # unknown protocol identity cannot authorize another automatic path
            closed = max((report.at for order in self.store.for_seat(seat.name)
                          if order.kind == "order" and order.id not in protocol
                          and not (order.sender == order.to and order.re in protocol)
                          and (report := self.store.closing_report(order))
                          and report.at > launched.when), default=None)
            if closed is None or (record := seat.record()) is None:
                continue
            try:
                state = seat.harness.state(record)
            except OSError:
                continue  # no screen or default idle reading can prove a recorded closing turn
            if (state.turn_open is False and state.queue_error is None and not state.queued
                    and any(turn.timed and turn.started is not None and turn.ended is not None
                            and turn.started <= closed <= turn.ended for turn in state.completed_turns)):
                proof = seat.fresh()  # law: context-is-cache
                self.log("fresh", seat=seat.name, proof=proof)

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
        once it has ended, its retirement is finished here: reseeded, and its launch removed.
        """
        ready = []
        for seat in self.resolved_seats():
            if seat.retiring() is None:
                ready.append(seat)
                continue
            try:
                if proof := seat.finish_retirement():
                    self.log("retired", seat=seat.name, proof=proof)
            except ShipError as error:
                self.log("error", step="retire", seat=seat.name, error=str(error))
        for line in credential.reseed(self.ship, ready):
            self.log("credential", proof=line)

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
        before = self.accounting_health.shown()
        try:
            observations, notices, human_readings = self.account_records()
        except Exception as error:
            if self.accounting_health.failed(accounting_health.cause(error), post.now()):
                self.log('accounting-failing', since=self.accounting_health.since,
                         cause=self.accounting_health.current, failures=self.accounting_health.failures,
                         causes=dict(self.accounting_health.causes))
            self.publish_accounting_health(force=True)
            self.accounting_shown_changed(before)
            return  # no per-pass raw exception log; the bounded operational alarm owns this failure
        previous = self.accounting_health.since
        failures = self.accounting_health.failures
        causes = dict(self.accounting_health.causes)
        if self.accounting_health.committed():
            self.log('accounting-recovered', since=previous, failures=failures, causes=causes)
        self.publish_accounting_health(force=True)
        self.accounting_shown_changed(before)
        # These duties happen after commit. Their failures retain tick's existing
        # error visibility and cannot be misreported as rolled-back accounting.
        for event, data in notices:
            self.log(event, **data)
        for seat, record, state, turns, stamp in human_readings:
            from . import turn_failure
            turn_failure.publish(seat, record, state, turns, stamp)
        for seat, _, launched, live, _, _, _, _ in observations:
            quota.refresh(self, seat, live, launched)
        quota.refresh_declared(self)

    def accounting_shown_changed(self, before) -> None:
        """A status snapshot taken before the outcome would disagree with it: the next status check republishes."""
        if self.accounting_health.shown() != before:
            self.status_checked = None

    def refresh_usage(self):
        """Live provider evidence is independent of a historical accounting backlog."""
        if not getattr(self, 'usage_pending', False):
            return
        self.usage_pending = False
        for seat in self.resolved_seats():
            pane = seat.pane()
            quota.refresh(self, seat, pane is not None and not pane.dead, seat.launched())
        quota.refresh_declared(self)

    def account_records(self, *, source_records=None, source_screens=None, source_walls=None,
                        replay=True, process_table=None, source_reader_keys=None):
        """Prepare and commit the pass; callers handle only its accounting outcome."""
        # A usage write whose commit was refused is completed first, never rolled back, so that no
        # record is read while its transaction stands; if it fails again the pass fails and is counted.
        self.ledger.prepare_usage()
        table = machine.processes() if process_table is None else process_table
        observations, notices, human_readings = [], [], []
        marks, errors = dict(self.read_marks), dict(self.account_errors)
        meters = deepcopy(self.meters)
        checkpoints = {}
        reader_keys = reader_stamp.readers() if source_reader_keys is None else source_reader_keys
        for seat in self.resolved_seats():
            pane, launched = seat.pane(), seat.launched()
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
                pool = launched.pool if (record == current and launched) else UNKNOWN
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
                    stamp = (owned.name, record_fingerprint(record))
                    # The launches joined to this record. A down seat or a newer launch of another record does not move it.
                    launch = self.ledger.provenance.owning_launch(prepared[0], record)
                    evidence = (stamp, owned.reason, record_fingerprint(record_owner.registry(seat.home.parent)), launch)
                    # New launch/price evidence for this record must be reconciled even if its bytes stand still.
                    if marks.get(record) == stamp and prepared[2] is None:
                        continue
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
                        stable = stamp == (owned.name, record_fingerprint(record))
                    except OSError:
                        stable = False
                    # Empty records retain ordinary read/error checks: no measured turn was accounted.
                    if stable and turns and state.turns is not None and state.turn_open is not None:
                        checkpoints[record] = checkpoint
                except OSError as error:
                    message = str(error)
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
                    if error is not None:
                        book.mark_accounted(record, None)
                        book.mark_record(record, seat.name, pool, error, commit=False, evidence=settlement)
                        marks.pop(record, None)
                        continue
                    if state is None:  # an empty record of unknown reader: settled, with nothing to account
                        book.mark_record(record, seat.name, pool, None, commit=False)
                        continue
                    if source_walls is None:
                        book.quota.record(record, seat.name, state.walls, book.provenance, post.now(), commit=False)
                    else:
                        source_walls(record, seat.name, state.walls, post.now())
                    incomplete = "incomplete turn state in record" if (state.turns is None or state.turn_open is None) else None
                    book.mark_record(record, seat.name, pool, incomplete, commit=False)
                    if turns:
                        qualify = getattr(reader, "price_turn", lambda turn, price: turn)
                        turns = [qualify(t, book.provenance.price(record, t.started, t.ended)) for t in turns]
                        figures = {turns[-1].index: meter.result()} if live and record == current and meter and state.turn_open is False else {}
                        written = book.ingest(seat.name, UNKNOWN, record, turns, self.store, figures, commit=False)
                        if written:
                            if live and record == current:
                                meters[seat.name] = machine.Meter(pane.pid)
                            notices.append(("ledger", dict(seat=seat.name, record=record.name, turns=written)))
                    marks[record] = stamp
                    if record in checkpoints:
                        book.mark_accounted(record, checkpoints[record])
                    if record == current and seat.session.human:
                        human_readings.append((seat, record, state, turns, stamp[1]))
                quota.observe_screen(book, seat, screen, launched, commit=False)
            if replay:
                book.attributions.replay(self.store, self.runtime, commit=False, retry_interval=ACCOUNT_RETRY,
                                         framework=getattr(self, 'status_framework', None))
                book.finish_catchup(getattr(self, 'accounting_epoch', None))

        self.read_marks, self.account_errors, self.meters = marks, errors, meters
        return observations, notices, human_readings

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
            if self.not_idle(seat):
                continue
            ordered = quota.candidate_pools(seat)
            target = next((p for p in ordered if self.known_open(seat, p)), None)
            if target is None:
                continue  # every pool is walled or unread: a fact the status line shows, not a move
            came_from = seat.pool.name
            proof = seat.failover_to(target)
            self.store.new("report", post.SHIP, seat.name, f"moved to pool {target} from {came_from} at its wall")
            self.log("failover", seat=seat.name, pool=target, came_from=came_from, proof=proof)

    def not_idle(self, seat: Seat) -> str | None:
        """The ordinary live proof shared by failover, select and relaunch."""
        _, hold = idle.reading(seat)
        return hold.reason if hold is not None else None

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
        if reason := self.not_idle(seat):
            return f"{seat.name} not moved: {reason}; ask again when its turn has ended"
        # The destination is read as failover would read it. Only a wall refuses the explicit act: a pool
        # whose usage is unknown, such as a background provider with no seat on it to read, is the human's call.
        unknown = not self.known_open(seat, pool)
        if unknown and self.ledger.walled(pool, self.ship, scope=quota.model_scope(seat, pool)):
            return f"{seat.name} not moved: {pool} is walled"
        came_from = seat.pool.name
        proof = seat.failover_to(pool)
        self.store.new("report", post.SHIP, seat.name, f"moved to pool {pool} from {came_from} by qd set")
        self.log("select", seat=seat.name, pool=pool, came_from=came_from, usage_unknown=unknown, proof=proof)
        note = f"; {pool} usage unknown; moved on the explicit act" if unknown else ""
        return f"{seat.name} moved to {pool} from {came_from}{note}\n{proof}"

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
        stamp = self._brief_stamp()
        if stamp == self.brief_stamp:
            return
        selected = self.resolved_seats()
        for seat in selected:
            if (text := self.brief_texts.get(seat.name)) is None:
                continue  # a selection resolved after the stamp; retry it next pass
            path = self.runtime / 'brief' / f'{seat.name}.md'
            before = path.read_text() if path.exists() else None
            standing.render(self.ship, seat.spec, text=text)
            if seat.is_up and path.read_text() != before:
                self.store.new("report", "ship", seat.name, f"ship's orders changed · read runtime/brief/{seat.name}.md")
        if len(selected) == len(self.brief_texts) == len(self.ship.seats):
            self.brief_stamp = stamp
        self.log("rebrief", seats=[seat.name for seat in selected])

    def upgrade_rebrief(self, data: dict) -> str | None:
        """A durable baseline survives rendering and retries; posting is not proof of reading."""
        stamp = self._brief_stamp()
        affected, changed = [], []
        selected = self.resolved_seats()
        for seat in selected:
            if (text := self.brief_texts.get(seat.name)) is None:
                continue  # a selection resolved after the stamp; retry it next pass
            launch = seat.folder / 'launch.json'
            same = (data['launches'].get(seat.name) is not None
                    and install.launch_stamp(launch) == data['launches'][seat.name] and seat.is_up)
            if same and data['before'].get(seat.name) is None:
                return None  # no known prior brief to compare; never overwrite that unknown baseline
            digest = hashlib.sha256(text.encode()).hexdigest()
            notice = self.upgrade_notice(data, seat.name, digest) if same else None
            standing.render(self.ship, seat.spec, text=text)
            if notice is not None or (not same and digest != data['before'].get(seat.name)):
                changed.append(seat.name)
            if notice is not None:
                target = self.store.dir / f'{notice.id}.json'
                if target.exists():
                    if post.Message.from_json(target.read_text()) != notice:
                        raise ShipError('sync brief notice identity conflicts with an immutable post')
                else:
                    self.store.write(notice)
                affected.append(seat.name)
        if len(selected) != len(self.ship.seats) or len(self.brief_texts) != len(self.ship.seats):
            return None  # healthy briefs progressed; the unresolved seat keeps this operation held
        self.brief_stamp = stamp  # edits arriving during this pass still belong to ordinary rebrief
        if changed:
            proof = 'rebrief complete · changed briefs: ' + ', '.join(changed)
            proof += ' · notices posted: ' + (', '.join(affected) or 'none') + ' · delivery/read unconfirmed'
        else:
            proof = 'rebrief complete · no brief changed'
        return json.dumps(dict(token=data['token'], status='complete', proof=proof))

    def upgrade_notice(self, data: dict, name: str, digest: str) -> post.Message | None:
        """Advance from offered content, retaining one recoverable notice intent per seat."""
        body = f"brief changed · read runtime/brief/{name}.md"
        notices = data.setdefault('notices', {})
        pending = notices.get(name)
        before, previous = data['before'][name], f"{data['token']}:{name}"
        if pending:
            target = self.store.dir / f"{pending['id']}.json"
            if target.exists():
                expected = post.Message(pending['id'], 'report', post.SHIP, name,
                                        datetime.fromisoformat(pending['at']), body)
                if post.Message.from_json(target.read_text()) != expected:
                    raise ShipError('sync brief notice identity conflicts with an immutable post')
                before, previous = pending['hash'], pending['id']
            else:
                previous = pending['previous']
        if digest == before:
            return None
        key = hashlib.sha256(f'{previous}:{digest}'.encode()).hexdigest()[:16]
        identity = f'rep-upgrade-{key}'
        at = pending['at'] if pending and pending['id'] == identity else post.now().isoformat()
        data['before'][name] = before
        notices[name] = dict(hash=digest, id=identity, at=at, previous=previous)
        install.write_rebrief(self.runtime / 'requests' / f"upgrade-rebrief-{data['commit']}.json", data)
        return post.Message(identity, 'report', post.SHIP, name, datetime.fromisoformat(at), body)
