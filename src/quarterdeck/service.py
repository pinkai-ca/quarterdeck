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
from . import credential, install, machine, post, quota, record_owner, ship as shipmod, standing
from .door import CONFIRM_SECONDS, Door
from .harness.base import record_fingerprint
from .ledger import Ledger
from .provenance import UNKNOWN
from .post import Attempts, Store
from .seat import Seat, seats
from .servicectl import (  # noqa: F401  (the process control, reachable as service.* too)
    acquire_lock, alive, heartbeat_path, locked, read_heartbeat, request, stale, start, stop, write_heartbeat,
)
from .ship import Ship, ShipError
from .tmux import Tmux

TICK = 1.0
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
        self.brief_stamp = self._brief_stamp()
        copy = install.installed(self.runtime) if install.running_from(self.runtime / 'venv' / 'bin') else None
        self.framework_commit = copy['commit'] if copy else None
        self.ledger = Ledger(self.runtime / "ledger.sqlite")
        self.meters: dict[str, machine.Meter] = {}
        self.usage_read: dict[str, float] = {}
        self.read_marks: dict[Path, tuple] = {}  # record -> the fingerprint it was last read at
        self.blind_checked = -BLIND_INTERVAL  # when the blind attempts were last read against the records

    def run(self) -> None:
        self.lock = acquire_lock(self.runtime)  # one service per ship, for its whole life
        self.log("start", pid=os.getpid())
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        while True:
            self.tick()
            time.sleep(TICK)

    def tick(self) -> None:
        """One pass: the ship file read afresh, then every duty, each failing alone."""
        try:
            self.ship = shipmod.load(self.ship.folder)  # law: ship-file
        except ShipError as error:
            self.log("error", step="load", error=str(error))
        for step in (self.reconcile, self.account, self.inboxes, self.deliveries, self.requests, self.fresh_after_order,
                     self.rebrief, self.credentials, self.failover):  # accounting before anything relaunches a seat
            self.heartbeat()  # before every step: a slow delivery or usage read reads as busy, never as dead
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


    def reconcile(self) -> None:
        """Reconcile journals first, then unjournaled attempts through the same fresh receipt rule.

        Live owners keep their confirm window; queued inputs need consumption.
        Blind attempts are re-read at their existing minute cadence, never resent.
        """
        tracked = pending_delivery.reconcile(self)
        retry_blind = time.monotonic() - self.blind_checked >= BLIND_INTERVAL
        if retry_blind:
            self.blind_checked = time.monotonic()
        queued, blind = self.attempts.queued_ids(), self.attempts.blind_ids()
        for attempt in self.attempts.all():
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
            seat, message = self.seat(attempt.seat), self.store.get(attempt.message)
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
        return screen is None or screen.text != last  # law: events-not-clocks

    def needs_human(self, seat: Seat) -> None:
        """When a seat's door needs a human, the desk is told once, with a report from ``ship``."""
        reading = Door(seat).reading()
        reason = reading.reason if (not reading.open and reading.clears == "needs a human") else None
        reason = reason or next(iter(pending_delivery.detached(seat)), None)
        if reason == self.told.get(seat.name):
            return
        self.told[seat.name] = reason
        if reason:
            for desk in self.ship.seats.values():
                if desk.role == "desk":
                    self.store.new("report", "ship", desk.name, f"{seat.name} obstructed, {reason}")

    def deliver(self, seat: Seat, messages: list[post.Message], resend: bool = False) -> str:
        if resend:
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
        if kind == 'rebrief':
            copy = install.installed(self.runtime)
            if copy is None or copy['commit'] != data['commit']:
                return json.dumps(dict(token=data['token'], status='superseded'))
            if self.framework_commit != data['commit']:
                return None  # only the requested installed/running revision may render and announce it
            return self.upgrade_rebrief(data)
        if kind == "stop":
            return Door(self.seat(data["seat"])).interrupt()
        if kind == "resend":
            message = self.store.get(data["message"])
            seat = self.seat(message.to)
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
        return f"unknown request {kind!r}"

    def relaunch(self, seat: Seat, resume: bool) -> str | None:
        """Restart or fresh at the turn's end, so nothing is relaunched under a running tool call."""
        if seat.is_working():
            return None
        return seat.restart() if resume else seat.fresh()

    def control(self, seat: Seat, name: str, value: str) -> str | None:
        """Apply a control by its in-session form, through the door, when the seat is idle."""
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


    def fresh_after_order(self) -> None:
        """A worker whose last open order closed, and whose closing turn ended, starts fresh."""
        for seat in seats(self.ship, self.tmux):
            if seat.spec.fresh != "after-order" or not seat.is_up or self.store.open_for(seat.name):
                continue
            launched = seat.launched()
            if launched is None:
                continue
            closed = max((report.at for order in self.store.for_seat(seat.name)
                          if order.kind == "order" and (report := self.store.closing_report(order))
                          and report.at > launched.when), default=None)
            if closed is None or (record := seat.record()) is None:
                continue
            try:
                state = seat.harness.state(record)
            except OSError:
                continue  # no screen or default idle reading can prove a recorded closing turn
            if (state.turn_open is False and not state.queued
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
        for line in credential.reseed(self.ship, seats(self.ship, self.tmux)):
            self.log("credential", proof=line)


    def account(self) -> None:
        """Ingest every ended turn of every record in each seat's home, sample a running seat's machine use,
        and refresh its pool usage at the provider cadence. A record a relaunch left behind is still accounted
        (law: every-turn-accounted); one whose file has not moved is not read again."""
        table = machine.processes()
        for seat in seats(self.ship, self.tmux):
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
        """A seat whose pool is walled moves on at its turn's end to the next pool of its list that is not, and stays."""
        for seat in seats(self.ship, self.tmux):
            if len(seat.spec.pools) < 2 or not seat.is_up or not self.ledger.walled(seat.pool.name, self.ship):
                continue
            try:
                record = seat.record()
                state = seat.harness.state(record) if record is not None else None
            except OSError:
                continue
            if state is None or state.turn_open is not False or state.queued:
                continue
            pools = list(seat.spec.pools)
            start = pools.index(seat.pool.name)
            ordered = pools[start + 1:]
            target = next((p for p in ordered if self.known_open(seat, p)), None)
            if target is None:
                continue  # every pool is walled or unread: a fact the status line shows, not a move
            came_from = seat.pool.name
            proof = seat.failover_to(target)
            self.store.new("report", post.SHIP, seat.name, f"moved to pool {target} from {came_from} at its wall")
            self.log("failover", seat=seat.name, pool=target, came_from=came_from, proof=proof)

    def known_open(self, seat: Seat, pool_name: str) -> bool:
        """A destination pool, judged by its own adapter's existing usage rules.

        A background provider uses its paced cache; other unread pools are read before the move; a program that reads no usage
        has only the Ledger's walls to go by, and the move says so."""
        pool = self.ship.pools[pool_name]
        if pool.kind in ("key", "endpoint"):  # no usage windows to read: a key is measured by spend, an endpoint has no quota
            return not self.ledger.walled(pool_name, self.ship)
        reader = harnesses.get(pool.provider)
        if reader.background_usage:
            return self.ledger.usage_known(pool_name) and not self.ledger.walled(pool_name, self.ship)
        if not reader.reads_usage:
            return not self.ledger.walled(pool_name, self.ship)
        if not self.ledger.usage_known(pool_name):
            if not self.ledger.quota.due(pool_name, post.now(), interval=reader.usage_interval):
                return False  # retained failures retry by attempt time, including after a restart
            usage = reader.usage(pool)
            if usage is None:
                from .harness.limits import Usage
                self.ledger.record_usage(pool_name, Usage((), post.now(), "provider usage unavailable"))
                self.log("failover", seat=seat.name, pool=pool_name, skipped="its usage could not be read")
                return False
            self.ledger.record_usage(pool_name, usage, attempted_at=post.now())
        return self.ledger.usage_known(pool_name) and not self.ledger.walled(pool_name, self.ship)

    def rebrief(self) -> None:
        """When the orders or a card change, every brief is rendered again and each seat is told."""
        if self.framework_commit and (self.runtime / 'requests' / f'upgrade-rebrief-{self.framework_commit}.json').exists():
            return  # the durable upgrade publisher owns these edits until its request completes
        stamp = self._brief_stamp()
        if stamp == self.brief_stamp:
            return
        self.brief_stamp = stamp
        for seat in seats(self.ship, self.tmux):
            path = self.runtime / 'brief' / f'{seat.name}.md'
            before = path.read_text() if path.exists() else None
            standing.render(self.ship, seat.spec)
            if seat.is_up and path.read_text() != before:
                self.store.new("report", "ship", seat.name, f"ship's orders changed · read runtime/brief/{seat.name}.md")
        self.log("rebrief", seats=list(self.ship.seats))

    def upgrade_rebrief(self, data: dict) -> str | None:
        """A durable baseline survives rendering and retries; posting is not proof of reading."""
        stamp = self._brief_stamp()
        affected, changed = [], []
        for seat in seats(self.ship, self.tmux):
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
