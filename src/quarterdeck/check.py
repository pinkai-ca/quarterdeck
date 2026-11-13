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

from . import harness as harnesses, pending, post, providers, standing
from .door import Delivery, Door
from .drill import Check, wait_for
from .harness.base import _version_in_runtime
from .seat import Seat, temporary_seat
from .ship import SeatSpec, Ship, ShipError
from .tmux import Tmux

PATIENCE = 180.0


def ready_for_probe(seat: Seat, door: Door) -> bool:
    """An opening prompt's own turn must finish before the check task is sent."""
    opened = door.reading()
    if not opened.open:
        return False
    if not seat.harness.opening_prompt_turn:
        return True
    record = seat.record()
    if record is None or opened.working:
        return False
    try:
        state = seat.harness.state(record)
    except OSError:
        return False
    return state.turns is not None and state.turns >= 1 and state.turn_open is False and not state.queued


def check(ship: Ship, name: str, tmux: Tmux) -> list[Check]:
    harness = harnesses.get(name)
    pool = next((p for p in ship.pools.values() if harness.accepts(p) is None), None)
    if pool is None:
        raise ShipError(f"no pool in {ship.file} that {name} runs on; declare one to check it against")
    version = _version_in_runtime(harness, ship.runtime)
    checks = [Check("installed", version is not None,
                    f"{name} {version or 'not found on the PATH'} · provider {providers.display(pool.provider)} · pool {pool.name}")]
    if version is None:
        return checks
    like = next((s.for_pool(pool.name) for s in ship.seats.values()
                 if pool.name in s.pools and s.for_pool(pool.name).harness == name), None)
    run = secrets.token_hex(2)
    seat_name, session_name = f"check-{name}-{run}", f"qd-check-{name}-{run}"
    spec = SeatSpec(seat_name, session_name, "worker", name, (pool.name,), model=like.model if like else None,
                    effort=like.effort if like else None, cwd=ship.workspace, sandbox=like.sandbox if like else None,
                    model_mode=like.model_mode if like else None, effort_mode=like.effort_mode if like else None)
    orders: list[post.Message] = []
    with temporary_seat(ship, tmux, spec, orders, "qd check", installed_version=version) as seat:
        checks += probe(seat.ship, seat, orders)
    if all(c.passed for c in checks):
        proven = ship.runtime / "proven"
        proven.mkdir(parents=True, exist_ok=True)
        (proven / f"{name}-{version}").write_text(f"{post.now().isoformat(timespec='seconds')}\n")
        checks.append(Check("proven: launch, an open door, a paste the record confirmed, the task, the turn read", True,
                            f"{proven / f'{name}-{version}'}"))
    return checks


def probe(ship: Ship, seat: Seat, orders: list[post.Message] | None = None) -> list[Check]:
    checks: list[Check] = []
    proof_line = f"qd-check-{secrets.token_hex(4)}"
    proof_file = ship.runtime / "check" / f"{seat.name}.txt"
    proof_file.parent.mkdir(parents=True, exist_ok=True)
    launched = seat.up(resume=False)
    checks.append(Check("launch", seat.is_up, launched))
    door = Door(seat)
    opened = wait_for(lambda: ready_for_probe(seat, door), PATIENCE if seat.harness.opening_prompt_turn else PATIENCE / 3)
    checks.append(Check("door open at start", bool(opened), str(door.reading()) +
                        (" · opening brief turn ended in the record" if opened and seat.harness.opening_prompt_turn else "")))
    if not opened:
        return checks
    try:
        initial = seat.harness.state(record) if (record := seat.record()) else None
        before_turns = initial.turns if initial and initial.turns is not None else 0
    except OSError:
        before_turns = 0  # a minimum bound only; the later receipt and ended task are still required
    store, attempts = post.Store(ship.runtime), post.Attempts(ship.runtime)
    laws = (standing.FRAMEWORK_DIR / "laws.md").read_text().strip()
    order = store.new("order", "ship", seat.name,
                      f"Write the file {proof_file} containing exactly one line: {proof_line}\n"
                      f"Then post your report on this order with the body: written\n\n"
                      f"The rest of this order is the laws you already run under, repeated so that the message is long "
                      f"enough to fold in your box; nothing in it changes the task.\n\n{laws}", subject="qd check")
    if orders is not None:
        orders.append(order)
    launch = seat.launched()
    attempt = attempts.open(order, seat.name, launch.pane, launch.pid, launch.at)
    text = post.typed([order])
    proof = pending.Proof.begin(seat, attempt, text)
    delivery = door.deliver(text, proof=proof)
    proof.finish(delivery.result)
    attempts.close(attempt, delivery.result, delivery.detail)
    route = str(delivery)
    if delivery.result in ("queued", "pending") and proof.data["phase"] in ("submitted", "queued"):
        # A known queue is not abandoned at the Door's short observation deadline.
        # Only the original exact input receipt may promote it; never paste/submit again.
        def consumed():
            result = proof.confirmed(seat)
            if result == "queued":
                proof.finish(result)
                attempts.close(attempt, result, "exact queued payload in the record after paste")
            return result == "delivered"
        if wait_for(consumed, PATIENCE):
            proof.finish("delivered")
            delivery = Delivery("delivered", "exact payload in the record after paste")
            attempts.close(attempt, delivery.result, delivery.detail)
            route += " → " + str(delivery)
    taken = delivery.result == "delivered"
    checks.append(Check(f"a {len(text)}-character paste matched the box as the program shows it, submitted, confirmed",
                        taken, route))
    if not taken:
        return checks
    closed = wait_for(lambda: store.closing_report(order), PATIENCE)
    checks.append(Check("task: the report closed the order", closed is not None,
                        f"report {closed.short}: {closed.body[:40]!r}" if closed else "no report within the patience"))  # law: can-post
    content = proof_file.read_text().strip() if proof_file.exists() else None
    checks.append(Check("task: the named file holds the given line", content == proof_line, f"{proof_file}: {content!r}"))
    def _turn_ended() -> bool:
        if (r := seat.record()) is None:
            return False
        try:
            s = seat.harness.state(r)
        except OSError:
            return False
        return s.turns is not None and s.turns >= before_turns + 1 and s.turn_open is False

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
