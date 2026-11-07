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
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from . import post, standing
from .door import Door
from .post import Attempts, Store
from .seat import Seat, qd_bin, seats
from .ship import Ship, ShipError
from .tmux import Tmux

TICK = 1.0
STALE_SECONDS = 10.0
RETRY_SECONDS = 60.0
ANSWER_SECONDS = 8.0
SPEEDS = {None: 0, "now": 1, "interrupt": 2}


def heartbeat_path(runtime: Path) -> Path:
    return runtime / "service.heartbeat"


def read_heartbeat(runtime: Path) -> tuple[int, float] | None:
    """The service's pid and the age of its last tick in seconds, or None when none was written."""
    try:
        pid, stamp = heartbeat_path(runtime).read_text().split()
    except (FileNotFoundError, ValueError):
        return None
    age = datetime.now(timezone.utc).timestamp() - datetime.fromisoformat(stamp).timestamp()
    return int(pid), age


def running(pid: int) -> bool:
    """Whether a process is alive and not a zombie waiting to be reaped."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        return state != "Z"
    except (FileNotFoundError, IndexError):
        pass
    except OSError:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def alive(runtime: Path) -> bool:
    beat = read_heartbeat(runtime)
    return beat is not None and beat[1] <= STALE_SECONDS and running(beat[0])


def start(ship: Ship) -> str:
    """Start the service detached, logging under the runtime; a proof line."""
    if alive(ship.runtime):
        pid, _ = read_heartbeat(ship.runtime)
        return f"service alive · pid {pid}"
    log_dir = ship.runtime / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    out = (log_dir / "service.out").open("ab")
    process = subprocess.Popen(
        [str(qd_bin() / "qd"), "--ship", str(ship.folder), "service"],
        stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True, cwd=str(ship.folder),
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not alive(ship.runtime):
        time.sleep(0.1)
    if not alive(ship.runtime):
        raise ShipError(f"service did not write its heartbeat within 5s; see {log_dir / 'service.out'}")
    return f"service started · pid {process.pid} · heartbeat {heartbeat_path(ship.runtime).relative_to(ship.folder)}"


def stop(runtime: Path) -> str:
    beat = read_heartbeat(runtime)
    if beat is None or not alive(runtime):
        return "service was not running"
    os.kill(beat[0], signal.SIGTERM)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and running(beat[0]):
        time.sleep(0.05)
    heartbeat_path(runtime).unlink(missing_ok=True)
    return f"service stopped · pid {beat[0]}"


# -- requests: what the CLI asks the service to do, since only the service types ----------------


def request(runtime: Path, kind: str, **data) -> str:
    """File a request and wait for the service's answer; refuse when the service is down."""
    if not alive(runtime):
        raise ShipError("service down; qd up restarts it")
    folder = runtime / "requests"
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{datetime.now().strftime('%H%M%S')}-{kind}-{os.getpid()}"
    path = folder / f"{name}.json"
    path.write_text(json.dumps({"kind": kind, **data}) + "\n")
    answer = folder / f"{name}.done"
    deadline = time.monotonic() + ANSWER_SECONDS
    while time.monotonic() < deadline:
        if answer.exists():
            proof = answer.read_text().strip()
            answer.unlink()
            return proof
        time.sleep(0.1)
    return f"pending: the service has not answered within {ANSWER_SECONDS:.0f}s (request {name}); it applies when it can"


class Service:
    def __init__(self, ship: Ship, tmux: Tmux):
        self.ship, self.tmux = ship, tmux
        self.runtime = ship.runtime
        self.store = Store(self.runtime)
        self.attempts = Attempts(self.runtime)
        self.log_path = self.runtime / "log" / "service.jsonl"
        self.withheld: dict[tuple[str, str], float] = {}
        self.brief_stamp = self._brief_stamp()

    # -- the loop ------------------------------------------------------------------------

    def run(self) -> None:
        self.log("start", pid=os.getpid())
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        self.reconcile()
        while True:
            self.tick()
            time.sleep(TICK)

    def tick(self) -> None:
        self.heartbeat()
        for step in (self.inboxes, self.deliveries, self.requests, self.fresh_after_order, self.rebrief):
            try:
                step()
            except Exception as error:  # one failing step never stops the others
                self.log("error", step=step.__name__, error=repr(error), trace=traceback.format_exc())

    def heartbeat(self) -> None:
        path = heartbeat_path(self.runtime)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(f"{os.getpid()} {datetime.now(timezone.utc).isoformat()}\n")
        os.replace(tmp, path)

    def log(self, event: str, **data) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a") as f:
            f.write(json.dumps({"at": datetime.now().astimezone().isoformat(timespec="seconds"), "event": event, **data}) + "\n")

    def seat(self, name: str) -> Seat:
        return Seat(self.ship, self.ship.seat(name), self.tmux)

    # -- deliveries ----------------------------------------------------------------------

    def reconcile(self) -> None:
        """Attempts left without a result: the record says delivered, else blind, never resent."""
        for attempt in self.attempts.unfinished():
            message = self.store.get(attempt.message)
            seat = self.seat(attempt.seat)
            record = seat.record()
            state = seat.harness.state(record) if record else None
            wanted = " ".join(message.body.split())
            seen = state and ((state.last_input and wanted in " ".join(state.last_input.split()))
                              or any(wanted in " ".join(q.split()) for q in state.queued))
            result = "delivered" if seen else "blind"
            self.attempts.close(attempt, result, "found in the record on reconciliation" if seen else
                                "the service died mid-delivery and the record does not show it; qd post resend")  # law: one-door
            self.log("reconciled", message=message.id, seat=seat.name, result=result)

    def deliveries(self) -> None:
        for seat in seats(self.ship, self.tmux):
            if seat.session.human or not seat.is_up:
                continue
            pending = [m for m in self.attempts.undelivered(self.store, seat.name) if self._due(m, seat.name)]
            if pending:
                self.deliver(seat, pending)

    def _due(self, message: post.Message, seat: str) -> bool:
        return time.monotonic() - self.withheld.get((message.id, seat), -RETRY_SECONDS) >= RETRY_SECONDS

    def deliver(self, seat: Seat, messages: list[post.Message], resend: bool = False) -> str:
        """One paste for all that waits for a seat, one attempt per message, written before typing."""
        door = Door(seat)
        reading = door.reading()
        if not reading.open:
            return f"{seat.name}: {reading}"
        launched = seat.launched()
        speed = max((m.speed for m in messages), key=lambda s: SPEEDS[s], default=None)
        attempts = [self.attempts.open(m, seat.name, launched.pane, launched.pid, launched.at) for m in messages]
        delivery = door.deliver(post.typed(messages), speed)
        detail = delivery.detail + (" · resend" if resend else "")
        for attempt in attempts:
            self.attempts.close(attempt, delivery.result, detail)
            if delivery.result == "withheld":
                self.withheld[(attempt.message, seat.name)] = time.monotonic()
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
        return f"unknown request {kind!r}"

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
