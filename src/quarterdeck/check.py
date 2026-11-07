"""qd check <harness>: prove the installed version of a program, live, on a throwaway seat.

The seat opens in a tmux session of its own, named for this run, in a home of its own under
the ship's runtime, on the ship's first pool for that provider. The contract probes run
once: launch, an open door, a message pasted and matched in the box, submitted and found in
the record, and one task the check can verify without reading prose: write a named file with
a given line, and post a report on the order. When everything holds the version is marked
proven in the runtime. It spends real tokens and runs only when asked.
"""

from __future__ import annotations

import secrets
import shutil
from dataclasses import replace

from . import harness as harnesses, post
from .door import Door
from .drill import Check, wait_for
from .seat import Seat
from .ship import SeatSpec, Session, Ship, ShipError
from .tmux import Tmux

PATIENCE = 180.0


def check(ship: Ship, name: str, tmux: Tmux) -> list[Check]:
    harness = harnesses.get(name)
    pool = next((p for p in ship.pools.values() if p.provider == name), None)
    if pool is None:
        raise ShipError(f"no pool of provider {name!r} in {ship.file}; declare one to check it against")
    version = harness.version()
    checks = [Check("installed", version is not None, f"{name} {version or 'not found on the PATH'}")]
    if version is None:
        return checks
    like = next((s for s in ship.seats.values() if s.harness == name), None)
    run = secrets.token_hex(2)
    seat_name, session_name = f"check-{name}-{run}", f"qd-check-{name}-{run}"
    spec = SeatSpec(seat_name, session_name, "worker", name, (pool.name,), model=like.model if like else None,
                    effort=like.effort if like else None, cwd=ship.workspace)
    checked = replace(ship, sessions={**ship.sessions, session_name: Session(session_name, session_name)},
                      seats={**ship.seats, seat_name: spec})
    seat = Seat(checked, spec, tmux)
    store = post.Store(ship.runtime)
    opened_session = not tmux.has_session(session_name)
    try:
        checks += probe(checked, seat)
    finally:
        seat.down()
        if opened_session and tmux.has_session(session_name):
            tmux.kill_session(session_name)  # only what this run opened
        for order in store.open_for(seat_name):  # the check closes what it opened, in the seat's name
            store.new("report", seat_name, order.sender, "qd check ended; closed by the check itself", re=order.id)
        shutil.rmtree(seat.folder, ignore_errors=True)
    if all(c.passed for c in checks):
        proven = ship.runtime / "proven"
        proven.mkdir(parents=True, exist_ok=True)
        (proven / f"{name}-{version}").write_text(f"{post.now().isoformat(timespec='seconds')}\n")
        checks.append(Check("proven", True, f"{proven / f'{name}-{version}'}"))
    return checks


def probe(ship: Ship, seat: Seat) -> list[Check]:
    checks: list[Check] = []
    proof_line = f"qd-check-{secrets.token_hex(4)}"
    proof_file = ship.runtime / "check" / f"{seat.name}.txt"
    proof_file.parent.mkdir(parents=True, exist_ok=True)
    launched = seat.up(resume=False)
    checks.append(Check("launch", seat.is_up, launched))
    door = Door(seat)
    opened = wait_for(lambda: door.reading().open, PATIENCE / 3)
    checks.append(Check("door open at start", bool(opened), str(door.reading())))
    if not opened:
        return checks
    store, attempts = post.Store(ship.runtime), post.Attempts(ship.runtime)
    order = store.new("order", "ship", seat.name,
                      f"Write the file {proof_file} containing exactly one line: {proof_line}\n"
                      f"Then post your report on this order with the body: written", subject="qd check")
    launch = seat.launched()
    attempt = attempts.open(order, seat.name, launch.pane, launch.pid, launch.at)
    delivery = door.deliver(post.typed([order]))
    attempts.close(attempt, delivery.result, delivery.detail)
    taken = delivery.result in ("delivered", "queued")
    checks.append(Check("paste matched the box, submitted, confirmed by the record", taken, str(delivery)))
    if not taken:
        return checks
    closed = wait_for(lambda: store.closing_report(order), PATIENCE)
    checks.append(Check("task: the report closed the order", closed is not None,
                        f"report {closed.short}: {closed.body[:40]!r}" if closed else "no report within the patience"))  # law: can-post
    content = proof_file.read_text().strip() if proof_file.exists() else None
    checks.append(Check("task: the named file holds the given line", content == proof_line, f"{proof_file}: {content!r}"))
    ended = wait_for(lambda: (r := seat.record()) and seat.harness.state(r).turns >= 1 and not seat.harness.state(r).turn_open, 60)
    record = seat.record()
    state = seat.harness.state(record) if record else None
    checks.append(Check("record joined, the turn ended and read", bool(ended),
                        f"{record.name if record else 'no record'} · {state.turns if state else 0} turns · model {state.model if state else '?'}"))
    return checks
