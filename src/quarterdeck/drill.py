"""The Drill: proof that the ship works.

The all-hands exercise: every seat sends an order to every other seat and each is closed
by its addressee's report; the service is killed in the middle of a delivery and recovers
with no message lost or doubled; every seat is relaunched fresh and receives what is open;
and the matrix runs again. On the example ship's fake harness by default, on a private
tmux server; on a real ship when one is named, with real programs and real tokens.
"""

from __future__ import annotations

import os
import shutil
import signal
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from . import post, seat as seatmod, service, ship as shipmod, watch
from .post import Attempts, Store
from .seat import Seat
from .tmux import Tmux

EXAMPLE = Path(__file__).resolve().parents[2] / "ships" / "example"
QUICK = '\n[seats.{seat}.settings]\nstep_seconds = "0.05"\n'
SLOW_TO_ACCEPT = 'accept_seconds = "1.5"\n'  # one seat takes its time writing a message into its record


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
        closed = wait_for(lambda: all(not self.store.is_open(o) for o in orders.values()), self.patience * 2)
        for (sender, to), order in orders.items():
            report = self.store.closing_report(order)
            results = [a.result for a in self.attempts.for_message(order.id) if a.delivered]
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
            printed += post.wait(self.ship.runtime, name, interval=0.1, stop_after=deadline - time.monotonic()) or ""
        for order in orders:
            received = order.short in printed
            self.check(f"{name} reached by its watch", received,
                       f"{order.short} " + ("printed by qd inbox --wait" if received else "not printed"))
            if received:
                self.store.new("report", name, order.sender, "drill-done", re=order.id)

    def crash(self) -> None:
        """Kill the service with an attempt open; after it restarts, the message is delivered once or visibly not."""
        captain = next(s for s in self.seats() if not s.session.human)
        order = self.store.new("order", "ship", captain.name, "drill: the service dies mid-delivery\n$ qd post report --re {this} survived")
        beat = service.read_heartbeat(self.ship.runtime)
        caught = wait_for(self.attempts.unfinished, 10, step=0.02)
        os.kill(beat[0], signal.SIGKILL)
        time.sleep(0.5)
        service.heartbeat_path(self.ship.runtime).unlink(missing_ok=True)
        restarted = service.start(self.ship)
        settled = wait_for(lambda: all(a.result for a in self.attempts.for_message(order.id)) and
                           self.attempts.for_message(order.id), self.patience)
        results = [a.result for a in self.attempts.for_message(order.id)]
        delivered = [r for r in results if r in post.DELIVERED]
        record = captain.record()
        typed = sum(order.short in t.input for t in captain.harness.turns(record)) if record else 0
        once = len(delivered) == 1 and typed <= 1
        visibly_not = not delivered and "blind" in results and watch.facts(self.ship, self.tmux).blind >= 1
        self.check("service killed mid-delivery, recovered", bool(settled) and (once or visibly_not),
                   f"{'caught with an attempt open' if caught else 'killed between attempts'}; attempts {results}; "
                   f"in the record {typed} time(s); {restarted}")

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


def temporary_example() -> tuple[shipmod.Ship, Tmux, Path]:
    """A copy of the example ship with quick fakes, on a private tmux server."""
    folder = Path(tempfile.mkdtemp(prefix="qd-drill-")) / "example"
    shutil.copytree(EXAMPLE, folder)
    ship = shipmod.load(folder)
    slow = next(name for name, spec in ship.seats.items() if not ship.session_of(spec).human)
    with (folder / "ship.toml").open("a") as f:
        f.write("".join(QUICK.format(seat=seat) + (SLOW_TO_ACCEPT if seat == slow else "") for seat in ship.seats))
    return shipmod.load(folder), Tmux(socket=f"qd-drill-{os.getpid()}"), folder.parent


def run(ship: shipmod.Ship | None, keep: bool = False) -> tuple[bool, list[str]]:
    """``qd drill``: the exercise and its pass table; the temporary ship is removed unless kept."""
    temporary = ship is None
    if temporary:
        ship, tmux, folder = temporary_example()
        os.environ["QD_TMUX_SOCKET"] = tmux.socket
    else:
        tmux, folder = Tmux.default(), None
    checks = Drill(ship, tmux).run()
    passed = all(c.passed for c in checks)
    lines = [str(c) for c in checks]
    lines.append(f"{'all' if passed else sum(not c.passed for c in checks)} {'passed' if passed else 'failed'} of {len(checks)} checks")
    if temporary and not keep:
        tmux.kill_server()
        shutil.rmtree(folder, ignore_errors=True)
    elif temporary:
        lines.append(f"kept: ship {ship.folder} on tmux -L {tmux.socket}")
    return passed, lines
