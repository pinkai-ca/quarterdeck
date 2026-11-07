"""The Service: the ship's one long-running process, not a seat.

Started by ``qd up`` and owned by whoever ran it, it renders inboxes, delivers messages
through the doors, writes attempts, answers requests (controls, stops, resends), starts
workers fresh after their orders, re-briefs seats when the orders change, and writes the
heartbeat the status line reads. Every tick is one pass; nothing waits on a clock longer
than the tick (law: events-not-clocks).
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from . import credential, machine, post, ship as shipmod, standing
from .door import CONFIRM_SECONDS, Door
from .ledger import Ledger
from .post import Attempts, Store
from .seat import Seat, seats
from .servicectl import (  # noqa: F401  (the process control, reachable as service.* too)
    acquire_lock, alive, heartbeat_path, locked, read_heartbeat, request, running, start, stop, write_heartbeat,
)
from .ship import Ship, ShipError
from .tmux import Tmux

TICK = 1.0
USAGE_SECONDS = 60.0
SPEEDS = {None: 0, "now": 1, "interrupt": 2}


class Service:
    def __init__(self, ship: Ship, tmux: Tmux):
        self.ship, self.tmux = ship, tmux
        self.runtime = ship.runtime
        self.store = Store(self.runtime)
        self.attempts = Attempts(self.runtime)
        self.log_path = self.runtime / "log" / "service.jsonl"
        self.withheld: dict[tuple[str, str], str] = {}  # (message, seat) -> the screen it was withheld on
        self.told: dict[str, str | None] = {}  # seat -> the obstruction the desk was last told of
        self.brief_stamp = self._brief_stamp()
        self.ledger = Ledger(self.runtime / "ledger.sqlite")
        self.meters: dict[str, machine.Meter] = {}
        self.usage_read: dict[str, float] = {}

    # -- the loop ------------------------------------------------------------------------

    def run(self) -> None:
        self.lock = acquire_lock(self.runtime)  # one service per ship, for its whole life
        self.log("start", pid=os.getpid())
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        while True:
            self.tick()
            time.sleep(TICK)

    def tick(self) -> None:
        """One pass: the ship file read afresh, then every duty, each failing alone."""
        self.heartbeat()
        try:
            self.ship = shipmod.load(self.ship.folder)  # law: ship-file
        except ShipError as error:
            self.log("error", step="load", error=str(error))
        for step in (self.reconcile, self.inboxes, self.deliveries, self.requests, self.fresh_after_order,
                     self.rebrief, self.credentials, self.account, self.failover):
            try:
                step()
            except Exception as error:  # one failing step never stops the others
                self.log("error", step=step.__name__, error=repr(error), trace=traceback.format_exc())

    def heartbeat(self) -> None:
        write_heartbeat(self.runtime)

    def log(self, event: str, **data) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a") as f:
            f.write(json.dumps({"at": datetime.now().astimezone().isoformat(timespec="seconds"), "event": event, **data}) + "\n")

    def seat(self, name: str) -> Seat:
        return Seat(self.ship, self.ship.seat(name), self.tmux)

    # -- deliveries ----------------------------------------------------------------------

    def reconcile(self) -> None:
        """Attempts left without a result: the record says delivered, else blind, never resent.

        The program is given the door's confirm window from the attempt's time before it is
        judged, since a message typed a moment before the crash may still be on its way in.
        """
        for attempt in self.attempts.unfinished():
            message = self.store.get(attempt.message)
            if attempt.pane is None:
                self.attempts.close(attempt, "blind", "a watch opened it and never reported printing it; qd post resend")
                continue
            seat = self.seat(attempt.seat)
            deadline = attempt.when.timestamp() + CONFIRM_SECONDS
            while not (seen := self.taken(seat, message, attempt.when)) and time.time() < deadline:
                time.sleep(0.2)
            result = "delivered" if seen else "blind"
            self.attempts.close(attempt, result, "found in the record on reconciliation" if seen else
                                "the service died mid-delivery and the record does not show it; qd post resend")  # law: one-door
            self.log("reconciled", message=message.id, seat=seat.name, result=result)

    @staticmethod
    def taken(seat: Seat, message: post.Message, since: datetime) -> bool:
        """Whether the record shows this very message, by its id and body, as an input after ``since``."""
        record = seat.record()
        if record is None:
            return False
        state, turns = seat.harness.read(record)
        wanted = " ".join(message.body.split())

        def carries(text: str | None) -> bool:
            flat = " ".join((text or "").split())
            return message.short in flat and wanted in flat

        inputs = [t.input for t in turns if t.started and t.started >= since]
        if state.turn_open and state.last_input_at and state.last_input_at >= since:
            inputs.append(state.last_input)
        return any(carries(text) for text in inputs + list(state.queued) + list(state.consumed))

    def deliveries(self) -> None:
        for seat in seats(self.ship, self.tmux):
            if seat.session.human or not seat.is_up:
                continue
            pending = [m for m in self.attempts.undelivered(self.store, seat.name) if self._due(m, seat)]
            if pending:
                self.deliver(seat, pending)
            self.needs_human(seat)

    def _due(self, message: post.Message, seat: Seat) -> bool:
        """A withheld message is tried again when the screen has changed since, not on a clock (law: events-not-clocks)."""
        last = self.withheld.get((message.id, seat.name))
        if last is None:
            return True
        screen = Door(seat).screen()
        return screen is None or screen.text != last

    def needs_human(self, seat: Seat) -> None:
        """When a seat's door needs a human, the desk is told once, with a report from ``ship``."""
        reading = Door(seat).reading()
        reason = reading.reason if (not reading.open and reading.clears == "needs a human") else None
        if reason == self.told.get(seat.name):
            return
        self.told[seat.name] = reason
        if reason:
            for desk in self.ship.seats.values():
                if desk.role == "desk":
                    self.store.new("report", "ship", desk.name, f"{seat.name} obstructed, {reason}")

    def deliver(self, seat: Seat, messages: list[post.Message], resend: bool = False) -> str:
        """One paste for all that waits for a seat, one attempt per message, written before typing."""
        door = Door(seat)
        reading = door.reading(door.clear_known())  # a notice the adapter knows never holds a delivery
        if not reading.open:
            return f"{seat.name}: {reading}"
        launched = seat.launched()
        speed = max((m.speed for m in messages), key=lambda s: SPEEDS[s], default=None)
        attempts = [self.attempts.open(m, seat.name, launched.pane, launched.pid, launched.at) for m in messages]
        delivery = door.deliver(post.typed(messages), speed)
        detail = delivery.detail + (" · resend" if resend else "")
        screen = door.screen()
        for attempt in attempts:
            self.attempts.close(attempt, delivery.result, detail)
            if delivery.result == "withheld":
                self.withheld[(attempt.message, seat.name)] = screen.text if screen else ""
        self.log("delivery", seat=seat.name, messages=[m.id for m in messages], speed=speed, result=delivery.result,
                 detail=detail)
        return f"{', '.join(m.short for m in messages)} to {seat.name} · {delivery.result}: {detail}"

    def inboxes(self) -> None:
        for name in self.ship.seats:
            path = self.runtime / "post" / "inbox" / f"{name}.md"
            text = post.inbox_text(self.store, name)
            if not path.exists() or path.read_text() != text:
                post.render_inbox(self.runtime, self.store, name)

    # -- requests ------------------------------------------------------------------------

    def requests(self) -> None:
        folder = self.runtime / "requests"
        for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
            data = json.loads(path.read_text())
            answer = self.answer(data)
            if answer is None:
                continue  # not yet: the request waits for the seat's turn to end
            path.with_suffix(".done").write_text(answer + "\n")
            path.unlink()
            self.log("request", **data, answer=answer)

    def answer(self, data: dict) -> str | None:
        kind = data["kind"]
        if kind == "stop":
            return Door(self.seat(data["seat"])).interrupt()
        if kind == "resend":
            message = self.store.get(data["message"])
            seat = self.seat(message.to)
            if not seat.is_up:
                return f"{message.short}: {seat.name} is down; it is delivered at qd up"
            return self.deliver(seat, [message], resend=True)
        if kind == "control":
            return self.control(self.seat(data["seat"]), data["name"], str(data["value"]))
        if kind == "relaunch":
            return self.relaunch(self.seat(data["seat"]), bool(data["resume"]))
        return f"unknown request {kind!r}"

    def relaunch(self, seat: Seat, resume: bool) -> str | None:
        """Restart or fresh at the turn's end, so nothing is relaunched under a running tool call."""
        record = seat.record() if seat.is_up else None
        if record and seat.harness.state(record).turn_open:
            return None
        return seat.restart() if resume else seat.fresh()

    def control(self, seat: Seat, name: str, value: str) -> str | None:
        """Apply a control by its in-session form, through the door, when the seat is idle."""
        control = seat.harness.control(name)
        if control is None or control.session is None:
            return f"{name} written; qd restart {seat.name} applies it"
        if not seat.is_up:
            return f"{name} written; {seat.name} is down, so qd up {seat.name} applies it"
        door = Door(seat)
        screen = door.screen()
        if screen is not None and screen.working:
            return None
        command = control.command(value)
        delivery = door.deliver(command, confirm=False)
        if delivery.result != "typed":
            return f"{name}: {delivery}"
        time.sleep(0.5)
        door.clear_known()  # a program that asks "switch?" after the command is answered yes
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            record = seat.record()
            state = seat.harness.state(record) if record else None
            confirmed = seat.harness.confirms(control, value, door.screen(), state)
            if confirmed:
                return f"{name} = {value} · applied by {command} in {seat.pane().id} · confirmed by {control.confirmed_by}"
            time.sleep(0.2)
        return f"{name} = {value} · typed {command} in {seat.pane().id} · not confirmed by the {control.confirmed_by} yet"

    # -- fresh starts and briefs -----------------------------------------------------------

    def fresh_after_order(self) -> None:
        """A worker whose last open order closed, and whose closing turn ended, starts fresh."""
        for seat in seats(self.ship, self.tmux):
            if seat.spec.fresh != "after-order" or not seat.is_up or self.store.open_for(seat.name):
                continue
            launched = seat.launched()
            closed_since_launch = any(
                m.kind == "report" and m.sender == seat.name and m.re and m.at > launched.when
                for m in self.store.all()
            )
            record = seat.record()
            if closed_since_launch and record and not seat.harness.state(record).turn_open:
                proof = seat.fresh()  # law: context-is-cache
                self.log("fresh", seat=seat.name, proof=proof)

    def _brief_stamp(self) -> tuple:
        paths = [standing.FRAMEWORK_DIR / "laws.md", self.ship.folder / "orders.md"]
        for folder in (standing.FRAMEWORK_DIR, self.ship.folder):
            paths += sorted((folder / "roles").glob("*.md")) + sorted((folder / "harnesses").glob("*.md"))
        return tuple((str(p), p.stat().st_mtime) for p in paths if p.exists())

    def credentials(self) -> None:
        """A seat whose credential link became a file has refreshed the token: reseed the pool and relink."""
        for line in credential.reseed(self.ship, seats(self.ship, self.tmux)):
            self.log("credential", proof=line)

    # -- the ledger ------------------------------------------------------------------------

    def account(self) -> None:
        """Sample each running seat's machine use, ingest its ended turns, and read its pool's usage once a minute."""
        table = machine.processes()
        for seat in seats(self.ship, self.tmux):
            pane = seat.pane()
            record = seat.record() if pane else None
            if pane is None or pane.dead or record is None:
                continue
            state, turns = seat.harness.read(record)
            meter = self.meters.setdefault(seat.name, machine.Meter())
            meter.sample(pane.pid, table)
            if turns and self.ledger.ingested(record) < len(turns):
                figures = {turns[-1].index: meter.result()} if not state.turn_open else {}
                written = self.ledger.ingest(seat.name, seat.pool.name, record, turns, self.store, figures)
                if written:
                    self.meters[seat.name] = machine.Meter()
                    self.log("ledger", seat=seat.name, turns=written)
            pool = seat.pool
            if pool.kind == "subscription" and time.monotonic() - self.usage_read.get(pool.name, -USAGE_SECONDS) >= USAGE_SECONDS:
                self.usage_read[pool.name] = time.monotonic()
                usage = seat.harness.usage(pool)
                if usage:
                    self.ledger.record_usage(pool.name, usage)

    def failover(self) -> None:
        """A seat whose pool is walled moves on at its turn's end to the next pool of its list that is not, and stays."""
        for seat in seats(self.ship, self.tmux):
            if len(seat.spec.pools) < 2 or not seat.is_up or not self.ledger.walled(seat.pool.name):
                continue
            record = seat.record()
            if record and seat.harness.state(record).turn_open:
                continue
            pools = list(seat.spec.pools)
            start = pools.index(seat.pool.name)
            ordered = pools[start + 1:] + pools[:start]
            target = next((p for p in ordered if not self.ledger.walled(p)), None)
            if target is None:
                continue  # every pool is walled: a fact the status line shows, not a move
            came_from = seat.pool.name
            seat.move_to(target)
            proof = seat.restart()
            self.store.new("report", post.SHIP, seat.name, f"moved to pool {target} from {came_from} at its wall")
            self.log("failover", seat=seat.name, pool=target, came_from=came_from, proof=proof)

    def rebrief(self) -> None:
        """When the orders or a card change, every brief is rendered again and each seat is told."""
        stamp = self._brief_stamp()
        if stamp == self.brief_stamp:
            return
        self.brief_stamp = stamp
        for seat in seats(self.ship, self.tmux):
            standing.render(self.ship, seat.spec)
            if seat.is_up:
                self.store.new("report", "ship", seat.name, f"ship's orders changed · read runtime/brief/{seat.name}.md")
        self.log("rebrief", seats=list(self.ship.seats))
