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
from datetime import datetime
from pathlib import Path

from . import harness as harnesses
from . import pending as pending_delivery, delivery_record
from . import credential, durable, install, machine, post, presentation, quota, record_owner, recovery, ship as shipmod, standing
from .door import CONFIRM_SECONDS, Door
from .harness.base import record_fingerprint
from .ledger import Ledger
from .provenance import UNKNOWN
from .post import Attempts, Store
from .seat import Seat, SelectionError, UnresolvedSeat, seats
from .servicectl import (  # noqa: F401  (the process control, reachable as service.* too)
    acquire_lock, alive, heartbeat_path, locked, read_heartbeat, request, stale, start, stop, write_heartbeat,
)
from .ship import Ship, ShipError
from .tmux import Tmux, TmuxError

TICK = 1.0
TYPING_QUIET = 30  # seconds since a client's last key or mouse input before its session may be switched
STATUS_INTERVAL = 0.7  # presentation only; rendering age and owner are checked independently

# Door alarms only a person can clear: a sign-in (every adapter names that dialog "login"),
# or a delivery whose paste or submit outcome is uncertain (the pending journal's own reasons).
# These go to the desk as well as the systems seat; every other alarm goes to the systems
# seat alone while one is up. With no systems seat up, every alarm goes to the desk.
PERSON_ONLY = (
    "login",
    "paste/submit outcome uncertain",
    "pending delivery",
    "pending box changed",
    "pending folded box",
    "prior launch has an unconfirmed delivery",
)


def person_only(reason: str) -> bool:
    return reason.startswith(PERSON_ONLY)
BLIND_INTERVAL = 60.0  # how often the blind attempts are read against the records


class Service:
    def __init__(self, ship: Ship, tmux: Tmux):
        self.ship, self.tmux = ship, tmux
        self.runtime = ship.runtime
        self.store = Store(self.runtime)
        self.attempts = Attempts(self.runtime)
        self.log_path = self.runtime / "log" / "service.jsonl"
        self.withheld: dict[tuple[str, str], str] = {}  # (message, seat) -> the screen it was withheld on
        self.told: dict[str, str | None] = {}  # seat -> the obstruction the desk was last told of
        self.alarmed: dict[str, tuple[str, set[str]]] = {}  # seat -> its obstruction and who has been told of it
        self.status_framework = presentation.framework_stamp()
        self.brief_stamp = self._brief_stamp()
        copy = install.installed(self.runtime) if install.running_from(self.runtime / 'venv' / 'bin') else None
        self.framework_commit = copy['commit'] if copy else None
        self.ledger = Ledger(self.runtime / "ledger.sqlite")
        self.meters: dict[str, machine.Meter] = {}
        self.usage_read: dict[str, float] = {}
        self.usage_hold: dict[str, float] = {}  # a refused pool's Retry-After, by the monotonic clock
        self.usage_logged: dict[str, str] = {}  # each pool's last logged usage failure
        self.read_marks: dict[Path, tuple] = {}  # record -> the fingerprint it was last read at
        self.working: dict[str, bool] = {}  # seat -> whether its record last showed a turn open
        self.work_marks: dict[str, tuple] = {}  # seat -> (record, fingerprint) its working state was read at
        self.blind_checked = -BLIND_INTERVAL  # when the blind attempts were last read against the records
        self.next_status = 0.0
        self.status_checked: float | None = None

    def run(self) -> None:
        self.lock = acquire_lock(self.runtime)  # one service per ship, for its whole life
        self.log("start", pid=os.getpid())
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        while True:
            self.tick()
            until = time.monotonic() + TICK
            while time.monotonic() < until:
                self.status_due()
                remaining = until - time.monotonic()
                if remaining > 0:
                    time.sleep(min(remaining, max(0.001, self.next_status - time.monotonic())))

    def tick(self) -> None:
        """One pass: the ship file read afresh, then every duty, each failing alone."""
        try:
            self.ship = shipmod.load(self.ship.folder)  # law: ship-file
        except ShipError as error:
            self.log("error", step="load", error=str(error))
        for step in (self.reconcile, self.account, self.inboxes, self.recoveries, self.deliveries, self.follow, self.requests, self.compactions, self.fresh_after_order,
                     self.rebrief, self.credentials, self.failover):  # accounting before anything relaunches a seat
            self.heartbeat()  # before every step: a slow delivery or usage read reads as busy, never as dead
            try:
                step()
            except Exception as error:  # one failing step never stops the others
                self.log("error", step=step.__name__, error=repr(error), trace=traceback.format_exc())
            self.status_due()

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

    def heartbeat(self) -> None:
        write_heartbeat(self.runtime)

    def log(self, event: str, **data) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a") as f:
            f.write(json.dumps({"at": datetime.now().astimezone().isoformat(timespec="seconds"), "event": event, **data}) + "\n")

    def seat(self, name: str) -> Seat:
        return Seat(self.ship, self.ship.seat(name), self.tmux)

    def hold_selection(self, error: SelectionError) -> None:
        reason = str(error)
        if self.told.get(error.seat) == reason:
            return
        self.told[error.seat] = reason
        self.log('held', seat=error.seat, error=reason)
        for desk in self.ship.seats.values():
            if desk.role == 'desk':
                self.store.new('report', 'ship', desk.name, f'{reason}; seat held, restore its declared selection')

    def resolved_seats(self) -> list[Seat]:
        found = []
        for seat in seats(self.ship, self.tmux, False):
            if isinstance(seat, UnresolvedSeat):
                self.hold_selection(seat.error)
            else:
                found.append(seat)
        return found


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
        latest = self.attempts.latest(attempts=records)
        queued = {message for message, result in latest.items() if result == 'queued'}
        blind = {message for message, result in latest.items() if result == 'blind'}
        for attempt in records:
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

    def deliveries(self) -> None:
        results = self.attempts.latest()
        for seat in self.resolved_seats():
            if seat.session.human:
                if recovery.held(seat):
                    self.needs_human(seat)
                continue
            pending = [m for m in self.attempts.undelivered(self.store, seat.name, results=results) if self._due(m, seat)]
            if pending:
                if not seat.is_up:
                    continue
                self.deliver(seat, pending)
            self.needs_human(seat)

    def recoveries(self) -> None:
        recovery.run(self)

    def _due(self, message: post.Message, seat: Seat) -> bool:
        """A withheld message is tried again when the screen has changed since, not on a clock (law: events-not-clocks)."""
        last = self.withheld.get((message.id, seat.name))
        if last is None:
            return True
        screen = Door(seat).screen()
        return screen is None or screen.text != last  # law: events-not-clocks

    def needs_human(self, seat: Seat) -> None:
        """When a seat's door needs a human, each of the alarm's recipients is told once, with a report from ``ship``.

        Once is per addressee and per reason: a repeated reading tells no one again, a changed
        reason tells again, and a recipient added while the reason stands is told it then.
        """
        _, reason = Door(seat).human_obstruction()
        reason = reason or next(iter(pending_delivery.detached(seat)), None)
        if reason != self.told.get(seat.name):
            self.told[seat.name] = reason
        if not reason:
            self.alarmed.pop(seat.name, None)
            return
        told = self.alarmed.get(seat.name)
        if told is None or told[0] != reason:
            told = self.alarmed[seat.name] = (reason, set())
        for name in self.alarm_recipients(seat, reason):
            if name not in told[1]:
                told[1].add(name)
                self.store.new("report", "ship", name, f"{seat.name} obstructed, {reason}")

    def alarm_recipients(self, seat: Seat, reason: str) -> list[str]:
        """The systems seats that are up, and the desk too for what only a person can clear.

        The obstructed seat is never its own recipient: its door is the one that cannot take the report.
        """
        desks = [spec.name for spec in self.ship.seats.values() if spec.role == "desk"]
        systems = [spec.name for spec in self.ship.seats.values()
                   if spec.role == "systems" and spec.name != seat.name and self._up(spec.name)]
        if not systems:
            return desks
        return (systems + [name for name in desks if name not in systems]) if person_only(reason) else systems

    def _up(self, name: str) -> bool:
        try:
            return self.seat(name).is_up
        except ShipError:
            return False  # an unresolved or unconfirmed launch is not a seat that can take the alarm

    def deliver(self, seat: Seat, messages: list[post.Message], resend: bool = False) -> str:
        if resend:
            from . import paste_refusal
            if len(messages)==1 and (reason:=paste_refusal.refused(seat,post.typed(messages),Door(seat).screen())):
                return f'{messages[0].short} to {seat.name} · withheld: {reason}'
            pending_delivery.explicit_resend(self, seat, messages)
        latest = self.attempts.last_results(seat.name)
        ordered = sorted(messages, key=lambda message: latest.get(message.id) == "withheld")
        lines = []
        for message in ordered:
            lines.append(self._deliver(seat, message, resend))
            reading = Door(seat).reading()
            if not reading.open and reading.clears != "dismissable":
                break  # an occupied or uncertain door holds every remaining item
        return "\n".join(lines)

    def _deliver(self, seat: Seat, message: post.Message, resend: bool = False) -> str:
        """One isolated transport, journaled before typing."""
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
            launched = seat.launched()
            attempt = self.attempts.open(message, seat.name, launched.pane, launched.pid, launched.at)
            text = post.typed([message])
            proof = pending_delivery.Proof.begin(seat, attempt, text)
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

    def inboxes(self) -> None:
        for name in self.ship.seats:
            path = self.runtime / "post" / "inbox" / f"{name}.md"
            text = post.inbox_text(self.store, name)
            if not path.exists() or path.read_text() != text:
                post.render_inbox(self.runtime, self.store, name)


    def requests(self) -> None:
        """Each request answered alone, in name order: none keeps a later one waiting.

        A rebrief persists its intent and posts each notice once, so one that fails stays to be tried
        again; its error is raised once every other request has had its answer, and the tick logs it.
        """
        folder = self.runtime / "requests"
        retry = []
        for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
            data = None
            try:
                claim = path.with_suffix(".claimed")
                if claim.exists() or claim.is_symlink():
                    continue  # an earlier action may have run; its raw durable claim needs inspection, never replay
                raw = path.read_text()
                data = json.loads(raw)
                if not isinstance(data, dict) or data.get("kind") != "rebrief":
                    durable.replace_file(claim, raw)  # before dispatch: failed archival cannot requeue an executed action
                try:
                    answer = self.answer(data)
                except SelectionError as error:
                    self.hold_selection(error)
                    self.clear_request_claim(claim)
                    continue  # this request stays held; later healthy-seat requests still run
                except ShipError as error:
                    if isinstance(data, dict) and data.get("kind") == "rebrief":
                        raise  # an immutable notice conflict must retain the rebrief's durable baseline/notices
                    answer = f"refused: {error}"  # a refusal is an answer: its asker reads why
                if answer is None:
                    self.clear_request_claim(claim)
                    continue  # not yet: the request waits for the seat's turn to end
                # The final name is the client's completion signal, never an open writer.
                durable.replace_file(path.with_suffix(".done"), answer + "\n")
                self.log("request", **data, answer=answer)
                path.unlink()
                durable.sync_folder(folder)  # retire the active name durably before removing its replay guard
                self.clear_request_claim(claim)
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
        durable.sync_folder(claim.parent)

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
            durable.sync_folder(kept.parent)
            durable.sync_folder(path.parent)
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
            if seat.session.human:  # the Office is reached by its watch alone: the message is offered to it again
                self.attempts.close(self.attempts.open(message, seat.name, None, None, None), "resend", "re-offered to the watch")
                return f"{message.short} re-offered to {seat.name}'s watch; " + ("it prints it now" if post.watch_armed(self.runtime, seat.name) else "no watch is armed yet")
            if not seat.is_up:
                return f"{message.short}: {seat.name} is down; it is delivered at qd up"
            return self.deliver(seat, [message], resend=True)
        if kind == "control":
            return self.control(self.seat(data["seat"]), data["name"], str(data["value"]))
        if kind == "relaunch":
            return self.relaunch(self.seat(data["seat"]), bool(data["resume"]))
        if kind == "select":  # the seats failover acts on: a seat whose selection is unknown is held, not guessed
            seat = next((s for s in self.resolved_seats() if s.name == data["seat"]), None)
            if seat is None:
                return f"{data['seat']} not moved: its pool selection is unknown or held; inspect with qd status"
            return self.select(seat, str(data["pool"]))
        return f"unknown request {kind!r}"

    def relaunch(self, seat: Seat, resume: bool) -> str | None:
        """Explicit recovery alone may interrupt a proven question-only wait."""
        if recovery.held(seat):
            return None
        pane = seat.pane()
        if pane is not None and not pane.dead:
            joined = seat.join()
            if joined.record is None:
                return None  # an idle screen cannot certify absent/unknown execution
            try:
                state = seat.harness.state(joined.record)
            except OSError:
                return None
            if not seat.harness.waiting_for_human(state):
                if state.turn_open is not False or state.pending_tools != ():
                    return None
                if not any(turn.timed and turn.started is not None and turn.ended is not None
                           and state.last_input_at is not None
                           and turn.started <= state.last_input_at <= turn.ended for turn in state.completed_turns):
                    return None  # empty/header-only and inferred idle are not recorded completion
        return seat.restart() if resume else seat.fresh()

    def control(self, seat: Seat, name: str, value: str) -> str | None:
        """Apply a control by its in-session form, through the door, when the seat is idle."""
        if reason := recovery.held(seat):
            return f'{name} held: {reason}'
        if seat.spec.pool_choices and (name in {"harness", "model", "effort"} or name not in shipmod.SEAT_KEYS):
            return f"{name} not applied: inline pool choices require an explicit edit in ship.toml; queued program controls have no choice scope"
        control = seat.harness.control(name)
        if control is None or control.session is None:
            return f"{name} written; qd restart {seat.name} applies it"
        if not seat.is_up:
            return f"{name} written; {seat.name} is down, so qd up {seat.name} applies it"
        if seat.is_working():
            return None  # at the turn's end
        door = Door(seat)
        command = control.command(value)
        delivery = door.deliver(command, confirm=False)
        if delivery.result != "typed":
            return f"{name}: {delivery}"
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
                return f"{name} = {value} · applied by {command} in {seat.pane().id} · confirmed by {control.confirmed_by}"
            time.sleep(0.2)
        return f"{name} = {value} · typed {command} in {seat.pane().id} · not confirmed by the {control.confirmed_by} yet"


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
        from . import compaction
        selected = {spec.name for spec in self.ship.seats.values()
                    if spec.fresh_after_compactions or compaction.path(self.runtime, spec.name).exists()}
        if not selected:
            return
        for seat in self.resolved_seats():
            if seat.name not in selected:
                continue
            try:
                compaction.tick(seat)
            except (OSError, ShipError) as error:
                self.log('error', step='compactions', seat=seat.name, error=repr(error))

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
        paths = [standing.FRAMEWORK_DIR / "laws.md", self.ship.folder / "orders.md"]
        for folder in (standing.FRAMEWORK_DIR, self.ship.folder):
            paths += sorted((folder / "roles").glob("*.md")) + sorted((folder / "harnesses").glob("*.md"))
        return tuple((str(p), p.stat().st_mtime) for p in paths if p.exists())

    def credentials(self) -> None:
        """A seat whose credential link became a file has refreshed the token: reseed the pool and relink."""
        for line in credential.reseed(self.ship, self.resolved_seats()):
            self.log("credential", proof=line)


    def account(self) -> None:
        """Ingest every ended turn of every record in each seat's home, sample a running seat's machine use,
        and refresh its pool usage at the provider cadence. A record a relaunch left behind is still accounted
        (law: every-turn-accounted); one whose file has not moved is not read again."""
        table = machine.processes()
        for seat in self.resolved_seats():
            pane, launched = seat.pane(), seat.launched()
            live = pane is not None and not pane.dead
            current = seat.record()
            for changed in self.ledger.provenance.sync(seat, current, launched):
                self.read_marks.pop(changed, None)
            meter = self.meters.get(seat.name)
            if live:
                if meter is None or meter.root != pane.pid:
                    meter = self.meters[seat.name] = machine.Meter(pane.pid)  # a new process starts a new meter
                meter.sample(table)
            for owned in record_owner.records(seat):  # law: every-turn-accounted
                record = owned.path
                pool = launched.pool if (record == current and launched) else UNKNOWN
                try:
                    stamp = (owned.name, record_fingerprint(record))
                    if self.read_marks.get(record) == stamp:
                        continue
                    state, turns = owned.read()
                except OSError as error:
                    self.ledger.mark_record(record, seat.name, pool, str(error))
                    self.log("error", step="account", seat=seat.name, record=record.name, error=str(error))
                    continue
                self.ledger.quota.record(record, seat.name, state.walls, self.ledger.provenance, post.now())
                self.read_marks[record] = stamp
                incomplete = "incomplete turn state in record" if (state.turns is None or state.turn_open is None) else None
                self.ledger.mark_record(record, seat.name, pool, incomplete)
                if turns:
                    qualify = getattr(owned.reader, "price_turn", lambda turn, price: turn)
                    turns = [qualify(t, self.ledger.provenance.price(record, t.started, t.ended)) for t in turns]
                    figures = {turns[-1].index: meter.result()} if live and record == current and meter and state.turn_open is False else {}
                    written = self.ledger.ingest(seat.name, UNKNOWN, record, turns, self.store, figures)
                    if written:
                        if live and record == current:
                            self.meters[seat.name] = machine.Meter(pane.pid)
                        self.log("ledger", seat=seat.name, record=record.name, turns=written)
            if live and seat.harness.screen_walls:
                quota.observe_screen(self.ledger, seat, Door(seat).screen(), launched)  # the one place a screen's wall is written
            quota.refresh(self, seat, live, launched)
        quota.refresh_declared(self)
        self.ledger.attributions.replay(self.store, self.runtime)

    def failover(self) -> None:
        """At a wall, try eligible later choices first, then earlier choices in declared order."""
        for seat in self.resolved_seats():
            if recovery.held(seat) or len(seat.spec.pools) < 2 or not seat.is_up or not quota.current_walled(seat, self.ledger):
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
        """Why a seat may not move now: a move needs no open turn, no queued input and a recorded completion."""
        try:
            record = seat.record()
            state = seat.harness.state(record) if record is not None else None
        except OSError:
            return "its record is unreadable"
        if state is None:
            return "it has no joined record"
        if state.turn_open is not False:
            return "a turn is open"
        if state.queue_error:
            return f"queue unknown: {state.queue_error}"
        if state.queued:
            return "input is queued"
        if not any(turn.timed and turn.started is not None and turn.ended is not None
                   and state.last_input_at is not None
                   and turn.started <= state.last_input_at <= turn.ended for turn in state.completed_turns):
            return "its last input has no recorded completion"
        return None

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
            usage = reader.usage(pool)
            if usage is None or usage.error:
                from .harness.limits import Usage
                usage = usage or Usage((), post.now(), "provider usage unavailable")
                quota.record_read(self, pool_name, usage, reader.usage_interval, post.now())
                self.log("failover", seat=seat.name, pool=pool_name, skipped=f"its usage could not be read: {usage.error}")
                return False
            quota.record_read(self, pool_name, usage, reader.usage_interval, post.now())
        return quota.cached_open(self.ship, self.ledger, pool_name, seat)

    def rebrief(self) -> None:
        """When the orders or a card change, every brief is rendered again and each seat is told."""
        if self.framework_commit and (self.runtime / 'requests' / f'upgrade-rebrief-{self.framework_commit}.json').exists():
            return  # the durable upgrade publisher owns these edits until its request completes
        stamp = self._brief_stamp()
        if stamp == self.brief_stamp:
            return
        selected = self.resolved_seats()
        for seat in selected:
            path = self.runtime / 'brief' / f'{seat.name}.md'
            before = path.read_text() if path.exists() else None
            standing.render(self.ship, seat.spec)
            if seat.is_up and path.read_text() != before:
                self.store.new("report", "ship", seat.name, f"ship's orders changed · read runtime/brief/{seat.name}.md")
        if len(selected) == len(self.ship.seats):
            self.brief_stamp = stamp
        self.log("rebrief", seats=[seat.name for seat in selected])

    def upgrade_rebrief(self, data: dict) -> str | None:
        """A durable baseline survives rendering and retries; posting is not proof of reading."""
        stamp = self._brief_stamp()
        affected, changed = [], []
        selected = self.resolved_seats()
        for seat in selected:
            launch = seat.folder / 'launch.json'
            same = (data['launches'].get(seat.name) is not None
                    and install.launch_stamp(launch) == data['launches'][seat.name] and seat.is_up)
            if same and data['before'].get(seat.name) is None:
                return None  # no known prior brief to compare; never overwrite that unknown baseline
            text = standing.brief(self.ship, seat.spec).text
            digest = hashlib.sha256(text.encode()).hexdigest()
            notice = self.upgrade_notice(data, seat.name, digest) if same else None
            standing.render(self.ship, seat.spec, text=text)
            if notice is not None or (not same and digest != data['before'].get(seat.name)):
                changed.append(seat.name)
            if notice is not None:
                target = self.store.dir / f'{notice.id}.json'
                if target.exists():
                    if post.Message.from_json(target.read_text()) != notice:
                        raise ShipError('upgrade brief notice identity conflicts with an immutable post')
                else:
                    self.store.write(notice)
                affected.append(seat.name)
        if len(selected) != len(self.ship.seats):
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
                    raise ShipError('upgrade brief notice identity conflicts with an immutable post')
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
