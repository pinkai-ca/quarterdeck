"""Durable evidence for a single paste/submit, never a license to resend it.

The service writes these journals; its stopped owner may be annotated by a stopper
holding the exclusive service lock. Door/watch read them without advancing
clocks or marking delivery. The original attempt time survives every restart.
"""

from __future__ import annotations

from datetime import datetime
from contextlib import contextmanager
import fcntl
import json
import locale
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Any, Iterator, Sequence, TYPE_CHECKING

from . import durable, post, pane_obstruction
from . import input_receipt
from .harness import closed_record
from .harness.json_record import RecordError
from .harness.types import Screen

if TYPE_CHECKING:
    from .door import Door
    from .seat import Seat
    from .service import Service
    from .tmux import Pane

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
        previous_phase = self.data.get("phase")
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
        if (previous_phase != merged.get('phase')
                or merged.get('phase') == 'queued' and fields.get('screen_queue') is False):
            try:
                pane_obstruction.transitioned(self.path, merged)
            except OSError:
                pass  # optional reason cleanup can neither block nor settle this journal

    @classmethod
    def begin(cls, seat: Seat, attempt: post.Attempt, text: str, *, before_paste: bool = False) -> Proof:
        path = seat.ship.runtime / "post" / "pending" / seat.name / attempt.path.name
        record = seat.record()
        proof = cls(path, {})
        original = min(a.at for a in post.Attempts(seat.ship.runtime).for_message(attempt.message))
        from .shutdown import BEFORE_PASTE, prepare, prepared
        boundary = {"paste_boundary": BEFORE_PASTE} if before_paste else {}
        if before_paste:
            boundary.update(prepare(proof))
        proof.save(seat=seat.name, identity=identity(seat), at=original,
                   since=post.now().isoformat(), payload=text, phase="prepared",
                   record=str(record) if record else None, shown=None,
                   attempts=[attempt.path.name], **boundary)
        if before_paste:
            prepared(proof)
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

    def remedy(reading: tuple[str, str, bool]) -> tuple[str, str, bool]:
        if reading[1] != "needs a human" or seat.session.human:
            return reading
        from .door import Door
        proof, _ = _submit_candidate(Door(seat), screen, seat.pane(), proofs)
        if proof is not None:
            return (reading[0] + f"; qd submit {seat.name} sends it", reading[1], reading[2])
        return reading

    from .recovery import mode_hold
    if reason := mode_hold(seat, proofs, ignored):
        unresolved = [p for p in proofs if p.data.get('recovery') and p.data['recovery']['mode'] != 'done'
                      and (ignored is None or ignored.path != p.path)]
        if unresolved and all(p.data['recovery']['mode'] == 'submitted' and p.data['phase'] in ('submitted', 'queued')
                and (seat.ship.runtime / 'post/store' / (p.data['recovery']['post'] + '.json')).is_file() for p in unresolved):
            for proof in unresolved:
                if reading := proof.reading(seat, screen):
                    return remedy(reading)  # retain the ordinary receipt/draft/600s uncertainty rule
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
        return remedy((inspection(seat, reason), 'needs a human', False))
    for proof in proofs:
        if proof.data['phase'] in TERMINAL:
            continue
        if ignored is not None and proof.path == ignored.path and proof.data["phase"] == "prepared":
            continue  # only this delivery's pre-paste check may ignore its own reservation
        if not proof.same_launch(seat) and screen and screen.box == "" and not screen.unknown and not screen.interruption:
            continue  # another empty launch is usable; the old uncertainty remains a global notice
        if reading := proof.reading(seat, screen):
            return remedy(reading)
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


def _check_paths(runtime: Path, name: str) -> tuple[Path, Path]:
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', name):
        raise ValueError('invalid temporary seat name')
    folder = runtime / 'post' / 'transient'
    return folder / f'{name}.lock', folder / f'{name}.check.json'


def _owner_start(pid: int) -> str | None:
    """Optional host diagnostic only; registration-lease authority needs no PID view."""
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return fields[19] if len(fields) > 19 and fields[19].isdigit() else None
    except (OSError, ValueError, IndexError, UnicodeError):
        return None


@contextmanager
def check_inbox_registration(runtime: Path, name: str, human: bool, *, enabled=False):
    """Hold an exclusive registration lease inside the existing temporary lifetime."""
    if not enabled:
        yield
        return
    from .turn_failure import metadata
    lock, marker = _check_paths(runtime, name)
    if type(human) is not bool:
        raise ValueError('unknown temporary human ownership')
    info = lock.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('temporary check lock unknown')
    descriptor = os.open(marker, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    published = False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        registration = os.fstat(descriptor)
        data = dict(format=1, operation='check', runtime=str(runtime.resolve()), seat=name,
                    human=human, pid=os.getpid(), start=_owner_start(os.getpid()),
                    lock=[info.st_dev, info.st_ino],
                    registration=[registration.st_dev, registration.st_ino], token=secrets.token_hex(16))
        raw = json.dumps(data, sort_keys=True) + '\n'
        remaining = raw.encode('utf-8')
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError('temporary check registration write incomplete')
            remaining = remaining[written:]
        os.fsync(descriptor)
        durable.sync_folder(marker.parent)
        published = True
        yield
    finally:
        try:
            if published:
                with post.admission(runtime, name):
                    current = marker.lstat()
                    if ([current.st_dev, current.st_ino] != data['registration'] or metadata(marker) != raw):
                        raise ValueError('temporary check registration changed; retained')
                    marker.unlink()
                    durable.sync_folder(marker.parent)
        finally:
            os.close(descriptor)  # death or normal exit releases this lifetime's lease


def check_inbox_owner(runtime: Path, name: str) -> dict:
    """Read-only inbox admission by live kernel leases, independent of PID namespaces."""
    from .turn_failure import metadata
    from .ship import ShipError
    descriptors = []
    try:
        lock, marker = _check_paths(runtime, name)
        raw = metadata(marker)
        data = json.loads(raw)
        if (not isinstance(data, dict) or set(data) != {
                'format', 'operation', 'runtime', 'seat', 'human', 'pid', 'start', 'lock', 'registration', 'token'}
                or type(data['format']) is not int or data['format'] != 1
                or data['operation'] != 'check' or data['runtime'] != str(runtime.resolve())
                or data['seat'] != name or type(data['human']) is not bool
                or type(data['pid']) is not int or data['pid'] <= 0
                or data['start'] is not None and (not isinstance(data['start'], str) or not data['start'].isdigit())
                or not isinstance(data['token'], str) or not re.fullmatch(r'[0-9a-f]{32}', data['token'])
                or not isinstance(data['lock'], list) or len(data['lock']) != 2
                or any(type(item) is not int for item in data['lock'])
                or not isinstance(data['registration'], list) or len(data['registration']) != 2
                or any(type(item) is not int for item in data['registration'])):
            raise ValueError('invalid check registration')
        for path, key in ((marker, 'registration'), (lock, 'lock')):
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
            descriptors.append(descriptor)
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or [info.st_dev, info.st_ino] != data[key]):
                raise ShipError(f'{name}: temporary check registration or transient-lock inode replaced; inbox refused')
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass  # busy is live lease evidence; no lock is retained by this reader
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                raise ShipError(f'{name}: stale temporary check registration; inbox refused')
            current = path.lstat()
            if [current.st_dev, current.st_ino] != data[key]:
                raise ValueError('check lease inode changed during admission')
        for path, key in ((marker, 'registration'), (lock, 'lock')):
            current = path.lstat()
            if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                    or [current.st_dev, current.st_ino] != data[key]):
                raise ShipError(f'{name}: temporary check registration or transient-lock inode replaced; inbox refused')
        if metadata(marker) != raw:
            raise ValueError('check ownership changed during admission')
        return data
    except (OSError, ValueError, TypeError, KeyError, IndexError, UnicodeError):
        raise ShipError(f"{name}: no live check-owned temporary inbox; missing, stale or unknown ownership") from None
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


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
            from .shutdown import confined, never_pasted
            if never_pasted(proof.data) and confined(service.runtime, proof.path):
                proof.save(phase="cancelled", resolution="attested prepared reservation; no paste began; eligible for ordinary delivery")
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


# -- the door's one look -----------------------------------------------------------------
# Where the door cannot confirm a delivery, it looks once more by itself, record first, then the screen,
# and settles only three cases in code: the record shows the message (delivered); its exact text still sits
# in the box (submit); the box is empty, the seat idle by the one idle proof and the record lacks it (typed
# once more, same id, under the second-delivery line). Anything else stays held. The look is journalled, and
# the door never types that message again by itself.

RETYPED = "retyped by the door's one look"
# The latest results of a delivery the door could not confirm: unfinished, typed and waiting for the record,
# blind, or queued and not yet read. Every other result is settled, and its journal is never opened by the look.
UNCONFIRMED = (None, "pending", "typed", "blind", "queued")


def candidates(attempts: Sequence[post.Attempt]) -> dict[str, list[post.Attempt]]:
    """Per seat, the attempts whose message's latest result there is one the door could not confirm.

    Read from the attempts the pass already holds, in memory: a pass with nothing to look at opens nothing."""
    latest: dict[tuple[str, str], post.Attempt] = {}
    for attempt in attempts:  # oldest first: the latest attempt is the one that speaks
        latest[(attempt.seat, attempt.message)] = attempt
    found: dict[str, list[post.Attempt]] = {}
    for attempt in latest.values():
        if attempt.result in UNCONFIRMED:
            found.setdefault(attempt.seat, []).append(attempt)
    return found


def _named(seat: Seat, names: Sequence[str], known: dict[str, post.Attempt]) -> list[Proof]:
    """The journals these attempts name, and only those; one that is missing or unreadable is left to reconcile."""
    found = []
    for name in dict.fromkeys(names):
        path = seat.ship.runtime / "post" / "pending" / seat.name / name
        try:
            data = json.loads(path.read_text())
            validate(data, seat.name)
        except FileNotFoundError:
            continue  # an attempt with no journal: a watch's, or the door's before journals
        except (OSError, ValueError, TypeError, KeyError):
            continue  # reconcile and the status line already name an unreadable journal; the look holds it
        if all(n in known for n in data["attempts"]):
            found.append(Proof(path, data))
    return found


def _message_of(service: Service, proof: Proof, known: dict[str, post.Attempt]) -> post.Message | None:
    ids = {known[n].message for n in proof.data["attempts"] if n in known}
    if len(ids) != 1:
        return None  # a batch, or an attempt receipt missing: never settled by the look
    try:
        return service.store.get(ids.pop())
    except Exception:
        return None


def _in_record(seat: Seat, message: post.Message, since: str) -> str | None:
    record = seat.record()
    if record is None:
        return None
    try:
        return input_receipt.confirmed(seat.harness, record, post.delivered_forms(message), datetime.fromisoformat(since))
    except OSError:
        return "unreadable"


def _looked_before(seat: Seat, message: post.Message, attempts: Sequence[post.Attempt],
                   known: dict[str, post.Attempt]) -> bool:
    """A message the look already typed once more is never typed again by itself.

    Only the journals this message's own attempts at this seat name are opened."""
    names = [a.path.name for a in attempts if a.seat == seat.name and a.message == message.id]
    if any(known[n].detail == RETYPED for n in names):
        return True
    return any(p.data.get("look", {}).get("outcome") in ("retyped", "second delivery") for p in _named(seat, names, known))


def one_look(service: Service, seat: Seat, attempts: Sequence[post.Attempt] | None = None) -> list[str]:
    """Look once at the seat's deliveries the door could not confirm, from the attempts the pass holds."""
    from . import idle
    from .door import Door
    if attempts is None:
        attempts = service.attempts.all()
    mine = candidates(attempts).get(seat.name)
    if not mine:
        return []  # nothing unconfirmed: no journal opened, no screen read
    if seat.session.human or not seat.is_up:
        return []  # a human's session takes only its waking line
    known = {a.path.name: a for a in attempts}
    done = []
    for proof in _named(seat, [a.path.name for a in mine], known):
        data = proof.data
        if (data["phase"] in TERMINAL or "look" in data or not proof.same_launch(seat) or data.get("recovery")
                or "receipt_refresh" in data or data.get("paste_clear")):
            continue
        door = Door(seat)
        screen = door.screen()
        reading = proof.reading(seat, screen)
        if reading is None or reading[1] != "needs a human":
            continue  # still within its ordinary wait: the look is only for what the door cannot confirm
        message = _message_of(service, proof, known)
        if message is None:
            proof.save(look=dict(at=post.now().isoformat(), outcome="held", saw="not one message"))
            continue
        at = post.now().isoformat()
        if _looked_before(seat, message, attempts, known):
            proof.save(look=dict(at=at, outcome="held", saw="typed once more already; never again by itself"))
            continue
        seen = _in_record(seat, message, data["since"])
        if seen == "delivered":
            proof.save(look=dict(at=at, outcome="delivered", saw="the record shows it"))
            proof.finish("delivered")
            for name in data["attempts"]:
                service.attempts.close(known[name], "delivered", "the record shows it at the door's one look")
            done.append(f"{message.short} to {seat.name}: delivered (the record shows it)")
            continue
        clear = screen is not None and not screen.unknown and not screen.interruption and not screen.starting
        pane = seat.pane()
        if (seen is None and clear and pane is not None and screen.box
                and door.matches(seat.harness.box(data["payload"]), screen)):
            proof.save(look=dict(at=at, outcome="submitted", saw="its exact text in the box"),
                       phase="submitted", shown=screen.box)
            door.submit(pane.id)
            done.append(f"{message.short} to {seat.name}: submitted from the box; the record confirms it")
            continue
        if seen is None and clear and screen.box == "" and idle.reading(seat)[1] is None:
            proof.save(look=dict(at=at, outcome="retyped", saw="an empty box, an idle seat, not in the record"),
                       phase="cancelled", resolution=RETYPED)
            for name in data["attempts"]:
                service.attempts.close(known[name], "blind", RETYPED)
            proof_line = service.deliver(seat, [message], resend=True)
            # The second delivery's journal is named by its new attempt: only this message's journals are listed.
            folder = seat.ship.runtime / "post" / "pending" / seat.name
            for path in sorted(folder.glob(f"{message.id}-*.json")):
                if path == proof.path:
                    continue
                later = Proof(path, json.loads(path.read_text()))
                if "look" not in later.data:
                    later.save(look=dict(at=at, outcome="second delivery", of=proof.path.name))
            done.append(f"{message.short} to {seat.name}: typed once more ({proof_line})")
            continue
        proof.save(look=dict(at=at, outcome="held", saw=reading[0][:200]))
    return done


def _submit_candidate(door: Door, screen: Screen | None, pane: Pane | None,
                      proofs: Sequence[Proof] | None = None) -> tuple[Proof | None, str]:
    """The read-only eligibility check shared by qd submit and held readings."""
    seat = door.seat
    if seat.session.human:
        return None, f"refused: {seat.name} is in a human's session; nothing is pressed there"
    if screen is None or pane is None or screen.box is None or screen.unknown or screen.interruption:
        return None, f"refused: {seat.name}'s box cannot be read" + (f" ({screen.unknown})" if screen and screen.unknown else "")
    if screen.box == "":
        return None, f"refused: {seat.name}'s box is empty"
    try:
        proofs = [p for p in (active(seat) if proofs is None else proofs)
                  if p.data["phase"] not in TERMINAL and p.same_launch(seat)]
    except (RecordError, OSError, ValueError, KeyError):
        return None, f"refused: {seat.name}'s pending journal is unreadable"
    matching = [p for p in proofs if door.matches(seat.harness.box(p.data["payload"]), screen)]
    if len(matching) != 1:
        shown = screen.box if len(screen.box) <= 80 else screen.box[:77] + "..."
        return None, f"refused: {seat.name}'s box shows {shown!r}, not exactly a pending message of {seat.name}"
    return matching[0], ""


def submit(service: Service, seat: Seat) -> str:
    """qd submit: Enter on the box only when it holds exactly one of the seat's pending messages."""
    from .door import Door
    if seat.session.human:
        return f"refused: {seat.name} is in a human's session; nothing is pressed there"
    door = Door(seat)
    screen, pane = door.screen(), seat.pane()
    proof, refusal = _submit_candidate(door, screen, pane)
    if proof is None:
        return refusal
    known = {a.path.name: a for a in service.attempts.all()}  # one load, on a request, never a pass
    message = _message_of(service, proof, known)
    proof.save(phase="submitted", shown=screen.box,
               look=dict(at=post.now().isoformat(), outcome="submitted", saw="qd submit"))
    door.submit(pane.id)
    door.proof = proof
    delivery = door.confirm(proof.data["payload"])
    proof.finish(delivery.result)
    if delivery.result in ("delivered", "queued"):
        for name in proof.data["attempts"]:
            if name in known:
                service.attempts.close(known[name], delivery.result, "submitted from the box by qd submit")
    label = message.short if message else "its pending message"
    return f"{label} to {seat.name}: submitted from the box · {delivery}"
