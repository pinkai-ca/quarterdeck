"""The Drill: proof that the ship works.

The all-hands exercise: every seat sends an order to every other seat and each is closed
by its addressee's report; the service is killed in the middle of a delivery and recovers
with no message lost or doubled; every seat is relaunched fresh and receives what is open;
and the matrix runs again. On the drydock's simulator by default, on a private
tmux server; on a real ship when one is named, with real programs and real tokens.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from contextlib import nullcontext
from pathlib import Path

from . import harness as harnesses, human_wake, input_receipt, lifecycle, post, seat as seatmod, service, ship as shipmod, standing, watch
from .post import Attempts, Store
from .pending import journals
from .harness.synthetic_control import Rendezvous, events
from .harness.types import Input
from .seat import Seat
from .tmux import Tmux, TmuxError

DRYDOCK = standing.PROJECT_DIR / "ships" / "drydock"
DRILL_TICK = 0.1  # seconds: the generated drydock's service passes quickly; ships/drydock keeps the default



@dataclass(frozen=True)
class Check:
    name: str
    passed: bool | None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.passed is None and not self.detail.startswith('not applicable: '):
            raise ValueError('an inapplicable check needs an explicit named reason')

    def __str__(self) -> str:
        return f"{'N/A' if self.passed is None else 'PASS' if self.passed else 'FAIL'}  {self.name}" + (f" · {self.detail}" if self.detail else "")


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
        receipt_deadline = time.monotonic() + self.patience
        for (sender, to), order in orders.items():
            report = self.store.closing_report(order)
            attempts = self.attempts.for_message(order.id)
            if report is not None and not any(a.accepted for a in attempts):
                # The global wait may expire just before this report appears.
                # Reported orders share one extra window, not one per order.
                remaining = max(0.0, receipt_deadline - time.monotonic())
                wait_for(lambda: any(a.accepted for a in self.attempts.for_message(order.id)), remaining)
                attempts = self.attempts.for_message(order.id)
            results = [a.result for a in attempts if a.accepted]
            ok = report is not None and len(results) == 1
            how = f"{results[0]}" if results else "never delivered"
            states = [a.result or 'unfinished' for a in attempts]
            self.check(f"{label}: {sender} → {to}", ok,
                       f"delivered once by {how}; attempts {states}; closed by {report.short if report else 'nobody'}")
        closed = all(not self.store.is_open(o) for o in orders.values())
        self.check(f"{label}: every order closed", bool(closed), f"{sum(not self.store.is_open(o) for o in orders.values())} of {len(orders)}")
        for seat in self.seats():
            if seat.session.human:
                screen = seat.harness.screen(self.tmux.capture(seat.pane().id)) if seat.pane() else None
                self.check(f"{label}: {seat.name} box has only permitted input",
                           screen is not None and screen.box in ('', human_wake.LINE),
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
                             captain.harness.screen(self.tmux.capture(pane.id)).working is False, self.patience)
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

    def fresh_after_delivery(self, seat: Seat, order: post.Message) -> tuple[str | None, str]:
        """Wait for active delivery to settle; an unread open order can be carried by fresh."""
        reason = "pending order has no settled delivery attempt"

        def ready():
            nonlocal reason
            try:
                attempts = self.attempts.all()
                current = [a for a in attempts if a.message == order.id and a.seat == seat.name]
                if seat.session.human and not (current and current[-1].accepted):
                    # The same stand-in watch as the matrix, without closing the open order.
                    arrived = post.wait(self.ship.runtime, seat.name, interval=0.1, stop_after=0)
                    if arrived:
                        post.printed(self.ship.runtime, arrived[1])
                    attempts = self.attempts.all()
                    current = [a for a in attempts if a.message == order.id and a.seat == seat.name]
                if seat.session.human and (not current or not current[-1].accepted):
                    reason = "pending order has no settled delivery attempt"
                    return None
                # Blind closes the transport, not receipt acceptance. Journals
                # and fresh's lifecycle claim still exclude in-flight work.
                if current and not current[-1].accepted and current[-1].result not in {'withheld', 'cancelled', 'blind'}:
                    reason = f"pending order delivery is {current[-1].result or 'unfinished'}"
                    return None
                if any(a.seat == seat.name and a.result is None for a in attempts):
                    reason = "a delivery attempt is still unfinished"
                    return None
                proofs, errors = journals(seat)
                if errors:
                    reason = "pending delivery evidence is unreadable"
                    return None
                if any(p.data["phase"] in {"prepared", "pasting", "submitting", "submitted"} for p in proofs):
                    reason = "a delivery is still being typed or confirmed"
                    return None
                was_blind = bool(current and current[-1].result == 'blind')
            except (OSError, ValueError, TypeError):
                reason = "delivery evidence is unreadable"
                return None
            try:
                # Seat.fresh's Busy is its entry claim refusing before the transition body.
                # Recheck every condition on the next pass; no started fresh is retried.
                proof = seat.fresh()
            except lifecycle.Busy:
                reason = "the delivery/lifecycle claim is still held"
                return None
            # A delivery can settle between the reading above and fresh's claim.
            # Count prior acceptance after fresh, excluding this new launch's carriage.
            try:
                launched = seat.launched()
                prior = [a for a in self.attempts.for_message(order.id) if a.seat == seat.name
                         and (launched is None or a.launched_at != launched.at)]
                mode = ("delivered first" if any(a.accepted for a in prior) else
                        "carried after finished blind attempt" if was_blind else
                        f"carried after settled {prior[-1].result}" if prior else "carried unattempted")
            except (OSError, ValueError, TypeError):
                mode = "carried; prior delivery evidence unreadable"
            return proof, mode

        result = wait_for(ready, self.patience)
        return result if result is not None else (None, reason)

    def fresh_all(self) -> None:
        """Every seat relaunched fresh receives its brief and an index of its open threads."""
        pending = {name: self.store.new("order", "ship", name, f"drill: still open for {name} across a fresh start")
                   for name in self.ship.seats}
        freshened = set()
        for seat in self.seats():
            proof, reason = self.fresh_after_delivery(seat, pending[seat.name])
            if proof is None:
                self.check(f"{seat.name} fresh", False,
                           f"wait for {pending[seat.name].short} delivery/lifecycle settlement timed out: {reason}")
                continue
            freshened.add(seat.name)
            proof = proof.splitlines()[-1]
            self.check(f"{seat.name} fresh", "· fresh ·" in proof and proof.endswith("indexed"), f"{proof}; {reason}")
        for seat in self.seats():
            first = None
            if seat.name in freshened:
                record = wait_for(seat.record, self.patience)
                first = wait_for(lambda: (seat.harness.turns(record) or [None])[0] if record else None, self.patience)
            carried = first is not None and pending[seat.name].id in first.input and "qd > briefed as" in first.input
            self.check(f"{seat.name} briefed with its open order", carried,
                       first.input.splitlines()[0] if first else
                       "no first turn" if seat.name in freshened else "fresh not performed after delivery wait")  # law: context-is-cache
            self.store.new("report", seat.name, "ship", "closing the carried order", re=pending[seat.name].id)
        for seat in self.seats():
            wait_for(lambda: watch.reading(seat).door.open or seat.session.human, self.patience)


def temporary_drydock() -> tuple[shipmod.Ship, Tmux, Path]:
    """A copy of the drydock with quick simulators, on a private tmux server."""
    folder = Path(tempfile.mkdtemp(prefix="qd-drill-")) / "drydock"
    shutil.copytree(DRYDOCK, folder)
    declaration = folder / 'ship.toml'
    declaration.write_text(shipmod.edit(declaration.read_text(), 'ship', 'code', 'checkout'))
    ship = shipmod.load(folder)
    slow = next(name for name, spec in ship.seats.items() if not ship.session_of(spec).human)
    text = shipmod.edit(ship.file.read_text(), "ship", "tick", DRILL_TICK)  # this generated copy only
    # This exercise owns fresh/crash transitions. An automatic after-order
    # fresh can instead carry new matrix orders as an index, which the simulator
    # program does not interpret as executable report commands.
    text = re.sub(r'(?m)^[ \t]*fresh[ \t]*=[ \t]*"after-order"[^\n]*(?:\n|$)', '', text)
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
    text = shipmod.edit(text, 'ship', 'code', 'checkout')
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
        tmux = Tmux.for_ship(ship)
    try:
        checks = Drill(ship, tmux).run()
        passed = all(c.passed for c in checks)
        # A truncated installed-command log should name its failures before its passes.
        lines = [str(c) for c in sorted(checks, key=lambda c: c.passed is not False)]
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
