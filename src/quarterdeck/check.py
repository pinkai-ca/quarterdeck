"""qd check <harness>: prove the installed version of a program, live, on a throwaway seat.

The seat opens in a tmux session of its own, named for this run, in a home of its own under
the ship's runtime, on the ship's first pool for that provider. The contract probes run
once: launch, an open door, a message long enough to fold in any program's box, pasted and
matched as the program shows it, submitted and found in the record, and one task the check
can verify without reading prose: write a named file with a given line, and post a report
on the order. The file lives in that seat's own check folder. It is read without following
a link, and the row says only whether the regular file matched. When everything holds the
version is marked proven in the runtime. It spends
real tokens and runs only when asked. The explicit minimal mode keeps the same task, delivery and
record confirmation, omits the deliberate folding and human-session wake probes, and never marks the
full check proven.
"""

from __future__ import annotations

import errno
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path
import secrets
import signal
import threading
from dataclasses import dataclass

from . import check_outcome, harness as harnesses, durable, human_wake, pending, post, providers, record_owner, sandbox, standing
from .door import Delivery, Door
from .drill import Check, wait_for
from .harness.base import _version_in_runtime, program_where, record_fingerprint
from .seat import Seat, temporary_seat
from .ship import SeatSpec, Ship, ShipError
from .tmux import Tmux, TmuxError

PATIENCE = 180.0


@dataclass(frozen=True)
class ProbeCheck(Check):
    reason: str | None = None


def account_question(door: Door) -> str | None:
    """Only a recognized, named native prompt supplies a question classification."""
    screen = door.screen()
    return check_outcome.QUESTIONS.get(screen.interruption.name) if screen and screen.interruption else None


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
    if record is None or opened.working is not False:
        return False
    try:
        state = seat.harness.state(record)
    except OSError:
        return False
    return state.turns is not None and state.turns >= 1 and state.turn_open is False and not state.queued


class CheckInterrupted(KeyboardInterrupt):
    """A signal that ends qd check, raised where the check stands, so its throwaway seat is taken down."""


@contextmanager
def interruptible():
    """TERM and HUP end qd check as Ctrl-C does: by an exception, so the throwaway seat's teardown runs.

    Killed outright (KILL), nothing in the process runs: the seat it leaves is named in the status line
    until qd down takes it down."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def interrupted(signum, frame):
        raise CheckInterrupted(f"qd check interrupted by {signal.Signals(signum).name}")
    previous = {number: signal.signal(number, interrupted) for number in (signal.SIGTERM, signal.SIGHUP)}
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def check(ship: Ship, name: str, tmux: Tmux, *, model: str | None = None,
          minimal: bool = False) -> list[Check]:
    if model is not None and not model.strip():
        raise ShipError("check --model requires a nonempty model identifier")
    runtime = ship.runtime / 'minimal-checks' if minimal else ship.runtime
    harness = harnesses.get(name)
    check_outcome.publish(runtime, name, check_outcome.event('unknown', reason='incomplete'))
    version = None
    try:
        with interruptible():
            checks, version = (_check(ship, name, tmux, harness, model, minimal=True)
                               if minimal else _check(ship, name, tmux, harness, model))
    except BaseException as error:
        reason = 'interrupted' if isinstance(error, (KeyboardInterrupt, SystemExit)) else 'unclassified'
        check_outcome.publish(runtime, name, check_outcome.event('unknown', version, reason))
        raise
    failures = [c for c in checks if c.passed is False]  # a step not applicable is no failure
    question = next((c.reason for c in failures if isinstance(c, ProbeCheck)
                     and c.reason in check_outcome.QUESTIONS.values()), None)
    reason = question or (getattr(failures[0], 'reason', None) if failures else 'passed')
    if reason is None or not checks:
        observed = check_outcome.event('unknown', version, 'unclassified')
    else:
        observed = check_outcome.event('failed' if failures else 'passed', version, reason)
    check_outcome.publish(runtime, name, observed)
    if minimal:
        checks.insert(0, Check('minimal check mode', True, 'one short write-line task and report'))
    return checks


def _check(ship: Ship, name: str, tmux: Tmux, harness, model: str | None = None,
           *, minimal: bool = False) -> tuple[list[Check], str | None]:
    pool = next((p for p in ship.pools.values() if harness.accepts(p) is None), None)
    if pool is None:
        return [ProbeCheck('pool', False, f'no compatible declared pool for {name}', 'pool')], None
    version = _version_in_runtime(harness, ship.runtime)
    where = program_where(harness)
    selected = harness.selected_program() if where is not None else None
    checks = [ProbeCheck("installed", version is not None,
                    f"{name} {version or 'not found on the PATH'} · provider {providers.display(pool.provider)} · pool {pool.name}"
                    + (f" · runs {where}" if where else ""), 'installed')]
    if version is None:
        return checks, version
    like = next((s.for_pool(pool.name) for s in ship.seats.values()
                 if pool.name in s.pools and s.for_pool(pool.name).harness == name), None)
    run = secrets.token_hex(2)
    seat_name, session_name = f"check-{name}-{run}", f"qd-check-{name}-{run}"
    spec = SeatSpec(seat_name, session_name, "worker", name, (pool.name,), model=model if model is not None else like.model if like else None,
                    effort=like.effort if like else None, cwd=ship.workspace, sandbox=like.sandbox if like else None,
                    model_mode=like.model_mode if like else None, effort_mode=like.effort_mode if like else None)
    orders: list[post.Message] = []
    with temporary_seat(ship, tmux, spec, orders, "qd check", installed_version=version, check_inbox=True) as seat:
        options = {'requested_model': model} if model is not None else {}
        if minimal:
            options['minimal'] = True
        checks += probe(seat.ship, seat, orders, **options)
        if selected is not None:
            launched = seat.launched()
            ran = launched is not None and str(selected) in launched.argv
            checks.append(ProbeCheck("the seat ran the program doctor shows", ran,
                                     f"{selected} in launch.json" if ran else f"{selected} not in the seat's launch", 'program'))
    if not all(c.passed is True for c in checks):
        return checks, version
    if minimal:
        checks.append(Check('minimal: launch, task and ended turn proved', True))
        return checks, version
    inapplicable = harness.waking_inapplicability
    if inapplicable is not None:
        checks.append(Check('human-session waking line', None, f'not applicable: {inapplicable}'))
    else:
        human_spec = SeatSpec(f"check-wake-{name}-{run}", f"qd-check-wake-{name}-{run}", "worker", name,
                              (pool.name,), model=model if model is not None else like.model if like else None,
                              effort=like.effort if like else None, cwd=ship.workspace,
                              sandbox=like.sandbox if like else None)
        human_orders: list[post.Message] = []
        try:
            with temporary_seat(ship, tmux, human_spec, human_orders, "qd check human wake", human=True, check_inbox=True) as human_seat:
                try:
                    checks += [ProbeCheck(c.name, c.passed, c.detail, 'wake') for c in probe_wake(human_seat, human_orders)]
                finally:
                    wake = human_seat.folder / 'human-wake.json'
                    if wake.exists():
                        evidence = ship.runtime / 'check' / f'{human_seat.name}-human-wake.json'
                        evidence.parent.mkdir(parents=True, exist_ok=True)
                        durable.replace_file(evidence, wake.read_text())
        except (OSError, ShipError, TmuxError, ValueError) as error:
            checks.append(ProbeCheck('human-session waking line', False, f'wake proof obstructed: {error}', 'wake'))
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
    return checks, version


def model_confirmation(seat: Seat, requested: str, *, require_record: bool = False) -> ProbeCheck:
    """The requested launch model, confirmed by owned record first, screen second."""
    record = seat.record()
    try:
        state = seat.harness.state(record) if record else None
    except OSError:
        state = None
    recorded = state.model if state else None
    screen = Door(seat).screen()
    shown = screen.shows.get("model") if screen else None
    observed = recorded or shown
    launched = seat.spec.model
    passed = (launched == requested and seat.harness.model_matches(requested, observed) is True
              and (not shown or seat.harness.screen_model_consistent(requested, shown, screen.shows.get("effort")) is True)
              and (not require_record or recorded is not None))
    detail = (f"requested {requested} · launched {launched or '?'} · record-observed {recorded or '?'}"
              + (f" · screen-observed {shown}" if shown else ""))
    return ProbeCheck("requested model in record" if require_record else "requested launch model", passed, detail, 'model')


def _probe_record_check(seat: Seat, requested_model: str | None, ended: bool) -> ProbeCheck:
    record = seat.record()
    try:
        state = seat.harness.state(record) if record else None
    except OSError:
        state = None
    turns_shown = state.turns if state and state.turns is not None else "?"
    return ProbeCheck("record joined, the turn ended and read", bool(ended),
                      f"{record.name if record else 'no record'} · {turns_shown} turns · "
                      f"requested {requested_model or 'native default'} · launched {getattr(getattr(seat, 'spec', None), 'model', None) or 'native default'} · "
                      f"record-observed {state.model if state and state.model else '?'}", 'record')


_PROOF_SLACK = 8  # a newline and light padding; anything longer is not the one line the task asked for


def proof_verdict(path: Path, expected: str) -> tuple[bool, str]:
    """Whether ``path`` is a regular file whose text, stripped, is ``expected``.

    The folder is opened with ``O_DIRECTORY|O_NOFOLLOW`` and the proof from that descriptor, so a link at
    either name is not followed. A folder swapped after the check fails closed. The report is only
    ``matched``, ``did not match`` or ``not a regular file``: the file's bytes are never part of it.
    """
    try:
        folder = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return False, "not a regular file"
    try:
        try:
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                                 dir_fd=folder)
        except FileNotFoundError:
            return False, "did not match"
        except OSError as error:
            if error.errno == errno.ELOOP:  # the final component is a symlink
                return False, "not a regular file"
            return False, "did not match"
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                return False, "not a regular file"
            bound = len(expected.encode("utf-8")) + _PROOF_SLACK
            if info.st_size > bound:
                return False, "did not match"
            raw = os.read(descriptor, bound + 1)
        finally:
            os.close(descriptor)
    finally:
        os.close(folder)
    if len(raw) > bound:
        return False, "did not match"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False, "did not match"
    if text.strip() != expected:
        return False, "did not match"
    return True, "matched"


def proof_check(path: Path, expected: str) -> ProbeCheck:
    """The probe row for a proof file: passed or not, and nothing of what the file held."""
    passed, detail = proof_verdict(path, expected)
    return ProbeCheck("task: the named file holds the given line", passed, detail, "file")


def _task_body(proof_file: Path, proof_line: str, *, minimal: bool = False) -> str:
    body = (f"Write the file {proof_file} containing exactly one line: {proof_line}\n"
            f"Then post your report on this order with the body: written")
    if minimal:
        return body
    laws = (standing.FRAMEWORK_DIR / "laws.md").read_text().strip()
    return body + ("\n\nThe rest of this order is the laws you already run under, repeated so that the message is long "
                   f"enough to fold in your box; nothing in it changes the task.\n\n{laws}")


def closing_report(seat: Seat, store: post.Store, order: post.Message) -> post.Message | None:
    """File this owned check seat's report if no service owns filing, then read it.

    Both ordinary and waking probes retain their original patience. Only the
    current temporary seat's drop is selected; all admission stays in file_drops.
    """
    post.file_drops(store, seat.ship, seats=frozenset({seat.name}), service_absent=True)
    return store.closing_report(order)


def probe(ship: Ship, seat: Seat, orders: list[post.Message] | None = None, *, requested_model: str | None = None,
          minimal: bool = False) -> list[Check]:
    sandbox.ensure_check_proof_folder(ship.runtime, seat.name)  # a link refuses the check before any launch
    checks: list[Check] = []
    proof_line = f"qd-check-{secrets.token_hex(4)}"
    proof_file = sandbox.check_proof_file(ship.runtime, seat.name)
    launched = seat.up(resume=False)
    checks.append(ProbeCheck("launch", seat.is_up, launched, 'launch'))
    door = Door(seat)
    opened = wait_for(lambda: ready_for_probe(seat, door), PATIENCE if seat.harness.opening_prompt_turn else PATIENCE / 3)
    for done in getattr(seat.launched(), "answered", None) or []:
        checks.append(ProbeCheck(f"answered for the account holder: {done.get('question')}", done.get("outcome") == "cleared",
                                 f"{done.get('choice')} pressed at {done.get('at')} · {done.get('outcome')}", 'answer'))
    checks.append(ProbeCheck("door open at start", bool(opened), str(door.reading()) +
                             (" · opening brief turn ended in the record" if opened and seat.harness.opening_prompt_turn else ""), 'door'))
    if requested_model is not None:
        confirmed = model_confirmation(seat, requested_model)
        checks.append(confirmed)
        if not confirmed.passed:
            return checks
    if not opened:
        if reason := account_question(door):
            checks.append(ProbeCheck('account-holder question', False, check_outcome.REASONS[reason][1], reason))
        return checks
    try:
        initial = seat.harness.state(record) if (record := seat.record()) else None
        before_turns = initial.turns if initial and initial.turns is not None else 0
    except OSError:
        before_turns = 0  # a minimum bound only; the later receipt and ended task are still required
    store, attempts = post.Store(ship.runtime), post.Attempts(ship.runtime)
    order = store.new("order", "ship", seat.name,
                      _task_body(proof_file, proof_line, minimal=minimal),
                      subject="qd check minimal" if minimal else "qd check")
    if orders is not None:
        orders.append(order)
    text = post.typed([order])
    attempt = None
    try:
        bound = pending.current_identity(seat)
        if bound is None:
            raise pending.NoCurrentLaunch(f'{seat.name}: current pane/process/launch unproved; no delivery journal opened')
        attempt = attempts.open(order, seat.name, bound['pane'], bound['pid'], bound['launch'])
        proof = pending.Proof.begin(seat, attempt, text)
    except pending.NoCurrentLaunch as error:
        if attempt is not None:
            attempts.close(attempt, 'withheld', 'launch unproved before probe reservation; no input typed')
        checks.append(ProbeCheck('paste held: current launch unconfirmed', False, str(error), 'paste'))
        checks.append(_probe_record_check(seat, requested_model, False))
        return checks
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
    checks.append(ProbeCheck(f"a {len(text)}-character paste matched the box as the program shows it, submitted, confirmed",
                             taken, route, 'paste'))
    if not taken:
        if reason := account_question(door):
            checks.append(ProbeCheck('account-holder question', False, check_outcome.REASONS[reason][1], reason))
        return checks
    closed = wait_for(lambda: closing_report(seat, store, order), PATIENCE)
    checks.append(ProbeCheck("task: the report closed the order", closed is not None,
                             f"report {closed.short}: {closed.body[:40]!r}" if closed else "no report within the patience", 'report'))  # law: can-post
    checks.append(proof_check(proof_file, proof_line))
    def _turn_ended() -> bool:
        if (r := seat.record()) is None:
            return False
        try:
            s = seat.harness.state(r)
        except OSError:
            return False
        return s.turns is not None and s.turns >= before_turns + 1 and s.turn_open is False

    ended = wait_for(_turn_ended, 60)
    checks.append(_probe_record_check(seat, requested_model, bool(ended)))
    if requested_model is not None:
        checks.append(model_confirmation(seat, requested_model, require_record=True))
    return checks


def probe_wake(seat: Seat, orders: list[post.Message]) -> list[Check]:
    """Retain private observations before the check's temporary home is retired."""
    observed = dict(reason='human composer not ready', first_delivery=None)
    checks = []
    try:
        checks = _probe_wake(seat, orders, observed)
        observed['checks'] = [dict(name=item.name, passed=item.passed) for item in checks]
        if checks:
            observed['reason'] = checks[-1].detail[:512]
            observed['reason_truncated'] = len(checks[-1].detail) > 512
        return checks
    except BaseException:
        observed['probe_outcome_unknown'] = True
        raise
    finally:
        try:
            retain_wake_evidence(seat, observed)
        except Exception:
            # An evidence failure never replaces an earlier probe error/interrupt.
            checks.append(Check('human-session waking line: runtime evidence retained', False,
                                'private waking evidence could not be retained; outcome unknown'))


def _probe_wake(seat: Seat, orders: list[post.Message], observed: dict) -> list[Check]:
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

    def empty():
        watch = post.watch_reading(seat.ship.runtime, seat.name)
        if watch.armed is not False:
            observed['reason'] = watch.reason or 'watch already armed; wake precondition not met'
            return False
        screen = door.screen()
        reading = door._reading(screen)
        observed['reason'] = reading.reason
        if screen is not None and screen.box == '' and screen.working is False and not screen.queued and reading.open:
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

    def first_delivery(delivery, shown):
        if observed['first_delivery'] is None:
            observed['first_delivery'] = dict(result=delivery.result, detail=delivery.detail[:256],
                detail_truncated=len(delivery.detail) > 256,
                shown=shown[:256] if isinstance(shown, str) else None,
                shown_truncated=isinstance(shown, str) and len(shown) > 256)

    def taken():
        reason = human_wake.run(seat, observe_delivery=first_delivery)
        observed['reason'] = reason or ''
        proof = human_wake.load(seat)
        observed['phase'] = proof.data['phase'] if proof is not None else None
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
    report = wait_for(lambda: closing_report(seat, store, order), PATIENCE) if read else None
    checks.append(Check('human-session waking line: seat answered the message',
                        bool(report and report.body.strip() == token),
                        'answer closed the waiting order' if report and report.body.strip() == token else
                        'matching answer not received'))
    return checks


def retain_wake_evidence(seat: Seat, observed: dict) -> None:
    """No second native call or cross-record search; public Check text has no content."""
    from .harness.check_evidence import MAX_BYTES, MAX_PAIRS, unknown
    from .turn_failure import metadata

    tools = unknown('temporary seat pinned record unavailable')
    try:
        launch = seat.launched()
        pane = seat.pane()
        named = metadata(seat.folder / 'record').strip()
        record = Path(named)
        if (not named or not record.is_absolute() or seat.home not in record.parents
                or record.resolve() != record or launch is None or pane is None or pane.dead
                or (pane.id, pane.pid) != (launch.pane, launch.pid)
                or record_owner.launch_reader(launch) != seat.harness.name
                or record_owner.pinned_reader(seat, record) != seat.harness.name):
            raise ValueError('unproved temporary record ownership')
        stamp = record_fingerprint(record)
        tools = seat.harness.check_tool_evidence(record)
        if (seat.launched() != launch or metadata(seat.folder / 'record').strip() != named
                or record_fingerprint(record) != stamp):
            tools = unknown('temporary record changed during tool evidence reading')
    except Exception:
        tools = unknown('temporary seat tool evidence unreadable or unsupported')
    if (not isinstance(tools, dict) or not isinstance(tools.get('pairs'), list)
            or len(tools['pairs']) > MAX_PAIRS):
        tools = unknown('adapter tool evidence exceeded its shape bound')
    try:
        raw = json.dumps(tools, ensure_ascii=True, sort_keys=True) + '\n'
        if len(raw.encode('utf-8')) > MAX_BYTES:
            tools = unknown('adapter tool evidence exceeded 32 KiB bound')
    except (ValueError, TypeError, RecursionError):
        tools = unknown('adapter tool evidence shape unreadable')
    raw = json.dumps(tools, ensure_ascii=True, sort_keys=True) + '\n'
    final = dict(observed)
    try:
        proof = human_wake.load(seat)
        watch = post.watch_reading(seat.ship.runtime, seat.name)
        final.update(phase=proof.data['phase'] if proof is not None else None,
                     watch_armed=watch.armed, watch_generation=watch.generation,
                     watch_reason=watch.reason)
    except Exception:
        final['observation_unknown'] = True
    evidence = dict(format=1, seat=seat.name, first_delivery=final.pop('first_delivery', None),
                    final_observation=final,
                    first_delivery_field_limit=256, tool_pair_limit=MAX_PAIRS, tool_byte_limit=MAX_BYTES)
    text = json.dumps(evidence, ensure_ascii=True, sort_keys=True) + '\n'
    if len(text.encode('utf-8')) > 8192:
        evidence = dict(format=1, seat=seat.name, first_delivery=None,
                        final_observation=dict(reason='waking observation exceeded 8 KiB bound; unknown'))
        text = json.dumps(evidence, sort_keys=True) + '\n'
    folder = seat.ship.runtime / 'check'
    folder.mkdir(parents=True, exist_ok=True)
    durable.replace_file(folder / f'{seat.name}-wake-observation.json', text)
    durable.replace_file(folder / f'{seat.name}-tool-evidence.json', raw)
