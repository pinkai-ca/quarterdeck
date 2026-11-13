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

from . import harness as harnesses, durable, human_wake, pending, post, providers, standing
from .door import Delivery, Door
from .drill import Check, wait_for
from .harness.base import _version_in_runtime, program_where
from .seat import Seat, temporary_seat
from .ship import SeatSpec, Ship, ShipError
from .tmux import Tmux, TmuxError

PATIENCE = 180.0


def ready_for_probe(seat: Seat, door: Door) -> bool:
    """An opening prompt's own turn must finish before the check task is sent."""
    opened = door.reading()
    if not opened.open:
        screen = door.screen()
        if screen is not None and screen.interruption is not None and screen.interruption.click is not None:
            door.clear_known(screen)  # the account holder's question the orders answer, through the door, once
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
    where = program_where(harness)
    selected = harness.selected_program() if where is not None else None
    checks = [Check("installed", version is not None,
                    f"{name} {version or 'not found on the PATH'} · provider {providers.display(pool.provider)} · pool {pool.name}"
                    + (f" · runs {where}" if where else ""))]
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
        if selected is not None:
            launched = seat.launched()
            ran = launched is not None and str(selected) in launched.argv
            checks.append(Check("the seat ran the program doctor shows", ran,
                                f"{selected} in launch.json" if ran else f"{selected} not in the seat's launch"))
    if not all(c.passed is True for c in checks):
        return checks
    inapplicable = harness.waking_inapplicability
    if inapplicable is not None:
        checks.append(Check('human-session waking line', None, f'not applicable: {inapplicable}'))
    else:
        human_spec = SeatSpec(f"check-wake-{name}-{run}", f"qd-check-wake-{name}-{run}", "worker", name,
                              (pool.name,), model=like.model if like else None,
                              effort=like.effort if like else None, cwd=ship.workspace,
                              sandbox=like.sandbox if like else None)
        human_orders: list[post.Message] = []
        try:
            with temporary_seat(ship, tmux, human_spec, human_orders, "qd check human wake", human=True) as human_seat:
                try:
                    checks += probe_wake(human_seat, human_orders)
                finally:
                    wake = human_seat.folder / 'human-wake.json'
                    if wake.exists():
                        evidence = ship.runtime / 'check' / f'{human_seat.name}-human-wake.json'
                        evidence.parent.mkdir(parents=True, exist_ok=True)
                        durable.replace_file(evidence, wake.read_text())
        except (OSError, ShipError, TmuxError, ValueError) as error:
            checks.append(Check('human-session waking line', False, f'wake proof obstructed: {error}'))
    if all(c.passed is not False for c in checks):
        proven = ship.runtime / "proven"
        proven.mkdir(parents=True, exist_ok=True)
        (proven / f"{name}-{version}").write_text(f"{post.now().isoformat(timespec='seconds')}\n"
                                                  + (f"{where}\n" if where else ""))
        label = "proven: launch, door, paste, task, turn read"
        if inapplicable is None:
            label += ", human-session wake and inbox answer"
        checks.append(Check(label, True,
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
    for done in getattr(seat.launched(), "answered", None) or []:
        checks.append(Check(f"answered for the account holder: {done.get('question')}", done.get("outcome") == "cleared",
                            f"{done.get('choice')} pressed at {done.get('at')} · {done.get('outcome')}"))
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


def probe_wake(seat: Seat, orders: list[post.Message]) -> list[Check]:
    """The real service wake path, with its own human-owned throwaway session.

    No message is carried at launch. The order is published only after an empty
    composer is observed with no watch. An account-holder dialog is a failure;
    this probe never dismisses one or presses a choice (including training).
    """
    checks: list[Check] = []
    launched = seat.up(resume=False)
    if not seat.is_up:
        return [Check('human-session waking line', False, f'human launch failed: {launched}')]
    door = Door(seat)
    observed = {'reason': 'human composer not ready'}

    def empty():
        watch = post.watch_reading(seat.ship.runtime, seat.name)
        if watch.armed is not False:
            observed['reason'] = watch.reason or 'watch already armed; wake precondition not met'
            return False
        screen = door.screen()
        reading = door._reading(screen)
        observed['reason'] = reading.reason
        if screen is not None and screen.box == '' and not screen.working and not screen.queued and reading.open:
            return True
        return False

    if not wait_for(empty, PATIENCE / 3):
        return [Check('human-session waking line', False, observed['reason'] or 'known-empty composer not established')]
    generation = post.watch_reading(seat.ship.runtime, seat.name).generation
    store, attempts = post.Store(seat.ship.runtime), post.Attempts(seat.ship.runtime)
    token = f'wake-answer-{secrets.token_hex(4)}'
    order = store.new('order', 'ship', seat.name,
                      'Keep `qd inbox --wait` armed in the background yourself. '
                      'Read this order through your inbox watch, then report on this order '
                      f'with exactly this body: {token}', subject='qd check human wake')
    orders.append(order)
    post.render_inbox(seat.ship.runtime, store, seat.name)

    def taken():
        reason = human_wake.run(seat)
        observed['reason'] = reason or ''
        proof = human_wake.load(seat)
        return proof is not None and proof.data['phase'] == 'delivered'

    received = wait_for(taken, PATIENCE)
    checks.append(Check('human-session waking line: known-empty paste and record receipt', bool(received),
                        'fixed ship waking line confirmed; waiting payload was not typed' if received else
                        observed['reason'] or 'wake receipt not confirmed'))
    if not received:
        return checks
    armed = wait_for(lambda: (reading := post.watch_reading(seat.ship.runtime, seat.name)).armed is not None
                     and reading.generation is not None and reading.generation != generation, PATIENCE)
    checks.append(Check('human-session waking line: seat armed its inbox watch', bool(armed),
                        'durable watch registration observed' if armed else 'watch not armed after confirmed wake'))
    read = wait_for(lambda: any(a.result == 'watch' for a in attempts.for_message(order.id)), PATIENCE) if armed else None
    checks.append(Check('human-session waking line: watch read the waiting message', bool(read),
                        'ordinary watch receipt' if read else 'waiting message has no watch receipt'))
    report = wait_for(lambda: store.closing_report(order), PATIENCE) if read else None
    checks.append(Check('human-session waking line: seat answered the message',
                        bool(report and report.body.strip() == token),
                        'answer closed the waiting order' if report and report.body.strip() == token else
                        'matching answer not received'))
    return checks
