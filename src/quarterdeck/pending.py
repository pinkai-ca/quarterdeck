"""Durable evidence for a single paste/submit, never a license to resend it.

Only the service writes these journals. Door/watch read them without advancing
clocks or marking delivery. The original attempt time survives every restart.
"""

from __future__ import annotations

from datetime import datetime
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path

from . import post
from . import input_receipt
from .harness.json_record import RecordError

WAIT_SECONDS = 600  # intervention policy, not a claim about normal program latency
TERMINAL = {'delivered', 'cancelled', 'carried', 'abandoned'}


def identity(seat) -> dict:
    launch, pane = seat.launched(), seat.pane()
    return {'pane': pane.id if pane else None, 'pid': pane.pid if pane else None,
            'launch': launch.at if launch else None, 'harness': seat.harness.name}


class Proof:
    def __init__(self, path: Path, data: dict):
        self.path, self.data = path, data

    def save(self, **fields) -> None:
        self.data.update(fields)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.data, indent=2) + '\n')
        os.replace(tmp, self.path)

    @classmethod
    def begin(cls, seat, attempts, text):
        first = attempts[0]
        path = seat.ship.runtime / 'post' / 'pending' / seat.name / first.path.name
        record = seat.record()
        proof = cls(path, {})
        original = min(a.at for attempt in attempts for a in post.Attempts(seat.ship.runtime).for_message(attempt.message))
        proof.save(seat=seat.name, identity=identity(seat), at=original,
                   since=post.now().isoformat(), payload=text, phase='prepared',
                   record=str(record) if record else None, shown=None,
                   attempts=[a.path.name for a in attempts])
        return proof

    def same_launch(self, seat) -> bool:
        return self.data['identity'] == identity(seat)

    def finish(self, result: str) -> None:
        if result == 'withheld' and self.data['phase'] == 'prepared':
            self.save(phase='cancelled')
        elif result in ('delivered', 'queued'):
            self.save(phase=result)

    def confirmed(self, seat) -> str | None:
        """Only an exact owned-record input after this paste proves delivery."""
        name = self.data['identity']['harness']
        record = self.data['record']
        if not record and self.same_launch(seat):
            joined = seat.record()
            if joined:
                self.save(record=str(joined))
                record = str(joined)
        if not record:
            return None
        try:
            return input_receipt.confirmed(name, Path(record), self.data['payload'],
                                           datetime.fromisoformat(self.data['since']))
        except OSError:
            return None  # unreadable evidence never becomes success

    def reading(self, seat, screen, now=None):
        """Return (reason, remedy, quiet) or None for a proven accepted queue."""
        phase = self.data['phase']
        if phase == 'queued' and (not self.same_launch(seat) or (screen and screen.box == '')):
            return None
        if not self.same_launch(seat):
            return ('pending delivery belongs to a different pane/process/launch', 'needs a human', False)
        if phase not in ('submitted', 'queued'):
            return ('paste/submit outcome uncertain; never resent', 'needs a human', False)
        if screen is None or screen.box is None or screen.interruption or screen.unknown:
            return ('pending delivery; composer cannot be verified', 'needs a human', False)
        if screen.box and screen.box != self.data['shown']:
            return ('pending composer changed or is foreign; untouched', 'needs a human', False)
        if screen.box and screen.box.startswith('[Pasted'):
            return ('pending folded composer is ambiguous; untouched', 'needs a human', False)
        age = ((now or post.now()) - datetime.fromisoformat(self.data['at'])).total_seconds()
        if age >= WAIT_SECONDS:
            return ('pending delivery exceeded 600s; inspect composer and record before explicit resend', 'needs a human', False)
        reason = 'waiting to be taken' if screen.box else 'waiting for record confirmation'
        return (f'{reason} ({int(max(0, age))}s of 600s)', 'record confirmation', True)


def journals(seat):
    runtime = getattr(getattr(seat, 'ship', None), 'runtime', None)
    return _journals(runtime, seat.name) if runtime is not None else ([], [])


def _journals(runtime, name):
    found, errors = [], []
    if runtime is None:
        return found, errors
    for path in sorted((runtime / 'post' / 'pending' / name).glob('*.json')):
        try:
            data = json.loads(path.read_text())
            validate(data, name)
            if any(not (runtime / 'post' / 'attempts' / n).is_file() for n in data['attempts']):
                raise ValueError('pending attempt receipt missing')
            found.append(Proof(path, data))
        except (OSError, ValueError, TypeError, KeyError):
            errors.append(path.name)
    return found, errors


def validate(data, seat):
    from .harness import NAMES

    identity = data['identity']
    if (data['seat'] != seat or data['phase'] not in TERMINAL | {'prepared', 'pasting', 'submitting', 'submitted', 'queued'}
            or not isinstance(data['payload'], str) or not isinstance(identity['pane'], str)
            or not isinstance(identity['pid'], int) or identity['harness'] not in NAMES
            or not isinstance(data['attempts'], list) or not data['attempts']
            or any(not isinstance(n, str) or Path(n).name != n for n in data['attempts'])
            or (data['record'] is not None and not isinstance(data['record'], str))
            or (data['shown'] is not None and not isinstance(data['shown'], str))):
        raise ValueError('invalid pending journal')
    for stamp in (data['at'], data['since'], identity['launch']):
        if datetime.fromisoformat(stamp).tzinfo is None:
            raise ValueError('pending journal timestamp lacks timezone')


def active(seat, include_terminal=False) -> list[Proof]:
    found, errors = journals(seat)
    if errors:
        raise RecordError('pending delivery journal unreadable; never resent')
    return [p for p in found if include_terminal or p.data['phase'] not in TERMINAL]


def held(seat, screen, ignored=None):
    try:
        proofs = active(seat)
    except (OSError, ValueError, KeyError):
        return ('pending delivery journal unreadable; never resent', 'needs a human', False)
    for proof in proofs:
        if ignored is not None and proof.path == ignored.path and proof.data['phase'] == 'prepared':
            continue  # only this delivery's pre-paste check may ignore its own reservation
        if not proof.same_launch(seat) and screen and screen.box == '' and not screen.unknown and not screen.interruption:
            continue  # another empty launch is usable; the old uncertainty remains a global notice
        if reading := proof.reading(seat, screen):
            return reading
    return None


@contextmanager
def transient_lock(runtime: Path, name: str):
    """Hold a temporary seat's explicit lifetime, or guard its orphan cleanup.

    A check owns this lock from before its first attempt through teardown. The
    non-inheritable descriptor releases on owner exit, including a killed check.
    Keep the empty file: unlinking could let two processes lock different inodes.
    Names alone (including check-*) and stale unlocked files establish no owner.
    """
    path = runtime / 'post' / 'transient' / f'{name}.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
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
        if proof.data['phase'] not in TERMINAL:
            proof.save(phase='abandoned', resolution=reason)
    changed = post.Attempts(runtime).abandon(name, reason)
    if changed:
        path = runtime / 'log' / 'service.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a') as stream:
            for attempt in changed:
                stream.write(json.dumps({'at': post.now().isoformat(timespec='seconds'), 'event': 'abandoned',
                                         'message': attempt.message, 'seat': name, 'result': 'abandoned',
                                         'detail': reason}) + '\n')


def reconcile(service) -> set[Path]:
    paths = set()
    attempts = {a.path.name: a for a in service.attempts.all()}
    names = {a.seat for a in attempts.values()}
    names.update(p.name for p in (service.runtime / 'post' / 'pending').glob('*') if p.is_dir())
    for name in names - service.ship.seats.keys():
        with transient_lock(service.runtime, name) as unowned:
            if unowned:
                abandon(service.runtime, name, 'seat no longer declared; submission unconfirmed and never resent')
        paths.update(a.path for a in attempts.values() if a.seat == name)
    for name in service.ship.seats:
        seat = service.seat(name)
        proofs, errors = journals(seat)
        if errors:
            # Do not fall through to weaker legacy reconstruction for this seat.
            # Other intact journals (including this seat's) still get their receipts.
            paths.update(a.path for a in attempts.values() if a.seat == name)
            service.log('error', step='reconcile', seat=name, error='pending journal unreadable; never resent', files=errors)
        for proof in proofs:
            if any(n not in attempts for n in proof.data['attempts']):
                paths.update(a.path for a in attempts.values() if a.seat == name)
                service.log('error', step='reconcile', seat=name, error='pending attempt receipt missing; never resent')
                continue
            phase = proof.data['phase']
            carried = [attempts[n] for n in proof.data['attempts']]
            if phase not in TERMINAL and all(any(a.message == old.message and a.seat == old.seat
                    and a.result == 'launch' and a.launched_at != old.launched_at and a.when >= old.when
                    for a in attempts.values()) for old in carried):
                proof.save(phase='carried', resolution='carried by a later explicit launch; old submission unconfirmed')
                phase = 'carried'
            result = 'delivered' if phase == 'delivered' else None
            if phase not in TERMINAL:
                result = proof.confirmed(seat)
            if result:
                proof.finish(result)
            for filename in proof.data['attempts']:
                attempt = attempts[filename]
                paths.add(attempt.path)
                if result and attempt.result != result:
                    service.attempts.close(attempt, result, 'exact payload found in owned record after paste')
                elif attempt.result is None:
                    recovered = {'submitted': 'pending', 'queued': 'queued', 'cancelled': 'withheld', 'abandoned': 'abandoned'}.get(proof.data['phase'], 'blind')
                    service.attempts.close(attempt, recovered, 'recovered durable paste/submit phase; never resent')
    return paths


def explicit_resend(service, seat, messages) -> None:
    """An explicit request can retire uncertainty only after the composer is empty.

    It authorizes another submission, not clearing any draft or calling the old
    one delivered. Normal ticks never call this function.
    """
    from .door import Door

    screen = Door(seat).screen()
    if seat.session.human or screen is None or screen.box != '' or screen.unknown or screen.interruption:
        return
    requested = {m.id for m in messages}
    attempts = {a.path.name: a for a in service.attempts.all()}
    for proof in active(seat):
        carried = [attempts[name] for name in proof.data['attempts']]
        if {a.message for a in carried} <= requested:
            proof.save(phase='cancelled', resolution='explicit resend requested with empty composer')
            for attempt in carried:
                service.attempts.close(attempt, 'blind', 'explicit resend superseded this unconfirmed submission')


def detached(seat) -> list[str]:
    """Old unresolved submissions remain visible without owning a different door."""
    try:
        return ['prior launch has an unconfirmed delivery; inspect its record' for p in active(seat)
                if not p.same_launch(seat) and p.data['phase'] != 'queued']
    except (OSError, ValueError, KeyError):
        return ['pending delivery journal unreadable; never resent']
