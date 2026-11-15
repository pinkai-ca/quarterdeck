"""Durable evidence for a single paste/submit, never a license to resend it.

The service writes these journals; its stopped owner may be annotated by a stopper
holding the exclusive service lock. Door/watch read them without advancing
clocks or marking delivery. The original attempt time survives every restart.
"""

from __future__ import annotations

from datetime import datetime
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import errno
import fcntl
import json
import locale
import os
import re
import secrets
import stat
import time
from pathlib import Path
from typing import Any, Iterator, Sequence, TYPE_CHECKING

from . import durable, pane_obstruction, post, post_dir
from . import input_receipt
from .harness import closed_record
from .harness.json_record import RecordError
from .harness.types import Screen
from .ship import ShipError

if TYPE_CHECKING:
    from .door import Door
    from .seat import Seat
    from .service import Service
    from .tmux import Pane

WAIT_SECONDS = 600  # intervention policy, not a claim about normal program latency
_JOURNAL_READER = ContextVar('quarterdeck_status_journals', default=None)
TERMINAL = {"delivered", "cancelled", "carried", "abandoned", "withheld", "unacted", "uncertain", closed_record.PHASE}
# unacted: the record ended its turn in an error with proved no model output; uncertain: output unknown.
# Neither is ever typed again by a tick (law: one-door).
UNSETTLED = {
    "unacted": "turn ended in a recorded error before any model output; not received; carried at the next start",
    "uncertain": "turn ended in a recorded error and its model output is unknown; never resent; inspect the record",
}


# A paste whose box the reader could not find keeps the screen it read, for a person to see what was drawn.
SCREEN_ROWS, SCREEN_COLUMNS, SCREEN_CHARS = 120, 320, 24_000
SECRET_SHAPES = re.compile(
    r"\b(?:sk|xai|ghp|gho|ghs|ghu|github_pat|glpat)[-_][A-Za-z0-9_\-]{16,}"  # provider and forge tokens
    r"|\bAKIA[0-9A-Z]{16}\b"  # a cloud access key id
    r"|\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"  # a signed web token
    r"|(?i:\b(?:bearer|authorization|api[_-]?key|token|password|secret)\b\s*[:=]?\s*)[^\s'\"]{12,}")


def screen_evidence(screen: Screen | None) -> dict[str, Any]:
    """The screen a paste's box was read from when the reader found no box, for diagnosis alone.

    Bounded to its last rows, where the composer is drawn, each cut to a width and all to a total, with
    anything shaped like a credential masked. Nothing reads it back to decide, clear or resend.
    """
    if screen is None:
        return {"reading": "no pane captured", "rows": [], "cut": 0}
    rows = screen.text.split("\n")
    while rows and not rows[-1].strip():
        rows.pop()
    kept, total = [], 0
    for row in reversed(rows[-SCREEN_ROWS:]):
        row = SECRET_SHAPES.sub("[masked]", row)[:SCREEN_COLUMNS]
        if total + len(row) > SCREEN_CHARS:
            break
        kept.append(row)
        total += len(row)
    kept.reverse()
    reading = (screen.unknown or (screen.interruption.name if screen.interruption else None)
               or ("starting" if screen.starting else "no box"))
    return {"reading": reading, "rows": kept, "cut": len(rows) - len(kept)}


def identity(seat: Seat) -> dict[str, str | int | None]:
    launch, pane = seat.launched(), seat.pane()
    return {"pane": pane.id if pane else None, "pid": pane.pid if pane else None,
            "launch": launch.at if launch else None, "harness": seat.harness.name}


class NoCurrentLaunch(ShipError):
    """No complete current identity exists to reserve a paste against."""


def current_identity(seat: Seat) -> dict[str, str | int] | None:
    """A complete snapshot from Seat.pane's existing owned-pane view. A launch record refused is no identity."""
    try:
        launch = seat.launched()
        bound = identity(seat)
    except ShipError:
        return None
    try:
        if (launch is None or not isinstance(bound['pane'], str) or not bound['pane']
                or type(bound['pid']) is not int or bound['pid'] <= 0
                or not isinstance(bound['launch'], str)
                or datetime.fromisoformat(bound['launch']).tzinfo is None
                or seat.launched() != launch):
            return None
    except (AttributeError, TypeError, ValueError):
        return None
    return bound


def _named_message(name: str, message: object) -> str | None:
    """A diagnostic reference must agree with the exact attempt basename."""
    if not isinstance(message, str) or not re.fullmatch(r'(?:ord|rep|que|ans|ack)-[A-Za-z0-9_-]+', message):
        return None
    suffix = name.removeprefix(message + '-')
    return message if suffix != name and re.fullmatch(r'[1-9][0-9]*\.json', suffix) else None


def _legacy_message(seat: Seat, name: str) -> str | None:
    """Read only the named receipt, including one retired at its original citation."""
    try:
        path = post.attempt_path(seat.ship.runtime, name)
        attempt = post.Attempt(**json.loads(post_dir.read_text(path)))
        if attempt.seat == seat.name:
            return _named_message(name, attempt.message)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ShipError):
        pass
    return None


def inspection(seat: Seat, reason: str, attempts: Sequence[str] = (), *, message: str | None = None) -> str:
    """Journal identity first, then its named receipt; old generated basenames remain readable."""
    labels = []
    for name in attempts:
        identifier = _named_message(name, message) if message is not None else _legacy_message(seat, name)
        if identifier is None and message is None:
            match = re.fullmatch(r"((?:ord|rep|que|ans|ack)-[0-9]{8}-[0-9]{4}-.+-[0-9a-f]{4})-[1-9][0-9]*\.json", name)
            identifier = match[1] if match else None
        labels.append(post.short_of(identifier) if identifier else f"attempt {name!r}: message ID unknown")
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
                unchanged = post_dir.read_bytes(self.path) == text.encode(encoding)
            except FileNotFoundError:
                unchanged = False
            if not unchanged:
                post_dir.ensure_directory(self.path.parent)
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
    def begin(cls, seat: Seat, attempt: post.Attempt, text: str, *, before_paste: bool = False,
              attempt_history=None) -> Proof:
        bound = current_identity(seat)
        if (bound is None or attempt.seat != seat.name
                or (attempt.pane, attempt.pid, attempt.launched_at)
                != (bound['pane'], bound['pid'], bound['launch'])):
            raise NoCurrentLaunch(f'{seat.name}: current pane/process/launch unproved; no delivery journal opened')
        path = seat.ship.runtime / "post" / "pending" / seat.name / attempt.path.name
        record = seat.record()
        if current_identity(seat) != bound:
            raise NoCurrentLaunch(f'{seat.name}: launch changed before reservation; no delivery journal opened')
        proof = cls(path, {})
        history = (post.Attempts(seat.ship.runtime).for_message(attempt.message)
                   if attempt_history is None else attempt_history)
        original = min([attempt.at, *(a.at for a in history if a.message == attempt.message)])
        from .shutdown import BEFORE_PASTE, prepare, prepared
        boundary = {"paste_boundary": BEFORE_PASTE} if before_paste else {}
        if before_paste:
            boundary.update(prepare(proof))
        proof.save(seat=seat.name, message=attempt.message, identity=bound, at=original,
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
            return inspection(seat, reason, self.data["attempts"], message=self.data.get('message')), "needs a human", False

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
        if self.data.get("screen_queue") and screen.box == "":
            if seat.harness.queued_waiting(self.data["payload"], screen):
                return ("queued on screen; waiting for record confirmation", "record confirmation", True)
            # A queue entry can leave just after the service's first record read.
            # Read the owned record once more before applying the age limit; a
            # screen read never writes a receipt or advances a grace clock.
            if self.data["record"]:
                record = Path(self.data["record"])
                try:
                    receipt = input_receipt.confirmed(self.data["identity"]["harness"], record,
                        self.data["payload"], datetime.fromisoformat(self.data["since"]))
                    if receipt == "delivered":
                        return None
                    if seat.harness.queued_waiting(self.data["payload"], screen, record=record, now=now or post.now()):
                        return ("queued prompt starting; waiting for record confirmation", "record confirmation", True)
                except (OSError, ValueError):
                    pass
        age = ((now or post.now()) - datetime.fromisoformat(self.data["at"])).total_seconds()
        if age >= WAIT_SECONDS:
            return uncertain("pending delivery exceeded 600s; not resent; box and record need inspection")
        reason = ("queued on screen; waiting for record confirmation" if self.data.get("screen_queue")
                  else "waiting to be taken" if screen.box else "waiting for record confirmation")
        return (f"{reason} ({int(max(0, age))}s of 600s)", "record confirmation", True)


def journals(seat: Seat) -> tuple[list[Proof], list[str]]:
    runtime = getattr(getattr(seat, "ship", None), "runtime", None)
    if runtime is not None and (reader := _JOURNAL_READER.get()) is not None:
        return reader.journals(runtime, seat.name)
    return _journals(runtime, seat.name) if runtime is not None else ([], [])


@contextmanager
def journal_reading(reader):
    """A service presentation may reuse parsed journals, never an action's evidence."""
    token = _JOURNAL_READER.set(reader)
    try:
        yield
    finally:
        _JOURNAL_READER.reset(token)


class JournalReader:
    """Status-only parsed data; every new collection validates names and file identities."""

    def __init__(self):
        self._folders = {}

    @staticmethod
    def _stamp(info):
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    def journals(self, runtime, name):
        folder = runtime / 'post' / 'pending' / name
        found, errors = post.reading_value('journals', folder.absolute(),
                                          lambda: self._read(runtime, name, folder))
        # Proof.save and callers' nested edits must never alter cached observations.
        return [Proof(path, deepcopy(data)) for path, data in found], list(errors)

    def _read(self, runtime, name, folder):
        started = time.time_ns()
        found, errors = [], []
        try:
            descriptor = post_dir.open_directory(folder, create=False)
        except FileNotFoundError:
            self._folders.pop(folder, None)
            return found, errors
        try:
            info = os.fstat(descriptor)
            identity = info.st_dev, info.st_ino
            previous_identity, previous = self._folders.get(folder, (None, {}))
            if previous_identity != identity:
                previous = {}
            current = {}
            for filename in sorted(os.listdir(descriptor)):
                if not filename.endswith('.json'):
                    continue
                path = folder / filename
                try:
                    info = os.stat(filename, dir_fd=descriptor, follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode):
                        post_dir._link_held(path)
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    stamp = self._stamp(info)
                    cached = previous.get(filename)
                    if cached is None or cached[0] != stamp:
                        data = json.loads(post_dir._read_nofollow(descriptor, filename, path).decode(
                            locale.getpreferredencoding(False)))
                        validate(data, name)
                        # A complete replacement during the guarded read may be returned,
                        # but is cached only if it still matches the identity we inspected.
                        after = os.stat(filename, dir_fd=descriptor, follow_symlinks=False)
                        # Recent timestamps can hide a same-stamp replacement within
                        # one filesystem tick. Aging an earlier read cannot certify it.
                        if (self._stamp(after) == stamp
                                and max(stamp[-2:]) + post.POST_TIMESTAMP_TICK_NS
                                < started <= time.time_ns()):
                            current[filename] = stamp, data
                    else:
                        data = cached[1]
                        current[filename] = cached
                    # Receipts may disappear or move independently of an unchanged journal.
                    if any(not post.attempt_path(runtime, n).is_file() for n in data['attempts']):
                        raise ValueError('pending attempt receipt missing')
                    found.append((path, data))
                except FileNotFoundError:
                    if not post_dir.gone(path):
                        errors.append(path.name)
                except (OSError, ValueError, TypeError, KeyError):
                    errors.append(path.name)
            self._folders[folder] = identity, current
            return found, errors
        finally:
            os.close(descriptor)


def journal_path(runtime: Path, name: str, filename: str) -> Path:
    """Resolve a journal named by saved evidence, without scanning the archive."""
    if any(Path(part).name != part or part in ('.', '..') for part in (name, filename)):
        raise ShipError('invalid pending journal filename')
    from .post_retirement import resolve
    return resolve(runtime / 'post' / 'pending' / name / filename, runtime)


def _journals(runtime: Path | None, name: str, *, progress=None) -> tuple[list[Proof], list[str]]:
    found, errors = [], []
    if runtime is None:
        return found, errors
    for path in post_dir.json_paths(runtime / "post" / "pending" / name):
        if progress is not None:
            progress()
        try:
            data = json.loads(post_dir.read_text(path))
            validate(data, name)
            if any(not post.attempt_path(runtime, n).is_file() for n in data["attempts"]):
                raise ValueError("pending attempt receipt missing")
            found.append(Proof(path, data))
        except FileNotFoundError:
            if not post_dir.gone(path):
                errors.append(path.name)
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
    descriptor = post_dir.open_lock(path)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        os.close(descriptor)


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
    # Published whole: the record is written into an exclusive temporary beside the marker and renamed onto the
    # marker's name without replacing one (an existing marker refuses, as the exclusive create did). The lease's
    # descriptor is the temporary's own, so the registration's identity is the published file's.
    temporary = f'.{marker.name}.{secrets.token_hex(4)}.tmp'
    if post_dir.under_post(marker):
        parent, marker_name = post_dir._open_parent(marker, create=False)
        try:
            info = os.stat(lock.name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                post_dir._link_held(lock)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('temporary check lock unknown')
            descriptor = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                                 0o600, dir_fd=parent)
        except BaseException:
            os.close(parent)
            raise
    else:
        info = lock.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('temporary check lock unknown')
        parent, marker_name = os.open(marker.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC), marker.name
        try:
            descriptor = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                                 0o600, dir_fd=parent)
        except BaseException:
            os.close(parent)
            raise
    published = False
    try:
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
            try:
                post_dir._rename_noreplace(temporary, marker_name, parent, parent)
            except OSError as error:
                if error.errno == errno.EEXIST:
                    raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(marker)) from None
                raise
        except BaseException:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
            raise
        finally:
            os.close(parent)
        published = True
        yield
    finally:
        try:
            if published:
                with post.admission(runtime, name):
                    current = marker.lstat()
                    if ([current.st_dev, current.st_ino] != data['registration'] or metadata(marker) != raw):
                        raise ValueError('temporary check registration changed; retained')
                    post_dir.remove(marker)
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


def abandon(runtime: Path, name: str, reason: str, *, attempts: post.Attempts | None = None,
            inventory=None) -> None:
    """Retain the history, but retire unresolved transport for a seat that is gone."""
    proofs, _ = _journals(runtime, name)
    for proof in proofs:
        if proof.data["phase"] not in TERMINAL:
            proof.save(phase="abandoned", resolution=reason)
    reader = attempts if attempts is not None else post.Attempts(runtime)
    changed = (reader.abandon(name, reason) if inventory is None else
               reader.abandon(name, reason, attempts=inventory))
    if changed:
        path = runtime / "log" / "service.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as stream:
            for attempt in changed:
                stream.write(json.dumps({"at": post.now().isoformat(timespec="seconds"), "event": "abandoned",
                                         "message": attempt.message, "seat": name, "result": "abandoned",
                                         "detail": reason}) + "\n")


def reconcile(service: Service) -> set[Path]:
    """Read once before planning; snapshots never authorize settlement.

    Active/unfinished journals are reread before ordinary record-first recovery,
    whose writers revalidate under their locks. Settled terminal journals need
    no action. Publish the pass-local inventory only after successful recovery;
    Service rechecks every attempt/index identity before legacy handling.
    """
    service.loaded_attempts = None
    service._reconcile_attempt_inventory = None
    records = service.attempts.all()  # the original first inventory site
    snapshot = (service.attempts.inventory_snapshot(records)
                if hasattr(service.attempts, 'inventory_snapshot') else None)
    paths = _reconcile_inventory(service, records)
    service._reconcile_attempt_inventory = snapshot
    return paths


def _reconcile_inventory(service, records):
    from .seat import SelectionError

    paths = set()
    attempts = {a.path.name: a for a in records}
    latest = {a.message: a for a in records}
    names = {a.seat for a in attempts.values()}
    names.update(post_dir.directory_names(service.runtime / "post" / "pending"))
    for name in names - service.ship.seats.keys():
        with transient_lock(service.runtime, name) as unowned:
            if unowned:
                abandon(service.runtime, name, "seat no longer declared; submission unconfirmed and never resent",
                        attempts=service.attempts, inventory=records)
        paths.update(a.path for a in attempts.values() if a.seat == name)
    for name in service.ship.seats:
        try:
            seat = service.seat(name)
        except SelectionError as error:
            service.hold_selection(error)
            paths.update(a.path for a in attempts.values() if a.seat == name)
            continue  # neither uncertainty nor receipt evidence is changed under a guessed selection
        from . import lifecycle
        try:
            with lifecycle.claim(service.runtime, name):
                paths.update(_reconcile_claimed(service, seat, attempts, latest=latest))
        except lifecycle.Busy:
            paths.update(a.path for a in attempts.values() if a.seat == name)
    return paths


def _settled_terminal(data, attempts, latest):
    from .receipt_refresh import unfinished
    from .post_retirement import FINISHED
    if data['phase'] not in TERMINAL or unfinished(data):
        return False
    settled = FINISHED | set(post.UNSETTLED)
    for name in data['attempts']:
        attempt = attempts.get(name)
        if (attempt is None or attempt.seat != data['seat'] or attempt.result not in settled
                or latest.get(attempt.message) is None
                or latest[attempt.message].result not in settled):
            return False
        # Preserve the old recovery path for any outcome it would still change.
        if data['phase'] == 'delivered' and attempt.result not in (
                'delivered', 'abandoned', closed_record.PHASE, *post.UNSETTLED):
            return False
        if data['phase'] == closed_record.PHASE and attempt.result not in (
                closed_record.PHASE, 'abandoned', *post.UNSETTLED):
            return False
    return True


def _reconcile_claimed(service: Service, seat: Seat, attempts: dict[str, post.Attempt], *, latest=None) -> set[Path]:
    """Read phase once for settled terminals; reread active data before any recovery action.

    The lifecycle claim excludes retirement writers. The reread handles a journal
    turning terminal during planning; record receipts and attempt write owners
    retain their existing fresh checks, with no read-only scope around actions.
    """
    name, paths = seat.name, set()
    if latest is None:
        latest = {a.message: a for a in sorted(attempts.values(), key=lambda a: (a.at, a.path.name))}
    proofs, errors = journals(seat)
    if errors:
        from .pending_lifecycle import retire_missing_identity
        retired = retire_missing_identity(service.runtime, name, errors, attempts)
        for updated in retired.values():
            attempts.update((a.path.name, a) for a in updated)
        errors = [filename for filename in errors if filename not in retired]
    if errors:
        # Attempts without journals cannot reconstruct this seat's unreadable evidence.
        # Other intact journals (including this seat's) still get their receipts.
        paths.update(a.path for a in attempts.values() if a.seat == name)
        service.log("error", step="reconcile", seat=name, error=inspection(seat, "pending journal unreadable; never resent"), files=errors)
    for proof in proofs:
        if _settled_terminal(proof.data, attempts, latest):
            paths.update(attempts[n].path for n in proof.data['attempts'])
            continue
        try:
            data = json.loads(post_dir.read_text(proof.path))
            validate(data, name)
            proof = Proof(proof.path, data)
        except FileNotFoundError:
            if post_dir.gone(proof.path):
                continue
            paths.update(a.path for a in attempts.values() if a.seat == name)
            service.log('error', step='reconcile', seat=name,
                        error=inspection(seat, 'pending journal unreadable; never resent'),
                        files=[proof.path.name])
            continue
        except (OSError, ValueError, TypeError, KeyError):
            paths.update(a.path for a in attempts.values() if a.seat == name)
            service.log('error', step='reconcile', seat=name,
                        error=inspection(seat, 'pending journal unreadable; never resent'),
                        files=[proof.path.name])
            continue
        if _settled_terminal(proof.data, attempts, latest):
            paths.update(attempts[n].path for n in proof.data['attempts'])
            continue
        if any(n not in attempts for n in proof.data["attempts"]):
            paths.update(a.path for a in attempts.values() if a.seat == name)
            service.log("error", step="reconcile", seat=name, error=inspection(seat, "pending attempt receipt missing; never resent", proof.data["attempts"], message=proof.data.get('message')))
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
        result = "delivered" if phase == "delivered" else None
        if phase not in TERMINAL:
            result = proof.confirmed(seat)
        # A real receipt from the retained old join wins over a later launch's
        # context-only carry marker. Non-delivery evidence keeps the old rule.
        if result != 'delivered' and phase not in TERMINAL and all(any(a.message == old.message and a.seat == old.seat
                and a.result == "launch" and a.launched_at != old.launched_at and a.when >= old.when
                for a in attempts.values()) for old in carried):
            proof.save(phase="carried", resolution="carried by a later explicit launch; old submission unconfirmed")
            phase = "carried"
            result = None
        if phase not in TERMINAL:
            if (not result and not any(a.result in ("delivered", "launch", "watch", *post.UNSETTLED) for a in carried)
                    and closed_record.not_in_closed_record(seat, proof)):
                proof.save(phase=closed_record.PHASE, resolution=closed_record.DETAIL)
                phase = closed_record.PHASE
        if result:
            proof.finish(result)
        from .pending_lifecycle import settle_reported
        if settle_reported(service, seat, proof, attempts):
            paths.update(attempts[n].path for n in proof.data['attempts'])
            continue
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
        return [inspection(seat, "prior launch has an unconfirmed delivery; historical record evidence required", proof.data["attempts"], message=proof.data.get('message'))
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
        path = journal_path(seat.ship.runtime, seat.name, name)
        try:
            data = json.loads(post_dir.read_text(path))
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
    from . import lifecycle

    try:
        with lifecycle.claim(seat.ship.runtime, seat.name):
            return _one_look_claimed(service, seat, attempts)
    except lifecycle.Busy:
        return []


def _one_look_claimed(service: Service, seat: Seat, attempts: Sequence[post.Attempt] | None = None) -> list[str]:
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
            proof_line = service._deliver_claimed(seat, [message], resend=True)
            # The second delivery's journal is named by its new attempt: only this message's journals are listed.
            folder = seat.ship.runtime / "post" / "pending" / seat.name
            prefix = f"{message.id}-"
            for path in post_dir.json_paths(folder):
                if not path.name.startswith(prefix) or path == proof.path:
                    continue
                try:
                    later = Proof(path, json.loads(post_dir.read_text(path)))
                except FileNotFoundError:
                    if not post_dir.gone(path):
                        raise
                    continue
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
        shown = (repr(screen.box) if len(screen.box) <= 80 else
                 f"{screen.box[:77]!r}… (shortened by qd, {len(screen.box)} chars)")
        return None, f"refused: {seat.name}'s box shows {shown}, not exactly a pending message of {seat.name}"
    return matching[0], ""


def submit(service: Service, seat: Seat) -> str:
    from . import lifecycle

    try:
        with lifecycle.claim(seat.ship.runtime, seat.name):
            return _submit_claimed(service, seat)
    except lifecycle.Busy as error:
        return str(error)


def _submit_claimed(service: Service, seat: Seat) -> str:
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
