"""Durable evidence for a single paste/submit, never a license to resend it.

Only the service writes these journals. Door/watch read them without advancing
clocks or marking delivery. The original attempt time survives every restart.
"""

from __future__ import annotations

from datetime import datetime
from contextlib import contextmanager
import fcntl
import json
import locale
import re
from pathlib import Path
from typing import Any, Iterator, Sequence, TYPE_CHECKING

from . import durable, post
from . import input_receipt
from .harness import closed_record
from .harness.json_record import RecordError
from .harness.types import Screen

if TYPE_CHECKING:
    from .seat import Seat
    from .service import Service

WAIT_SECONDS = 600  # intervention policy, not a claim about normal program latency
TERMINAL = {"delivered", "cancelled", "carried", "abandoned", "withheld", "unacted", "uncertain", closed_record.PHASE}
# unacted: the record ended its turn in an error with proved no model output; uncertain: output unknown.
# Neither is ever typed again by a tick (law: one-door).
UNSETTLED = {
    "unacted": "turn ended in a recorded error before any model output; not received; carried at the next start",
    "uncertain": "turn ended in a recorded error and its model output is unknown; never resent; inspect the record",
}


def identity(seat: Seat) -> dict[str, str | int | None]:
    launch, pane = seat.launched(), seat.pane()
    return {"pane": pane.id if pane else None, "pid": pane.pid if pane else None,
            "launch": launch.at if launch else None, "harness": seat.harness.name}


def inspection(seat: Seat, reason: str, attempts: Sequence[str] = ()) -> str:
    """Only a generated attempt name supplies a short ID; other basenames remain unknown."""
    labels = []
    for name in attempts:
        match = re.fullmatch(r"((?:ord|rep|que|ans|ack)-[0-9]{8}-[0-9]{4}-.+-[0-9a-f]{4})-[1-9][0-9]*\.json", name)
        labels.append(post.short_of(match[1]) if match else f"attempt {name!r}: message ID unknown")
    references = ", ".join(dict.fromkeys(labels))
    identity = f"{seat.name}, {references}" if references else seat.name
    return f"{reason} ({identity}); inspect with qd watch {seat.name}"


class Proof:
    def __init__(self, path: Path, data: dict[str, Any]) -> None:
        self.path, self.data = path, data

    def save(self, **fields: Any) -> None:
        merged = {**self.data, **fields}
        text = json.dumps(merged, indent=2) + "\n"
        encoding = locale.getpreferredencoding(False)
        try:
            try:
                # Equality means only nothing to write, never durability or repair of an earlier failure.
                unchanged = self.path.read_bytes() == text.encode(encoding)
            except FileNotFoundError:
                unchanged = False
            if not unchanged:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                durable.replace_file(self.path, text, encoding=encoding)
        except OSError as error:
            error.add_note(f'pending journal: {self.path}')
            raise  # preserve the original error; the service pass records it once
        self.data = merged

    @classmethod
    def begin(cls, seat: Seat, attempt: post.Attempt, text: str) -> Proof:
        path = seat.ship.runtime / "post" / "pending" / seat.name / attempt.path.name
        record = seat.record()
        proof = cls(path, {})
        original = min(a.at for a in post.Attempts(seat.ship.runtime).for_message(attempt.message))
        proof.save(seat=seat.name, identity=identity(seat), at=original,
                   since=post.now().isoformat(), payload=text, phase="prepared",
                   record=str(record) if record else None, shown=None,
                   attempts=[attempt.path.name])
        return proof

    def same_launch(self, seat: Seat) -> bool:
        return self.data["identity"] == identity(seat)

    def finish(self, result: str) -> None:
        if result in ("unacted", "uncertain"):
            self.save(phase=result, resolution=UNSETTLED[result])
        elif result == "withheld" and self.data["phase"] == "prepared":
            self.save(phase="cancelled")
        elif result in ("delivered", "queued"):
            if result == "queued" and self.data.get("screen_queue"):
                return  # visible native queue; submitted stays unresolved until the record confirms
            self.save(phase=result)

    def confirmed(self, seat: Seat) -> str | None:
        """Only an exact owned-record input after this paste proves delivery."""
        name = self.data["identity"]["harness"]
        record = self.data["record"]
        if not record and self.same_launch(seat):
            joined = seat.record()
            if joined:
                self.save(record=str(joined))
                record = str(joined)
        if not record:
            return None
        try:
            result = input_receipt.confirmed(name, Path(record), self.data["payload"],
                                             datetime.fromisoformat(self.data["since"]))
            if result and self.data.get("screen_queue"):
                self.save(screen_queue=False)
            return result
        except OSError:
            return None  # unreadable evidence never becomes success

    def reading(self, seat: Seat, screen: Screen | None, now: datetime | None = None) -> tuple[str, str, bool] | None:
        """Return (reason, remedy, quiet) or None for a proven accepted queue."""
        phase = self.data["phase"]

        def uncertain(reason: str) -> tuple[str, str, bool]:
            return inspection(seat, reason, self.data["attempts"]), "needs a human", False

        if self.data.get('paste_clear') and phase!='withheld':
            return uncertain('paste/submit outcome uncertain: owned unsubmitted-paste cleanup unconfirmed; never resent')

        if phase == "queued" and (not self.same_launch(seat) or (screen and screen.box == "")):
            return None
        if not self.same_launch(seat):
            return uncertain("pending delivery belongs to a different pane/process/launch")
        if phase not in ("submitted", "queued"):
            return uncertain("paste/submit outcome uncertain; never resent")
        if screen is None or screen.box is None or screen.interruption or screen.unknown:
            return uncertain("pending delivery; box cannot be verified")
        if screen.box and screen.box != self.data["shown"]:
            return uncertain("pending box changed or is foreign; untouched")
        if screen.box and screen.box.startswith("[Pasted"):
            return uncertain("pending folded box is ambiguous; untouched")
        age = ((now or post.now()) - datetime.fromisoformat(self.data["at"])).total_seconds()
        if age >= WAIT_SECONDS:
            return uncertain("pending delivery exceeded 600s; not resent; box and record need inspection")
        reason = ("queued on screen; waiting for record confirmation" if self.data.get("screen_queue")
                  else "waiting to be taken" if screen.box else "waiting for record confirmation")
        return (f"{reason} ({int(max(0, age))}s of 600s)", "record confirmation", True)


def journals(seat: Seat) -> tuple[list[Proof], list[str]]:
    runtime = getattr(getattr(seat, "ship", None), "runtime", None)
    return _journals(runtime, seat.name) if runtime is not None else ([], [])


def _journals(runtime: Path | None, name: str) -> tuple[list[Proof], list[str]]:
    found, errors = [], []
    if runtime is None:
        return found, errors
    for path in sorted((runtime / "post" / "pending" / name).glob("*.json")):
        try:
            data = json.loads(path.read_text())
            validate(data, name)
            if any(not (runtime / "post" / "attempts" / n).is_file() for n in data["attempts"]):
                raise ValueError("pending attempt receipt missing")
            found.append(Proof(path, data))
        except (OSError, ValueError, TypeError, KeyError):
            errors.append(path.name)
    return found, errors


def validate(data: Any, seat: str) -> None:
    from .harness import NAMES

    identity = data["identity"]
    if (data["seat"] != seat or data["phase"] not in TERMINAL | {"prepared", "pasting", "submitting", "submitted", "queued"}
            or not isinstance(data["payload"], str) or not isinstance(identity["pane"], str)
            or not isinstance(identity["pid"], int) or identity["harness"] not in NAMES
            or not isinstance(data["attempts"], list) or not data["attempts"]
            or any(not isinstance(n, str) or Path(n).name != n for n in data["attempts"])
            or (data["record"] is not None and not isinstance(data["record"], str))
            or (data["shown"] is not None and not isinstance(data["shown"], str))):
        raise ValueError("invalid pending journal")
    if "screen_queue" in data and type(data["screen_queue"]) is not bool:
        raise ValueError("invalid screen queue evidence")
    if 'receipt_refresh' in data:
        from .receipt_refresh import validate_intent
        validate_intent(data)
    if 'paste_clear' in data:
        from .paste_refusal import validate_clear
        validate_clear(data['paste_clear'],identity)
    if 'paste_refusal' in data or data['phase']=='withheld':
        from .paste_refusal import validate as validate_refusal
        validate_refusal(data['paste_refusal'])
        if data['paste_refusal']['context']['identity']!=identity:
            raise ValueError('paste refusal belongs to a different launch')
        if data.get('paste_clear',{}).get('stage')!='empty_confirmed':
            raise ValueError('paste refusal has no confirmed empty cleanup')
    if 'recovery' in data:
        from .recovery import validate as validate_recovery
        validate_recovery(data['recovery'])
        if data['record'] is None or any(not n.startswith(data['recovery']['post'] + '-') for n in data['attempts']):
            raise ValueError('thinking-recovery reservation does not match its record/attempt')
    for stamp in (data["at"], data["since"], identity["launch"]):
        if datetime.fromisoformat(stamp).tzinfo is None:
            raise ValueError("pending journal timestamp lacks timezone")


def active(seat: Seat, include_terminal: bool = False) -> list[Proof]:
    found, errors = journals(seat)
    if errors:
        raise RecordError(inspection(seat, "pending delivery journal unreadable; never resent"))
    return [p for p in found if include_terminal or p.data["phase"] not in TERMINAL]


def held(seat: Seat, screen: Screen | None, ignored: Proof | None = None,
         wall: str | None = None) -> tuple[str, str, bool] | None:
    try:
        proofs, errors = journals(seat)
        if errors:
            raise ValueError('pending journal unreadable')
    except (OSError, ValueError, KeyError):
        return (inspection(seat, "pending delivery journal unreadable; never resent"), "needs a human", False)
    from .recovery import mode_hold
    if reason := mode_hold(seat, proofs, ignored):
        unresolved = [p for p in proofs if p.data.get('recovery') and p.data['recovery']['mode'] != 'done'
                      and (ignored is None or ignored.path != p.path)]
        if unresolved and all(p.data['recovery']['mode'] == 'submitted' and p.data['phase'] in ('submitted', 'queued')
                and (seat.ship.runtime / 'post/store' / (p.data['recovery']['post'] + '.json')).is_file() for p in unresolved):
            for proof in unresolved:
                if reading := proof.reading(seat, screen):
                    return reading  # retain the ordinary receipt/draft/600s uncertainty rule
            return (inspection(seat, reason), 'ended recovery and original native mode', True)
        if unresolved and all(p.data['recovery']['mode'] == 'submitted' and p.data['phase'] == 'delivered'
                and p.data['recovery']['error'] is None
                and (seat.ship.runtime / 'post/store' / (p.data['recovery']['post'] + '.json')).is_file() for p in unresolved):
            if wall:
                return (wall, 'quota reset or another pool', False)
            if screen is not None and screen.interruption is not None:
                return (screen.interruption.name, 'needs a human', False)
            if screen is not None and screen.box is not None and screen.box.strip():
                return ('text in the box', 'needs a human', False)
            return (inspection(seat, reason), 'ended recovery and original native mode', True)
        return (inspection(seat, reason), 'needs a human', False)
    for proof in proofs:
        if proof.data['phase'] in TERMINAL:
            continue
        if ignored is not None and proof.path == ignored.path and proof.data["phase"] == "prepared":
            continue  # only this delivery's pre-paste check may ignore its own reservation
        if not proof.same_launch(seat) and screen and screen.box == "" and not screen.unknown and not screen.interruption:
            continue  # another empty launch is usable; the old uncertainty remains a global notice
        if reading := proof.reading(seat, screen):
            return reading
    return None


@contextmanager
def transient_lock(runtime: Path, name: str) -> Iterator[bool]:
    """Hold a temporary seat's explicit lifetime, or guard its orphan cleanup.

    A temporary operation owns this lock before transport through teardown. The
    non-inheritable descriptor releases on owner exit, including a killed owner.
    Keep the empty file: unlinking could let two processes lock different inodes.
    Names alone (including check-*) and stale unlocked files establish no owner.
    """
    path = runtime / "post" / "transient" / f"{name}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True


def abandon(runtime: Path, name: str, reason: str) -> None:
    """Retain the history, but retire unresolved transport for a seat that is gone."""
    proofs, _ = _journals(runtime, name)
    for proof in proofs:
        if proof.data["phase"] not in TERMINAL:
            proof.save(phase="abandoned", resolution=reason)
    changed = post.Attempts(runtime).abandon(name, reason)
    if changed:
        path = runtime / "log" / "service.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as stream:
            for attempt in changed:
                stream.write(json.dumps({"at": post.now().isoformat(timespec="seconds"), "event": "abandoned",
                                         "message": attempt.message, "seat": name, "result": "abandoned",
                                         "detail": reason}) + "\n")


def reconcile(service: Service) -> set[Path]:
    from .seat import SelectionError

    paths = set()
    attempts = {a.path.name: a for a in service.attempts.all()}
    names = {a.seat for a in attempts.values()}
    names.update(p.name for p in (service.runtime / "post" / "pending").glob("*") if p.is_dir())
    for name in names - service.ship.seats.keys():
        with transient_lock(service.runtime, name) as unowned:
            if unowned:
                abandon(service.runtime, name, "seat no longer declared; submission unconfirmed and never resent")
        paths.update(a.path for a in attempts.values() if a.seat == name)
    for name in service.ship.seats:
        try:
            seat = service.seat(name)
        except SelectionError as error:
            service.hold_selection(error)
            paths.update(a.path for a in attempts.values() if a.seat == name)
            continue  # neither uncertainty nor receipt evidence is changed under a guessed selection
        proofs, errors = journals(seat)
        if errors:
            # Attempts without journals cannot reconstruct this seat's unreadable evidence.
            # Other intact journals (including this seat's) still get their receipts.
            paths.update(a.path for a in attempts.values() if a.seat == name)
            service.log("error", step="reconcile", seat=name, error=inspection(seat, "pending journal unreadable; never resent"), files=errors)
        for proof in proofs:
            if any(n not in attempts for n in proof.data["attempts"]):
                paths.update(a.path for a in attempts.values() if a.seat == name)
                service.log("error", step="reconcile", seat=name, error=inspection(seat, "pending attempt receipt missing; never resent", proof.data["attempts"]))
                continue
            from .receipt_refresh import recover, unfinished
            if unfinished(proof.data):
                paths.update(attempts[n].path for n in proof.data['attempts'])
                recover(service, proof)
                continue  # unfinished positive intents never enter transport/absence recovery
            phase = proof.data["phase"]
            carried = [attempts[n] for n in proof.data["attempts"]]
            if phase not in TERMINAL and all(any(a.message == old.message and a.seat == old.seat
                    and a.result == "launch" and a.launched_at != old.launched_at and a.when >= old.when
                    for a in attempts.values()) for old in carried):
                proof.save(phase="carried", resolution="carried by a later explicit launch; old submission unconfirmed")
                phase = "carried"
            result = "delivered" if phase == "delivered" else None
            if phase not in TERMINAL:
                result = proof.confirmed(seat)
                if (not result and not any(a.result in ("delivered", "launch", "watch", *post.UNSETTLED) for a in carried)
                        and closed_record.not_in_closed_record(seat, proof)):
                    proof.save(phase=closed_record.PHASE, resolution=closed_record.DETAIL)
                    phase = closed_record.PHASE
            if result:
                proof.finish(result)
            for filename in proof.data["attempts"]:
                attempt = attempts[filename]
                paths.add(attempt.path)
                if phase == closed_record.PHASE:
                    if attempt.result != closed_record.PHASE:
                        service.attempts.close(attempt, closed_record.PHASE, closed_record.DETAIL)
                elif result and attempt.result != result:
                    service.attempts.close(attempt, result, "exact payload found in owned record after paste")
                elif attempt.result is None:
                    recovered = {"submitted": "pending", "queued": "queued", "cancelled": "withheld", "withheld":"withheld", "abandoned": "abandoned",
                                 "unacted": "unacted", "uncertain": "uncertain"}.get(proof.data["phase"], "blind")
                    service.attempts.close(attempt, recovered, "recovered durable paste/submit phase; never resent")
    return paths


def explicit_resend(service: Service, seat: Seat, messages: list[post.Message], screen: Screen | None) -> None:
    """An explicit request can retire uncertainty only after the box is empty.

    It authorizes another submission, not clearing any draft or calling the old
    one delivered. Normal ticks never call this function.
    """
    if seat.session.human or screen is None or screen.box != "" or screen.unknown or screen.interruption:
        return
    requested = {m.id for m in messages}
    attempts = {a.path.name: a for a in service.attempts.all()}
    proofs = active(seat, include_terminal=True)
    for proof in proofs:
        if ('receipt_refresh' in proof.data and proof.data['phase'] != 'delivered'
                and proof.data['receipt_refresh'].get('outcome') != 'superseded'
                and any(attempts[name].message in requested for name in proof.data['attempts'])):
            raise RecordError('closed receipt refresh settlement pending; original input never resent')
    for proof in proofs:
        if proof.data['phase'] in TERMINAL:
            continue
        carried = [attempts[name] for name in proof.data["attempts"]]
        if {a.message for a in carried} <= requested:
            proof.save(phase="cancelled", resolution="explicit resend requested with empty box")
            for attempt in carried:
                service.attempts.close(attempt, "blind", "explicit resend superseded this unconfirmed submission")


def detached(seat: Seat) -> list[str]:
    """Old unresolved submissions remain visible without owning a different door."""
    try:
        return [inspection(seat, "prior launch has an unconfirmed delivery; historical record evidence required", proof.data["attempts"])
                for proof in active(seat) if not proof.same_launch(seat) and proof.data["phase"] != "queued"]
    except (OSError, ValueError, KeyError):
        return [inspection(seat, "pending delivery journal unreadable; never resent")]
