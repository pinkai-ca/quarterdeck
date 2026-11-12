"""A turn that ended in a recorded error: shown, alarmed and its input left unsettled.

The record is read first (law: record-first). A seat in a human's session is read from a small
operational file the service writes from its own reading of the record, so an automatic status line
never opens a human's conversation. Nothing here relaunches a seat or types anything; the provider
refusal's fresh start is ``refusal.py``'s.
"""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import stat
from typing import TYPE_CHECKING

from . import durable, input_receipt, pending, post
from .harness.base import record_fingerprint
from .harness.failures import words
from .harness.types import TurnFailure

if TYPE_CHECKING:
    from .seat import Seat
    from .service import Service

FILE = 'turn-failure.json'
MAX_BYTES = 8192
KINDS = ('provider refusal', 'provider error', 'program error')
REFUSAL = 'provider refusal'


def metadata(path: Path) -> str:
    """Read only a bounded regular operational file, with a stable opened identity."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_BYTES:
            raise ValueError('unproved operational metadata')
        raw = bytearray()
        while len(raw) <= MAX_BYTES:
            part = os.read(fd, min(4096, MAX_BYTES + 1 - len(raw)))
            if not part:
                break
            raw.extend(part)
        after = os.fstat(fd)

        def stamp(s):
            return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_nlink
        if len(raw) > MAX_BYTES or len(raw) != before.st_size or stamp(before) != stamp(after):
            raise ValueError('changing operational metadata')
        return raw.decode('utf-8')
    finally:
        os.close(fd)


def identity(seat: Seat) -> dict | None:
    launched = seat.launched()
    return ({'pane': launched.pane, 'pid': launched.pid, 'at': launched.at, 'harness': seat.harness.name}
            if launched else None)


def publish(seat: Seat, record: Path, state, turns, stamp=None) -> None:
    """Service only: the error's kind, code, first words and time, never inputs, replies or subjects."""
    stamp = stamp if stamp is not None else record_fingerprint(record)
    if record_fingerprint(record) != stamp:
        return  # never certify old turn data against a newer source
    failure = getattr(state, 'failure', None)
    data = {'format': 1, 'identity': identity(seat), 'record': str(record), 'fingerprint': stamp, 'failure': None}
    if failure:
        data['failure'] = {'message': words(failure.message), 'kind': failure.kind, 'code': failure.code,
                           'model_output': failure.model_output,
                           'at': failure.at.isoformat() if failure.at else None}
    path = seat.folder / FILE
    text = json.dumps(data, sort_keys=True) + '\n'
    try:
        previous = metadata(path)
    except (OSError, ValueError):
        previous = None
    if previous != text:
        durable.replace_file(path, text)


def cached(seat: Seat) -> TurnFailure | None:
    """A human seat's latest failure from owned metadata and the record's stat, never its bytes."""
    try:
        data = json.loads(metadata(seat.folder / FILE))
        if data.get('format') != 1 or data.get('identity') != identity(seat):
            return None
        if metadata(seat.folder / 'record').strip() != data['record']:
            return None
        failure = data['failure']
        if not failure:
            return None
        code = failure.get('code')
        if (not isinstance(failure, dict) or failure.get('kind') not in KINDS
                or not isinstance(failure.get('message'), str) or len(failure['message']) > 160
                or code is not None and not (isinstance(code, str) and code.isidentifier() and len(code) <= 64)
                or failure.get('model_output') is not None and type(failure['model_output']) is not bool):
            return None
        if data['fingerprint'] != json.loads(json.dumps(record_fingerprint(Path(data['record'])))):
            return TurnFailure('record changed since; current outcome unknown', None, kind=failure['kind'], code=code)
        at = datetime.fromisoformat(failure['at']) if failure.get('at') else None
        if at is not None and at.tzinfo is None:
            return None
        return TurnFailure(words(failure['message']), at, model_output=failure['model_output'],
                           kind=failure['kind'], code=code)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None  # missing or unproved metadata establishes no current failure


def current(seat: Seat) -> TurnFailure | None:
    """The latest ended turn's failure in the seat's own record; a later turn clears it."""
    if seat.session.human:
        return cached(seat)
    try:
        record = seat.record()
        return getattr(seat.harness.state(record), 'failure', None) if record is not None else None  # law: record-first
    except OSError:
        return None  # the unreadable record is reported by the ordinary reading


def describe(failure: TurnFailure) -> str:
    return f"{failure.kind}{f' ({failure.code})' if failure.code else ''}: {words(failure.message)}"


def obstruction(seat: Seat, store: post.Store | None = None) -> str | None:
    """Why the door holds after a recorded error, or None.

    A refused conversation holds the door whatever is open, since the next message would be typed into
    the conversation its provider refused; any other error holds it only while an order or question is
    open to the seat, so its alarm goes out (law: nothing-waits-silently).
    """
    failure = current(seat)
    if failure is None:
        return None
    if failure.kind == REFUSAL:
        from . import refusal
        return f"{describe(failure)}; {refusal.state_note(seat)}"
    store = store or post.Store(seat.ship.runtime)
    if not any(m.kind in ('order', 'question') for m in store.open_for(seat.name)):
        return None
    return describe(failure)


def unsettled(failure: TurnFailure) -> str | None:
    """unacted where the record proves no model output; uncertain where it cannot tell; else None."""
    return 'unacted' if failure.model_output is False else 'uncertain' if failure.model_output is None else None


def matching(seat: Seat, failure: TurnFailure, store: post.Store, attempts: post.Attempts) -> list[post.Attempt]:
    """This seat's latest attempts whose exact typed message is an input of the failed turn."""
    found = []
    for attempt in attempts.all():
        if attempt.seat != seat.name or attempt.result not in ('delivered', 'queued'):
            continue
        try:
            text = post.typed([store.get(attempt.message)])
        except (OSError, KeyError, ValueError):
            continue
        if any(item.at is not None and item.at >= attempt.receipt_since and input_receipt.same_text(item.text, text)
               for item in failure.inputs):
            found.append(attempt)
    latest = attempts.latest()
    return [a for a in found if latest.get(a.message) == a.result
            and a == max((b for b in found if b.message == a.message), key=lambda b: b.when)]


def settle(service: Service) -> None:
    """A message typed into a turn that ended in an error is not settled as received (law: one-door).

    Proved no model output makes it unacted: no tick types it again and the next start carries it.
    Unknown output makes it uncertain: it waits for a hand. Proved output keeps its receipt. The
    native acceptance, the record and the Ledger's turn are left as they are.
    """
    for seat in service.resolved_seats():
        if seat.session.human:
            continue  # its messages reach it by its watch; the ship never typed them
        failure = current(seat)
        result = unsettled(failure) if failure else None
        if result is None:
            continue
        try:
            proofs, _ = pending.journals(seat)
        except (OSError, ValueError):
            proofs = []
        for attempt in matching(seat, failure, service.store, service.attempts):
            for proof in proofs:
                if attempt.path.name in proof.data['attempts'] and proof.data['phase'] not in pending.UNSETTLED:
                    proof.finish(result)
            service.attempts.close(attempt, result, f"{pending.UNSETTLED[result]} ({describe(failure)})")
            service.log('unsettled', seat=seat.name, message=attempt.message, result=result, kind=failure.kind)
