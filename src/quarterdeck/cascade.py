"""A service-relayed ring: one durable run and growing notes file per question.

Posts keep their ordinary format and closure rules. A runtime journal saves immutable
publication intents before posting, and byte/hash append intents before writing notes.
It is never a second transport: ordinary post delivery still goes through the door.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from dataclasses import asdict
from pathlib import Path

from . import durable, post
from .ship import Cascade, Ship, ShipError

NOTE = "## Cascade note"
ANSWER = "## Answer"
EFFORTS = ("low", "medium", "high")


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
    ring = ship.cascade
    for name in (*ring.seats, ring.orchestrator):
        spec = ship.seat(name)
        if ship.session_of(spec).human:
            raise ShipError(f"cascade: {name} must run in a crew session")
        if spec.fresh == "after-order":
            raise ShipError(f"cascade: {name} must keep its conversation between laps; remove fresh=after-order")
    providers = {ship.pool(ship.seat(name).pools[0]).provider for name in ring.seats}
    synthetic = all(ship.seat(name).harness == "fake" for name in ring.seats)
    if len(providers) != 3 and not synthetic:
        raise ShipError("cascade: the three ring seats must start on three providers (fake drydock excepted)")


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
    data = json.loads((path / "state.json").read_text())
    if not isinstance(data, dict) or data.get("format") != 1 or data.get("question") != path.name:
        raise ShipError("cascade: unknown or mismatched run journal; retained")
    return data


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
        data = load(path)
        if levels != data["efforts"]:
            raise ShipError("cascade: this question already started with different laps")
        return proof(path, data)
    if not store.is_open(question):
        raise ShipError("cascade: the question is already answered")
    for existing in (ship.runtime / "ring").glob("*/state.json"):
        if load(existing.parent)["status"] == "running":
            raise ShipError("cascade: another question is running; wait for its final notice")
    path.mkdir(parents=True, exist_ok=True)
    durable.sync_folder(path.parent)
    durable.sync_folder(ship.runtime)
    data = dict(format=1, question=question.id, declaration=asdict(ring), efforts=levels,
                status="running", steps=[], intents={}, last=None, reason=None,
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
    def __init__(self, ship: Ship, store: post.Store, health, effort):
        self.ship, self.store = ship, store
        self.health, self.effort = health, effort

    def publish(self, path: Path, data: dict, key: str, kind: str, to: str, body: str) -> post.Message:
        intents = data["intents"]
        if key not in intents:
            message = self.store.prepare(kind, post.SHIP, to, body, re=data["question"])
            intents[key] = json.loads(message.to_json())
            save(path, data)
        message = post.Message.from_json(json.dumps(intents[key]))
        target = self.store.dir / f"{message.id}.json"
        if target.exists():
            if post.Message.from_json(target.read_text()) != message:
                raise ShipError("cascade: immutable publication intent conflicts with post; retained")
        else:
            self.store.write(message)
        return message

    @staticmethod
    def append(path: Path, data: dict) -> None:
        evidence = data["notes"]
        pending = evidence["pending"]
        if pending is None:
            return
        target = path / "responses.md"
        raw = target.read_bytes() if target.exists() else b""
        offset, addition = evidence["size"], pending.encode("utf-8")
        if (hashlib.sha256(raw[:offset]).hexdigest() != evidence["sha256"] or len(raw) < offset
                or not addition.startswith(raw[offset:]) or len(raw) > offset + len(addition)):
            raise ShipError("cascade notes changed outside their append intent; retained")
        descriptor = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            view = memoryview(addition[len(raw) - offset:])
            while view:
                view = view[os.write(descriptor, view):]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        durable.sync_folder(path)
        complete = raw[:offset] + addition
        evidence.update(size=len(complete), sha256=hashlib.sha256(complete).hexdigest(), pending=None)
        save(path, data)

    def finish(self, path: Path, data: dict, reason: str | None) -> None:
        if data["status"] == "running":
            data.update(status="stopped" if reason else "done", reason=reason)
            save(path, data)
        last = self.store.get(data["last"]).body if data["last"] else "No ring answer completed."
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
        body = (f"Cascade {data['status']}" + (f": {data['reason']}" if data["reason"] else ".")
                + outstanding + f"\nNotes: {where}" + effort_summary
                + "\nRead the notes and the last full answer below, then answer the original question once. "
                  "Include unraised actual/unknown effort and its limitations in your conclusion; requested levels "
                  "are not proof of escalation. This notice does not close that question.\n\nLast full answer:\n" + last)
        self.publish(path, data, "final", "report", data["declaration"]["orchestrator"], body)

    def advance(self, path: Path) -> None:
        data = load(path)
        if data["status"] != "running":
            self.finish(path, data, data["reason"])
            return
        ring = data["declaration"]
        if self.ship.cascade is None or json.loads(json.dumps(asdict(self.ship.cascade))) != ring:
            self.finish(path, data, "cascade declaration changed")
            return
        try:
            self.append(path, data)
        except (OSError, ShipError) as error:
            self.finish(path, data, f"notes append unavailable: {error}")
            return
        steps = data["steps"]
        total = len(ring["seats"]) * len(data["efforts"])
        if steps and "report" not in steps[-1]:
            current = steps[-1]
            order = self.store.get(current["order"]) if current.get("order") else None
            report = self.store.closing_report(order) if order else None
            if report is not None:
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
                if reason := self.health(current["seat"], order):
                    self.finish(path, data, f"{current['seat']}: {reason}")
                    return
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
            self.finish(path, data, f"{current['seat']}: {reason}")
            return
        self.dispatch(path, data, current)

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


def health(service, name: str, order: post.Message | None = None) -> str | None:
    """Common positive seat failure, or a recorded failure tied to this step's taking."""
    from . import group_post, thread_context
    from .tmux import TmuxError

    try:
        if reason := group_post.current_seat_failure(service, name):
            return reason
        if order is None:
            return None  # order-free preflight cannot associate an old failed turn
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

            def has_order(items):
                return order.id in thread_context.consumed_ids(
                    service.store, name, seat.spec.role, items)

            associated = has_order(failure.inputs)
            if not associated:
                # Accepted inputs are direct takings; consumed queue entries retain
                # their acceptance time, so their later consumed_at is the taking.
                taken = [item.at if item.kind == 'accepted' else item.consumed_at
                         for item in seat.harness.inputs(record) if has_order([item])]
                failed = [turn for turn in state.completed_turns[-1:] if turn.failure == failure
                          and turn.started is not None]
                associated = bool(taken and any(turn.started >= max(taken) for turn in failed))
            if seat.launched() != launched or seat.record() != record:
                return None
            return failure.kind if associated else None  # category only, never a provider body
    except (ShipError, OSError, TmuxError, ValueError, TypeError):
        return None  # absent, transitioning or unreadable evidence is not an early end


def effort(service, name: str, wanted: str) -> dict:
    from .watch import running_settings
    seat = service.seat(name)
    actual = running_settings(seat).get("effort", "unknown")
    detail = "Already at the requested effort, observed in the program."
    if actual != wanted:
        control = seat.harness.control("effort")
        if seat.spec.pool_choices or control is None or control.session is None:
            detail = "No proven in-session effort control; kept current effort."
        elif seat.is_working():
            detail = "Previous turn still open; kept current effort."
        else:
            result = service.control(seat, "effort", wanted)
            if result is not None and " · confirmed by " in result:
                actual, detail = wanted, "Effort applied through the existing control and confirmed by the program."
            else:
                actual = running_settings(seat).get("effort", "unknown")
                detail = "Effort control not confirmed; kept observed effort without retry."
    return dict(requested=wanted, actual=actual, detail=detail)


def run(service) -> None:
    runner = Runner(service.ship, service.store, lambda name, order: health(service, name, order),
                    lambda name, wanted: effort(service, name, wanted))
    for state in sorted((service.runtime / "ring").glob("*/state.json")):
        path = folder(service.runtime, state.parent.name)
        runner.advance(path)
