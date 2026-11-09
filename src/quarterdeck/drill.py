"""The Drill: proof that the ship works.

The all-hands exercise: every seat sends an order to every other seat and each is closed
by its addressee's report; the service is killed in the middle of a delivery and recovers
with no message lost or doubled; every seat is relaunched fresh and receives what is open;
and the matrix runs again. On the drydock's simulator by default, on a private
tmux server; on a real ship when one is named, with real programs and real tokens.
"""

from __future__ import annotations

import os
import shutil
import signal
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from contextlib import nullcontext
from pathlib import Path

from . import harness as harnesses, input_receipt, post, seat as seatmod, service, ship as shipmod, standing, watch
from .post import Attempts, Store
from .pending import journals
from .harness.synthetic_control import Rendezvous, events
from .harness.types import Input
from .seat import Seat
from .tmux import Tmux, TmuxError

DRYDOCK = standing.PROJECT_DIR / "ships" / "drydock"



@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str = ""

    def __str__(self) -> str:
        return f"{'PASS' if self.passed else 'FAIL'}  {self.name}" + (f" · {self.detail}" if self.detail else "")


def wait_for(predicate, seconds: float, step: float = 0.1):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return None


def acceptances(inputs: list[Input], text: str, since: datetime) -> int:
    """Count fresh acceptance evidence; dequeue and echo can share one enqueue time.

    This cannot distinguish separate enqueues with identical timestamps. The crash
    check also requires one attempt and, on the simulator, one actual submission.
    """
    accepted, consumed = 0, set()
    for item in inputs:
        if item.at is None or item.at < since or item.text.strip() != text.strip():
            continue
        if item.kind == 'accepted':
            accepted += 1
        elif item.kind == 'consumed' and item.consumed_at is not None and item.consumed_at >= item.at:
            consumed.add(item.at)  # original acceptance time, never the later consumption time
    return accepted + len(consumed)


class Drill:
    def __init__(self, ship: shipmod.Ship, tmux: Tmux, patience: float = 30.0):
        self.ship, self.tmux, self.patience = ship, tmux, patience
        self.store, self.attempts = Store(ship.runtime), Attempts(ship.runtime)
        self.checks: list[Check] = []

    def check(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append(Check(name, bool(passed), detail))

    def seats(self) -> list[Seat]:
        return seatmod.seats(self.ship, self.tmux)

    def run(self) -> list[Check]:
        try:
            self.up()
            self.matrix("round 1")
            self.crash()
            self.fresh_all()
            self.matrix("round 2")
        finally:
            service.stop(self.ship.runtime)
        return self.checks

    # -- the exercises -------------------------------------------------------------------

    def up(self) -> None:
        proofs = seatmod.bring_up(self.ship, self.tmux) + [service.start(self.ship)]
        up = [s for s in self.seats() if s.is_up]
        self.check("every seat up", len(up) == len(self.ship.seats), f"{len(up)} of {len(self.ship.seats)}; " + proofs[-1])
        ready = wait_for(lambda: all(watch.reading(s).door.open or s.session.human for s in self.seats()), self.patience)
        self.check("every door open", bool(ready), ", ".join(f"{s.name} {watch.reading(s).door}" for s in self.seats()))

    def matrix(self, label: str) -> None:
        """Every seat orders every other seat to report back; each order must close."""
        names = list(self.ship.seats)
        orders = {}
        for sender in names:
            for to in names:
                if sender != to:
                    body = f"drill {label}: {sender} to {to}\n$ qd post report --re {{this}} drill-done"
                    orders[(sender, to)] = self.store.new("order", sender, to, body, subject=f"drill {label}")
        for name, spec in self.ship.seats.items():
            if self.ship.session_of(spec).human:
                self.play_human_seat(name, [o for (_, to), o in orders.items() if to == name])
        # A program can post its report before the service finishes writing its
        # receipt. Wait for both observable facts, keeping the exact-once check.
        wait_for(lambda: all(not self.store.is_open(o) and any(a.accepted for a in self.attempts.for_message(o.id))
                             for o in orders.values()), self.patience * 2)
        closed = all(not self.store.is_open(o) for o in orders.values())
        for (sender, to), order in orders.items():
            report = self.store.closing_report(order)
            results = [a.result for a in self.attempts.for_message(order.id) if a.accepted]
            ok = report is not None and len(results) == 1
            how = f"{results[0]}" if results else "never delivered"
            self.check(f"{label}: {sender} → {to}", ok, f"delivered once by {how}; closed by {report.short if report else 'nobody'}")
        self.check(f"{label}: every order closed", bool(closed), f"{sum(not self.store.is_open(o) for o in orders.values())} of {len(orders)}")
        for seat in self.seats():
            if seat.session.human:
                screen = seat.harness.screen(self.tmux.capture(seat.pane().id)) if seat.pane() else None
                self.check(f"{label}: {seat.name} never typed into", screen is not None and screen.box == "",
                           f"box {screen.box!r}" if screen else "no screen")

    def play_human_seat(self, name: str, orders: list[post.Message]) -> None:
        """Stand in for the agent at a human-session seat: its watch prints the orders, it reports on each."""
        printed, deadline = "", time.monotonic() + self.patience
        while time.monotonic() < deadline and not all(o.short in printed for o in orders):
            arrived = post.wait(self.ship.runtime, name, interval=0.1, stop_after=deadline - time.monotonic())
            if arrived:
                printed += arrived[0]
                post.printed(self.ship.runtime, arrived[1])
        for order in orders:
            received = order.short in printed
            self.check(f"{name} reached by its watch", received,
                       f"{order.short} " + ("printed by qd inbox --wait" if received else "not printed"))
            if received:
                self.store.new("report", name, order.sender, "drill-done", re=order.id)

    def crash_attempt(self, captain: Seat, order: post.Message, gate) -> bool:
        """This order is submitted durably, still open, and not yet in its record."""
        attempts = self.attempts.for_message(order.id)
        if len(attempts) != 1 or attempts[0].result is not None:
            return False
        proofs, errors = journals(captain)
        proof = next((p for p in proofs if attempts[0].path.name in p.data["attempts"]), None)
        if (errors or proof is None or proof.data["phase"] != "submitted" or not proof.same_launch(captain)
                or proof.data["payload"] != post.typed([order]) or proof.confirmed(captain)):
            return False
        reached = gate.reached() if gate else None
        return gate is None or bool(reached and reached["text"] == post.typed([order]).strip() and
                                    reached["pid"] == captain.pane().pid)

    def crash(self) -> None:
        """Catch a submitted, unconfirmed transport; kill its owner and recover it exactly once."""
        captain = next(s for s in self.seats() if not s.session.human)
        marker = "drill: the service dies mid-delivery"
        gate = Rendezvous(captain.home, "accept", marker, self.patience * 2) if captain.harness.synthetic else None
        with gate or nullcontext():
            ready = wait_for(lambda: (pane := captain.pane()) and
                             not captain.harness.screen(self.tmux.capture(pane.id)).working, self.patience)
            if not ready:
                self.check("service killed mid-delivery, recovered", False, "seat never became idle; no crash claimed")
                return
            order = self.store.new("order", "ship", captain.name, marker + "\n$ qd post report --re {this} survived")
            caught = wait_for(lambda: self.crash_attempt(captain, order, gate), self.patience, step=0.02)
            if not caught:
                self.check("service killed mid-delivery, recovered", False,
                           "no submitted open attempt caught before confirmation; no crash claimed")
                return
            attempt, = self.attempts.for_message(order.id)
            beat = service.read_heartbeat(self.ship.runtime)
            os.kill(beat[0], signal.SIGKILL)
            if not wait_for(lambda: not service.alive(self.ship.runtime), self.patience):
                self.check("service killed mid-delivery, recovered", False, "killed service still owns its lock")
                return
            service.heartbeat_path(self.ship.runtime).unlink(missing_ok=True)
            restarted = service.start(self.ship)
            if gate:
                gate.release()
            settled = wait_for(lambda: self.store.closing_report(order) and
                               [a.result for a in self.attempts.for_message(order.id)] == ["delivered"], self.patience)
            results = [a.result for a in self.attempts.for_message(order.id)]
            record = captain.record()
            payload = post.typed([order])
            typed = acceptances(captain.harness.inputs(record), payload, attempt.receipt_since) if record else 0
            delivered = record is not None and input_receipt.confirmed(
                captain.harness, record, payload, attempt.receipt_since) == 'delivered'
            submits = sum(e == {"event": "submitted", "text": post.typed([order]).strip()} for e in events(captain.home)) if gate else None
            once = delivered and typed == 1 and (submits == 1 if gate else True)
            self.check("service killed mid-delivery, recovered", bool(settled) and once,
                       f"caught submitted open attempt before record acceptance; attempts {results}; "
                       f"in the record {typed} time(s); submissions {submits}; {restarted}")

    def fresh_all(self) -> None:
        """Every seat relaunched fresh receives its brief and every open message in full."""
        pending = {name: self.store.new("order", "ship", name, f"drill: still open for {name} across a fresh start")
                   for name in self.ship.seats}
        time.sleep(1.5)  # let the service deliver them to the running seats first
        for seat in self.seats():
            proof = seat.fresh().splitlines()[-1]
            self.check(f"{seat.name} fresh", "· fresh ·" in proof and proof.endswith("carried"), proof)
        for seat in self.seats():
            record = wait_for(seat.record, self.patience)
            first = wait_for(lambda: (seat.harness.turns(record) or [None])[0] if record else None, self.patience)
            carried = first is not None and pending[seat.name].short in first.input and "qd > briefed as" in first.input
            self.check(f"{seat.name} briefed with its open order", carried,
                       first.input.splitlines()[0] if first else "no first turn")  # law: context-is-cache
            self.store.new("report", seat.name, "ship", "closing the carried order", re=pending[seat.name].id)
        for seat in self.seats():
            wait_for(lambda: watch.reading(seat).door.open or seat.session.human, self.patience)


def temporary_drydock() -> tuple[shipmod.Ship, Tmux, Path]:
    """A copy of the drydock with quick simulators, on a private tmux server."""
    folder = Path(tempfile.mkdtemp(prefix="qd-drill-")) / "drydock"
    shutil.copytree(DRYDOCK, folder)
    ship = shipmod.load(folder)
    slow = next(name for name, spec in ship.seats.items() if not ship.session_of(spec).human)
    text = ship.file.read_text()
    for name, spec in ship.seats.items():
        for key, value in harnesses.get(spec.harness).drill_settings(name == slow).items():
            text = shipmod.edit(text, f"seats.{name}.settings", key, value)
    ship.file.write_text(text)
    return shipmod.load(folder), Tmux.private(standing.PROJECT_DIR / 'work'), folder.parent


def temporary_copy(ship: shipmod.Ship) -> tuple[shipmod.Ship, Path]:
    """A named ship's declaration copied for the live drill: its real pools and programs, in throwaway
    ``qd-drill`` sessions, with a runtime of its own; the live ship's seats and service are never touched."""
    folder = Path(tempfile.mkdtemp(prefix="qd-drill-")) / ship.folder.name
    shutil.copytree(ship.folder, folder, ignore=shutil.ignore_patterns("runtime", ".git"))
    text = (folder / "ship.toml").read_text()
    for name, session in ship.sessions.items():
        text = shipmod.edit(text, f"sessions.{name}", "title", f"qd-drill {session.title}")
    (folder / "ship.toml").write_text(text)
    return shipmod.load(folder), folder.parent


def run(ship: shipmod.Ship | None, keep: bool = False) -> tuple[bool, list[str]]:
    """``qd drill``: the exercise and its pass table; the temporary ship is removed unless kept."""
    private = ship is None
    previous_socket = os.environ.get('QD_TMUX_SOCKET')
    if private:
        ship, tmux, folder = temporary_drydock()
        os.environ["QD_TMUX_SOCKET"] = tmux.socket
    else:
        ship, folder = temporary_copy(ship)
        tmux = Tmux.default()
    try:
        checks = Drill(ship, tmux).run()
        passed = all(c.passed for c in checks)
        lines = [str(c) for c in checks]
        lines.append(f"{'all' if passed else sum(not c.passed for c in checks)} {'passed' if passed else 'failed'} of {len(checks)} checks")
    finally:
        try:
            if not keep:
                try:
                    seatmod.bring_down(ship, tmux)
                finally:
                    if private and not tmux.close():
                        raise TmuxError(f'private drill cleanup held: {tmux.held}; retained ship {ship.folder}')
                    shutil.rmtree(folder, ignore_errors=True)
        finally:
            if private:
                if previous_socket is None:
                    os.environ.pop('QD_TMUX_SOCKET', None)
                else:
                    os.environ['QD_TMUX_SOCKET'] = previous_socket
    if keep:
        session = next(iter(ship.sessions.values())).title
        lines.append(f'kept: ship {ship.folder} · {tmux.attach_command(session)}')
    return passed, lines
