"""A service-relayed ring: one durable run and growing notes file per question.

Posts keep their ordinary format and closure rules. A runtime journal saves immutable
publication intents before posting, and byte/hash append intents before writing notes.
It is never a second transport: ordinary post delivery still goes through the door.
"""

from __future__ import annotations

from contextlib import nullcontext

import hashlib
import json
import re
import shlex
from dataclasses import asdict
from pathlib import Path

from . import cascade_runtime, durable, post
from .ship import Cascade, Ship, ShipError

NOTE = "## Cascade note"
ANSWER = "## Answer"
EFFORTS = ("low", "medium", "high")


class Stop(str):
    """A fixed observation and explicit policy category, never inferred from its wording."""

    category: str

    def __new__(cls, reason: str, category: str = "seat"):
        result = super().__new__(cls, reason)
        result.category = category
        return result


def declaration(table: object) -> Cascade | None:
    if table is None:
        return None
    if not isinstance(table, dict) or set(table) - {"seats", "orchestrator", "efforts", "notes", "note_chars"}:
        raise ShipError("cascade: declare seats, orchestrator, efforts, notes and note_chars only")
    seats, orchestrator = table.get("seats"), table.get("orchestrator")
    if (not isinstance(seats, list) or len(seats) != 3
            or any(not isinstance(s, str) or not s for s in seats)
            or len(set(seats)) != 3 or not isinstance(orchestrator, str) or not orchestrator
            or orchestrator in seats):
        raise ShipError("cascade: name three distinct seats in order and a separate orchestrator")
    efforts = laps(table.get("efforts", list(EFFORTS)), EFFORTS)
    limit = table.get("note_chars", 800)
    if type(limit) is not int or not 1 <= limit <= 4000:
        raise ShipError("cascade: note_chars must be an integer from 1 to 4000")
    if table.get("notes", "ring") != "ring":
        raise ShipError("cascade: notes must be ring; each question writes runtime/ring/<question>/responses.md")
    return Cascade(tuple(seats), orchestrator, tuple(efforts), "ring", limit)


def laps(value: object, allowed: tuple[str, ...]) -> list[str]:
    if (not isinstance(value, (list, tuple)) or not value
            or any(not isinstance(level, str) or level not in allowed for level in value)
            or len(set(value)) != len(value)):
        raise ShipError("cascade: efforts must be distinct declared levels, in the order to run")
    return list(value)


def validate(ship: Ship) -> None:
    from . import harness
    from .harness.capabilities import validate_effort

    ring = ship.cascade
    for name in (*ring.seats, ring.orchestrator):
        spec = ship.seat(name)
        if ship.session_of(spec).human:
            raise ShipError(f"cascade: {name} must run in a crew session")
        if spec.fresh == "after-order":
            raise ShipError(f"cascade: {name} must keep its conversation between laps; remove fresh=after-order")
        if name in ring.seats:
            for pool in spec.pools:
                selected = spec.for_pool(pool)
                for level in ring.efforts:
                    validate_effort(harness.get(selected.harness), level,
                                    context=f'cascade seat {name} pool {pool}')
    providers = {ship.pool(ship.seat(name).pools[0]).provider for name in ring.seats}
    synthetic = all(harness.get(ship.seat(name).harness).synthetic for name in ring.seats)
    if len(providers) != 3 and not synthetic:
        raise ShipError("cascade: the three ring seats must start on three providers (synthetic drydock excepted)")


def folder(runtime: Path, question: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", question):
        raise ShipError("cascade: question identity is not a post filename")
    path = runtime / "ring" / question
    if path.resolve() != runtime.resolve() / "ring" / question:
        raise ShipError("cascade: run folder must be inside the ship runtime without links")
    return path


def save(path: Path, data: dict) -> None:
    durable.replace_file(path / "state.json", json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def load(path: Path) -> dict:
    data = json.loads(cascade_runtime.read_file(path / "state.json"))
    if (not isinstance(data, dict) or type(data.get("format")) is not int or data['format'] != 1
            or data.get("question") != path.name or data.get('status') not in ('running', 'done', 'stopped')
            or not isinstance(data.get('steps'), list) or not isinstance(data.get('intents'), dict)):
        raise ShipError("cascade: unknown or mismatched run journal; retained")
    ring = declaration(data['declaration'])
    if ring is None:
        raise ShipError('cascade: run has no declaration; retained')
    levels = laps(data['efforts'], ring.efforts)
    total = 3 * len(levels)
    if len(data['steps']) > total or data.get('stop_category') not in (None, 'seat', 'provider-refusal'):
        raise ShipError('cascade: inconsistent run journal; retained')
    last = None
    for index, step in enumerate(data['steps']):
        if (not isinstance(step, dict) or step.get('seat') != ring.seats[index % 3]
                or type(step.get('lap')) is not int or step['lap'] != index // 3 + 1
                or index < len(data['steps']) - 1 and 'report' not in step):
            raise ShipError('cascade: inconsistent run step; retained')
        if 'report' in step:
            if not isinstance(step['report'], str) or 'order' not in step or 'effort' not in step:
                raise ShipError('cascade: incomplete report association; retained')
            last = step['report']
    if data['last'] != last or data['status'] == 'done' and (len(data['steps']) != total or last is None):
        raise ShipError('cascade: inconsistent completed steps; retained')
    if data['status'] == 'stopped' and data.get('stop_category') is None:
        raise ShipError('cascade: stopped run has no explicit stop category; retained')
    if data['status'] != 'stopped' and (data['reason'] is not None or data.get('stop_category') is not None):
        raise ShipError('cascade: inconsistent stop evidence; retained')
    if data['status'] == 'stopped' and (not isinstance(data['reason'], str) or not data['reason']):
        raise ShipError('cascade: stopped run has no fixed reason; retained')
    notes = data['notes']
    if (type(notes['size']) is not int or notes['size'] < 0 or not isinstance(notes['sha256'], str)
            or re.fullmatch('[0-9a-f]{64}', notes['sha256']) is None
            or notes['pending'] is not None and not isinstance(notes['pending'], str)):
        raise ShipError('cascade: inconsistent notes evidence; retained')
    return data


def _load_for_start(path: Path) -> dict:
    """A start cannot skip unknown evidence; name the file that keeps it held."""
    try:
        return load(path)
    except (ShipError, OSError, ValueError, TypeError, KeyError, IndexError, RecursionError) as error:
        if isinstance(error, json.JSONDecodeError):
            reason = f"invalid JSON: {error.msg} (line {error.lineno}, column {error.colno})"
        elif isinstance(error, UnicodeError):
            reason = f"invalid text encoding ({type(error).__name__})"
        elif isinstance(error, ShipError):
            reason = str(error).removeprefix('cascade: ').removesuffix('; retained')
        else:
            reason = f"{type(error).__name__}: {error}"
        raise ShipError(f"cascade: {path / 'state.json'}: {reason}; retained") from None


def start(service, reference: str, caller: str, efforts: list[str] | None = None) -> str:
    ship, store = service.ship, service.store
    ring = ship.cascade
    if ring is None:
        raise ShipError("no [cascade] declared in ship.toml")
    if caller not in (ring.orchestrator, ship.operator.lower()):
        raise ShipError("only the orchestrator or operator starts the cascade")
    question = store.get(reference)
    if question.kind != "question" or question.to != ring.orchestrator:
        raise ShipError("cascade: start from a question addressed to the declared orchestrator")
    levels = laps(efforts if efforts is not None else ring.efforts, ring.efforts)
    path = folder(ship.runtime, question.id)
    if (path / "state.json").exists():
        data = _load_for_start(path)
        Runner(ship, store, None, None).verify(data)
        if levels != data["efforts"]:
            raise ShipError("cascade: this question already started with different laps")
        return proof(path, data)
    if not store.is_open(question):
        raise ShipError("cascade: the question is already answered")
    for existing in (ship.runtime / "ring").glob("*/state.json"):
        if _load_for_start(existing.parent)["status"] == "running":
            raise ShipError("cascade: another question is running; wait for its final notice")
    path.mkdir(parents=True, exist_ok=True)
    data = dict(format=1, question=question.id, declaration=asdict(ring), efforts=levels,
                status="running", steps=[], intents={}, last=None, reason=None, stop_category=None,
                stopped_report=None,
                notes=dict(size=0, sha256=hashlib.sha256(b"").hexdigest(),
                           pending=f"# Cascade notes\n\nQuestion: {question.id}\n\n"))
    save(path, data)
    Runner.append(path, data)  # a successful start returns a file that tail -f can already open
    return proof(path, data)


def proof(path: Path, data: dict) -> str:
    return (f"cascade {data['question']} · {data['status']} · notes {path / 'responses.md'} · "
            + shlex.join(["tail", "-f", str(path / "responses.md")]))


def note(body: str, limit: int) -> str:
    lines = body.strip().splitlines()
    if not lines or lines[0] != NOTE or ANSWER not in lines[1:]:
        raise ShipError(f"report must begin with {NOTE}, then {ANSWER}")
    boundary = lines.index(ANSWER)
    text = "\n".join(lines[1:boundary]).strip()
    if not text or len(text) > limit or not "\n".join(lines[boundary + 1:]).strip():
        raise ShipError(f"report needs a 1–{limit} character note and a nonempty full answer")
    return text


class Runner:
    def __init__(self, ship: Ship, store: post.Store, health, effort, *, refusal_check=None, outcomes=None):
        self.ship, self.store = ship, store
        self.health, self.effort = health, effort
        self.outcomes = outcomes
        self.refusal_check = refusal_check or (lambda name, order: current_refusal(ship.runtime, name, order))

    def publish(self, path: Path, data: dict, key: str, kind: str, to: str, body: str) -> post.Message:
        intents = data["intents"]
        if key not in intents:
            message = self.store.prepare(kind, post.SHIP, to, body, re=data["question"])
            intents[key] = json.loads(message.to_json())
            save(path, data)
        message = post.Message.from_json(json.dumps(intents[key]))
        target = self.store.dir / f"{message.id}.json"
        try:
            raw = cascade_runtime.read_file(target)
        except FileNotFoundError:
            self.store.write(message)
        else:
            if post.Message.from_json(raw.decode('utf-8')) != message:
                raise ShipError("cascade: immutable publication intent conflicts with post; retained")
        return message

    @staticmethod
    def append(path: Path, data: dict) -> None:
        evidence = data["notes"]
        pending = evidence["pending"]
        if pending is None:
            return
        data['notes'] = cascade_runtime.append_notes(path, evidence)
        save(path, data)

    def finish(self, path: Path, data: dict, reason: str | None, *, category: str = 'seat') -> None:
        if category not in ('seat', 'provider-refusal'):
            raise ShipError('cascade: unknown stop category; retained')
        if data["status"] == "running":
            data.update(status="stopped" if reason else "done", reason=reason,
                        stop_category=category if reason else None)
            save(path, data)
        from .structure_outcome import clear_wait
        for step in data['steps']:
            if step.get('order'):
                clear_wait(self.ship.runtime, step['order'])
        where = path / "responses.md"
        current = data["steps"][-1] if data["steps"] else {}
        outstanding = (f"\nInterrupted order remains open: {current['order']}."
                       if current.get("order") and "report" not in current else "")
        observations = []
        for step in data["steps"]:
            observed = step.get("effort", dict(requested=data["efforts"][step["lap"] - 1], actual="unknown",
                                               detail="No completed effort observation before the stop."))
            observations.append(f"- {step['seat']} lap {step['lap']}: requested {observed['requested']}; "
                                f"actual {observed['actual']}. {observed['detail']}")
        effort_summary = "\nEffort observations and limitations:\n" + ("\n".join(observations) or "No ring effort observed.")
        if data.get('stop_category') == 'provider-refusal':
            body = (f"Cascade stopped: provider refused this step.\nSeat: {current['seat']}; lap: {current['lap']}."
                    + f"\nNotes: {where}" + effort_summary
                    + f"\nLast completed report for a person to open: {data['last'] or 'none'}."
                    + f"\nStopped step's filed report for a person to open: {data.get('stopped_report') or 'none'}."
                      "\nThe last full answer is withheld because the step that carried it was refused."
                      "\nConclude from completed notes only, naming the stop and effort limitations."
                      " Do not automatically read or retype the referenced full report or refused step input."
                      " Answer the original question once; this notice does not close it.")
            self.publish(path, data, "final", "report", data["declaration"]["orchestrator"], body)
            return
        last = self.store.get(data["last"]).body if data["last"] else "No ring answer completed."
        body = (f"Cascade {data['status']}" + (f": {data['reason']}" if data["reason"] else ".")
                + outstanding + f"\nNotes: {where}" + effort_summary
                + "\nRead the notes and the last full answer below, then answer the original question once. "
                  "Include unraised actual/unknown effort and its limitations in your conclusion; requested levels "
                  "are not proof of escalation. This notice does not close that question.\n\nLast full answer:\n" + last)
        self.publish(path, data, "final", "report", data["declaration"]["orchestrator"], body)

    def advance(self, path: Path) -> None:
        data = load(path)
        self.verify(data)
        if data["status"] != "running":
            self.finish(path, data, data["reason"])
            return
        steps = data['steps']
        if steps and 'report' not in steps[-1]:
            current = steps[-1]
            order = self.current_order(data)
            if order is not None and (stop := self.refusal_check(current['seat'], order)) is not None:
                filed = self.store.closing_report(order)
                data['stopped_report'] = filed.id if filed is not None else None
                self.finish(path, data, f"{current['seat']}: {stop}", category=stop.category)
                return  # the filed report is neither accepted, parsed nor appended
        ring = data["declaration"]
        if self.ship.cascade is None or json.loads(json.dumps(asdict(self.ship.cascade))) != ring:
            self.finish(path, data, "cascade declaration changed")
            return
        self.append(path, data)
        total = len(ring["seats"]) * len(data["efforts"])
        if steps and "report" not in steps[-1]:
            current = steps[-1]
            order = self.store.get(current["order"]) if current.get("order") else None
            report = self.store.closing_report(order) if order else None
            if report is None:
                if reason := self.health(current["seat"], order):
                    self.finish(path, data, f"{current['seat']}: {reason}",
                                category=reason.category if isinstance(reason, Stop) else 'seat')
                    return
                observation = self.outcomes(current['seat'], order) if self.outcomes else nullcontext(None)
                stopped = False
                with observation as ended:
                    report = self.store.closing_report(order) if order else None
                    if ended is not None and report is None:
                        # Save the existing terminal state while ownership and
                        # report admission are still held; publish afterward.
                        data.update(status='stopped', reason=f"{current['seat']}: {ended} (lap {current['lap']})", stop_category='seat')
                        save(path, data)
                        stopped = True
                if stopped:
                    self.finish(path, data, data['reason'])
                    return
            if report is not None:
                from .structure_outcome import clear_wait
                clear_wait(self.ship.runtime, order.id)
                if report.to != post.SHIP:
                    self.finish(path, data, f"{current['seat']}: report was not addressed to ship")
                    return
                try:
                    text = note(report.body, ring["note_chars"])
                except ShipError as error:
                    self.finish(path, data, f"{current['seat']}: {error}")
                    return
                current["report"] = report.id
                data["last"] = report.id
                state = current["effort"]
                data["notes"]["pending"] = (f"## {current['seat']} · lap {current['lap']}\n\n"
                    f"Requested effort: {state['requested']}; actual: {state['actual']}. {state['detail']}\n\n{text}\n\n")
                save(path, data)
                self.append(path, data)
            else:
                self.dispatch(path, data, current)
                return
        if len(steps) == total:
            self.finish(path, data, None)
            return
        index = len(steps)
        current = dict(seat=ring["seats"][index % 3], lap=index // 3 + 1)
        steps.append(current)
        save(path, data)
        if reason := self.health(current["seat"], None):
            self.finish(path, data, f"{current['seat']}: {reason}",
                        category=reason.category if isinstance(reason, Stop) else 'seat')
            return
        self.dispatch(path, data, current)

    def current_order(self, data: dict) -> post.Message | None:
        current = data['steps'][-1]
        if current.get('order'):
            return self.store.get(current['order'])
        intent = data['intents'].get(f"turn-{len(data['steps'])}")
        return post.Message.from_json(json.dumps(intent)) if intent is not None else None

    def verify(self, data: dict) -> None:
        """Bind saved relay evidence to the ordinary canonical question and posts."""
        question = self.store.get(data['question'])
        ring = data['declaration']
        if question.kind != 'question' or question.to != ring['orchestrator']:
            raise ShipError('cascade: question differs from the run declaration')
        expected = {f'turn-{index + 1}': ('order', step['seat'])
                    for index, step in enumerate(data['steps'])}
        if data['status'] != 'running':
            expected['final'] = ('report', ring['orchestrator'])
        for key, intent in data['intents'].items():
            message = post.Message.from_json(json.dumps(intent))
            if (key not in expected or (message.kind, message.to) != expected[key]
                    or message.sender != post.SHIP or message.re != question.id
                    or not re.fullmatch('[A-Za-z0-9_-]+', message.id)):
                raise ShipError('cascade: publication intent differs from its run')
            target = self.store.dir / (message.id + '.json')
            try:
                raw = cascade_runtime.read_file(target)
            except FileNotFoundError:
                continue  # a saved intent may precede publication
            if post.Message.from_json(raw.decode('utf-8')) != message:
                raise ShipError('cascade: immutable post differs from its publication intent')
        for index, step in enumerate(data['steps']):
            if 'order' in step:
                intent = data['intents'][f'turn-{index + 1}']
                if step['order'] != intent['id']:
                    raise ShipError('cascade: current order differs from its intent')
                self.store.get(step['order'])
            if 'report' in step:
                report = self.store.get(step['report'])
                if (report.kind != 'report' or report.sender != step['seat']
                        or report.to != post.SHIP or report.re != step['order']):
                    raise ShipError('cascade: report differs from its step order')
        if data.get('stopped_report') is not None:
            current = data['steps'][-1]
            order = self.current_order(data)
            filed = self.store.get(data['stopped_report'])
            if (data.get('stop_category') != 'provider-refusal' or 'report' in current or order is None
                    or filed.kind != 'report' or filed.sender != current['seat'] or filed.re != order.id):
                raise ShipError('cascade: stopped filed report differs from its step')

    def dispatch(self, path: Path, data: dict, current: dict) -> None:
        wanted = data["efforts"][current["lap"] - 1]
        if "effort" not in current:
            # An interrupted control is not repeated: neither typing nor confirmation is guessed.
            current["effort"] = dict(requested=wanted, actual="unknown",
                                     detail="Effort control interrupted/unconfirmed; kept current effort.")
            save(path, data)
            current["effort"] = self.effort(current["seat"], wanted)
            save(path, data)
        previous = self.store.get(data["last"]).body if data["last"] else self.store.get(data["question"]).body
        state = current["effort"]
        body = (f"Cascade lap {current['lap']} of {len(data['efforts'])}. Requested effort {wanted}; "
                f"actual {state['actual']}. {state['detail']}\nImprove the whole answer below. "
                f"Report to this order (its sender is ship), beginning with {NOTE}, a note of at most "
                f"{data['declaration']['note_chars']} characters, then {ANSWER} and the full answer. "
                "Do not address the next seat or the orchestrator.\n\nAnswer to improve:\n" + previous)
        order = self.publish(path, data, f"turn-{len(data['steps'])}", "order", current["seat"], body)
        if "order" not in current:
            current["order"] = order.id
            save(path, data)


def current_refusal(runtime: Path, name: str, order: post.Message) -> Stop | None:
    """Only the existing exact-current-order exclusion fact; no new recognition."""
    from . import refusal
    try:
        excluded = refusal.excluded(runtime, name)
    except (refusal.JournalError, OSError):
        raise cascade_runtime.ExclusionUnknown('cascade step exclusion evidence is unreadable') from None
    return Stop('provider refused this step', 'provider-refusal') if order.id in excluded else None


def associated_failure(service, name: str, order: post.Message) -> Stop | None:
    """The existing exact native association, under unchanged lifecycle/launch checks."""
    from . import thread_context
    from .tmux import TmuxError

    try:
        seat = service.seat(name)
        with seat._lifecycle(automatic=True):
            launched = seat.launched()
            record = seat.record() if launched is not None else None
            if record is None:
                return None
            state = seat.harness.state(record)
            failure = state.failure
            if failure is None or state.turn_open is not False:
                return None

            def named(items):
                return thread_context.consumed_ids(service.store, name, seat.spec.role, items)

            def has_order(items):
                return order.id in named(items)

            in_failure = named(failure.inputs)
            associated = order.id in in_failure
            # Time decides only where the failed turn's inputs say nothing either way: inputs that name
            # another message make the failure that message's, never this order's.
            if not associated and not in_failure:
                # Accepted inputs are direct takings; consumed queue entries retain
                # their acceptance time, so their later consumed_at is the taking.
                taken = [item.at if item.kind == 'accepted' else item.consumed_at
                         for item in seat.harness.inputs(record) if has_order([item])]
                failed = [turn for turn in state.completed_turns[-1:] if turn.failure == failure
                          and turn.started is not None]
                associated = bool(taken and any(turn.started >= max(taken) for turn in failed))
            if seat.launched() != launched or seat.record() != record:
                return None
            if associated:
                return (Stop('provider refused this step', 'provider-refusal') if failure.kind == 'provider refusal'
                        else Stop(failure.kind))  # category only, never a provider body
            return None
    except (ShipError, OSError, TmuxError, ValueError, TypeError):
        return None  # absent, transitioning or unreadable evidence is not an early end


def known_refusal(service, name: str, order: post.Message) -> Stop | None:
    """Both existing refusal sources, without consulting dead/limit/generic outcomes."""
    if stop := current_refusal(service.ship.runtime, name, order):
        return stop  # unknown journal still raises the existing visible-HOLD exception
    stop = associated_failure(service, name, order)
    return stop if stop is not None and stop.category == 'provider-refusal' else None


def health(service, name: str, order: post.Message | None = None) -> str | None:
    """Ordinary health reuses the same associated failure proof, with no copied match."""
    from . import group_post
    from .tmux import TmuxError

    try:
        associated = None
        if order is not None:
            try:
                if stop := current_refusal(service.ship.runtime, name, order):
                    return stop
            except cascade_runtime.ExclusionUnknown:
                return None
            associated = associated_failure(service, name, order)
            if associated is not None and associated.category == 'provider-refusal':
                return associated
        if reason := group_post.current_seat_failure(service, name):
            return Stop(reason)
        return associated
    except (ShipError, OSError, TmuxError, ValueError, TypeError):
        return None


def effort(service, name: str, wanted: str) -> dict:
    from .watch import running_settings
    seat = service.seat(name)
    actual = running_settings(seat).get("effort", "unknown")
    detail = "Already at the requested effort, observed in the program."
    if actual != wanted:
        control = seat.harness.control("effort")
        if seat.spec.pool_choices or control is None or control.session is None:
            detail = "No proven in-session effort control; kept current effort."
        elif (working := seat.is_working()) is not False:
            detail = ("Previous turn still open; kept current effort." if working is True
                      else "Current activity unknown; kept current effort.")
        else:
            result = service.control(seat, "effort", wanted)
            if result is not None and " · confirmed by " in result:
                actual, detail = wanted, "Effort applied through the existing control and confirmed by the program."
            else:
                actual = running_settings(seat).get("effort", "unknown")
                detail = "Effort control not confirmed; kept observed effort without retry."
    return dict(requested=wanted, actual=actual, detail=detail)


def run(service) -> list[tuple[str, str]]:
    from . import structure_outcome
    runner = Runner(service.ship, service.store, lambda name, order: health(service, name, order),
                    lambda name, wanted: effort(service, name, wanted),
                    refusal_check=lambda name, order: known_refusal(service, name, order),
                    outcomes=lambda name, order: structure_outcome.observe(service, name, order))
    changed = []
    for state in sorted((service.runtime / "ring").glob("*/state.json")):
        path = None
        question = state.parent.name
        try:
            path = folder(service.runtime, question)
            runner.advance(path)
            cascade_runtime.clear(service.runtime, question)
        except Exception as error:  # each retained run is independent of the others
            reason = cascade_runtime.error_reason(error)
            try:
                raw = cascade_runtime.read_file(state) if path is not None else b'unreadable'
            except (OSError, ShipError):
                raw = b'unreadable'
            signature = hashlib.sha256(raw).hexdigest()
            if cascade_runtime.hold(service.runtime, question, reason, signature):
                changed.append((cascade_runtime.key(question), reason))
    return changed
