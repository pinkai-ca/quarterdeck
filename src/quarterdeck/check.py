"""qd check <harness>: prove the installed version of a program, live, on a throwaway seat.

The seat opens in a tmux session named ``qd-check-<harness>``, in a home of its own under
the ship's runtime, on the ship's first pool for that provider. The contract probes run
once: launch, an open door, a message pasted and matched in the box, submitted and found in
the record, and one task the check can verify without reading prose: write a named file with
a given line, and post a report on the order. When everything holds the version is marked
proven in the runtime. It spends real tokens and runs only when asked.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import replace
from pathlib import Path

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
    like = next((s for s in ship.seats.values() if s.harness == name), None)
    seat_name, session_name = f"check-{name}", f"qd-check-{name}"
    spec = SeatSpec(seat_name, session_name, "worker", name, (pool.name,), model=like.model if like else None,
                    effort=like.effort if like else None, cwd=ship.workspace)
    checked = replace(ship, sessions={**ship.sessions, session_name: Session(session_name, session_name)},
                      seats={**ship.seats, seat_name: spec})
    seat = Seat(checked, spec, tmux)
    checks: list[Check] = []
    version = harness.version()
    checks.append(Check("installed", version is not None, f"{name} {version or 'not found on the PATH'}"))
    try:
        checks += probe(checked, seat, version)
    finally:
        seat.down()
        if tmux.has_session(session_name):
            tmux.kill_session(session_name)
    if all(c.passed for c in checks) and version:
        proven = ship.runtime / "proven"
        proven.mkdir(parents=True, exist_ok=True)
        (proven / f"{name}-{version}").write_text(f"{post.now().isoformat(timespec='seconds')}\n")
        checks.append(Check("proven", True, f"{proven / f'{name}-{version}'}"))
    return checks


def probe(ship: Ship, seat: Seat, version: str | None) -> list[Check]:
    checks: list[Check] = []
    proof_line = f"qd-check-{secrets.token_hex(4)}"
    proof_file = ship.runtime / "check" / f"{seat.name}.txt"
    proof_file.parent.mkdir(parents=True, exist_ok=True)
    proof_file.unlink(missing_ok=True)
    launched = seat.up(resume=False)
    checks.append(Check("launch", seat.is_up, launched))
    door = Door(seat)
    opened = wait_for(lambda: door.reading().open, PATIENCE / 3)
    checks.append(Check("door open at start", bool(opened), str(door.reading())))
    if not opened:
        return checks
    store = post.Store(ship.runtime)
    order = store.new("order", "ship", seat.name,
                      f"Write the file {proof_file} containing exactly one line: {proof_line}\n"
                      f"Then post your report on this order with the body: written", subject="qd check")
    delivery = door.deliver(post.typed([order]))
    checks.append(Check("paste matched the box, submitted, found in the record", delivery.result == "delivered", str(delivery)))
    closed = wait_for(lambda: store.closing_report(order), PATIENCE)
    checks.append(Check("task: the report closed the order", closed is not None,
                        f"report {closed.short}: {closed.body[:40]!r}" if closed else "no report within the patience"))  # law: can-post
    content = proof_file.read_text().strip() if proof_file.exists() else None
    checks.append(Check("task: the named file holds the given line", content == proof_line,
                        f"{proof_file}: {content!r}"))
    record = seat.record()
    state = seat.harness.state(record) if record else None
    checks.append(Check("record joined and read", record is not None and state is not None and state.turns >= 1,
                        f"{record.name if record else 'no record'} · {state.turns if state else 0} turns · model {state.model if state else '?'}"))
    return checks
