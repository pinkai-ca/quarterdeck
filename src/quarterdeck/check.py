"""qd check <harness>: prove the installed version of a program, live, on a throwaway seat.

The seat opens in a tmux session of its own, named for this run, in a home of its own under
the ship's runtime, on the ship's first pool for that provider. The contract probes run
once: launch, an open door, a message long enough to fold in any program's box, pasted and
matched as the program shows it, submitted and found in the record, and one task the check
can verify without reading prose: write a named file with a given line, and post a report
on the order. When everything holds the version is marked proven in the runtime. It spends
real tokens and runs only when asked.
"""

from __future__ import annotations

import secrets
import shutil
from dataclasses import replace

from . import harness as harnesses, pending, post, standing
from .door import Door
from .drill import Check, wait_for
from .seat import Seat
from .ship import SeatSpec, Session, Ship, ShipError
from .tmux import Tmux

PATIENCE = 180.0


def check(ship: Ship, name: str, tmux: Tmux) -> list[Check]:
    harness = harnesses.get(name)
    pool = next((p for p in ship.pools.values() if harness.accepts(p) is None), None)
    if pool is None:
        raise ShipError(f"no pool in {ship.file} that {name} runs on; declare one to check it against")
    version = harness.version()
    checks = [Check("installed", version is not None, f"{name} {version or 'not found on the PATH'}")]
    if version is None:
        return checks
    like = next((s for s in ship.seats.values() if s.harness == name), None)
    run = secrets.token_hex(2)
    seat_name, session_name = f"check-{name}-{run}", f"qd-check-{name}-{run}"
    spec = SeatSpec(seat_name, session_name, "worker", name, (pool.name,), model=like.model if like else None,
                    effort=like.effort if like else None, cwd=ship.workspace, sandbox=like.sandbox if like else None)
    checked = replace(ship, sessions={**ship.sessions, session_name: Session(session_name, session_name)},
                      seats={**ship.seats, seat_name: spec})
    seat = Seat(checked, spec, tmux)
    store = post.Store(ship.runtime)
    opened_session = not tmux.has_session(session_name)
    with pending.transient_lock(ship.runtime, seat_name) as owned:
        if not owned:
            raise ShipError(f"{seat_name}: temporary seat ownership is busy; retry qd check")
        try:
            checks += probe(checked, seat)
        finally:
            # The transient seat is absent from the persisted declaration. Retire its
            # unresolved transport before deleting its home, even if the probe/stop fails.
            pending.abandon(ship.runtime, seat_name, 'qd check ended; submission unconfirmed and never resent')
            try:
                seat.down()
            finally:
                try:
                    if opened_session and tmux.has_session(session_name):
                        tmux.kill_session(session_name)  # only what this run opened
                finally:
                    for order in store.open_for(seat_name):
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
    laws = (standing.FRAMEWORK_DIR / "laws.md").read_text().strip()
    order = store.new("order", "ship", seat.name,
                      f"Write the file {proof_file} containing exactly one line: {proof_line}\n"
                      f"Then post your report on this order with the body: written\n\n"
                      f"The rest of this order is the laws you already run under, repeated so that the message is long "
                      f"enough to fold in your box; nothing in it changes the task.\n\n{laws}", subject="qd check")
    launch = seat.launched()
    attempt = attempts.open(order, seat.name, launch.pane, launch.pid, launch.at)
    text = post.typed([order])
    delivery = door.deliver(text)
    attempts.close(attempt, delivery.result, delivery.detail)
    taken = delivery.result in ("delivered", "queued")
    checks.append(Check(f"a {len(text)}-character paste matched the box as the program shows it, submitted, confirmed",
                        taken, str(delivery)))
    if not taken:
        return checks
    closed = wait_for(lambda: store.closing_report(order), PATIENCE)
    checks.append(Check("task: the report closed the order", closed is not None,
                        f"report {closed.short}: {closed.body[:40]!r}" if closed else "no report within the patience"))  # law: can-post
    content = proof_file.read_text().strip() if proof_file.exists() else None
    checks.append(Check("task: the named file holds the given line", content == proof_line, f"{proof_file}: {content!r}"))
    if not seat.harness.record_confirms:
        record = wait_for(seat.record, 60)
        checks.append(Check("record joined; not read, this program's record is opaque", record is not None,
                            record.name if record else "no record"))
        return checks
    def _turn_ended() -> bool:
        if (r := seat.record()) is None:
            return False
        try:
            s = seat.harness.state(r)
        except OSError:
            return False
        return s.turns is not None and s.turns >= 1 and s.turn_open is False

    ended = wait_for(_turn_ended, 60)
    record = seat.record()
    try:
        state = seat.harness.state(record) if record else None
    except OSError:
        state = None
    turns_shown = state.turns if state and state.turns is not None else "?"
    checks.append(Check("record joined, the turn ended and read", bool(ended),
                        f"{record.name if record else 'no record'} · {turns_shown} turns · model {state.model if state and state.model else '?'}"))
    return checks
