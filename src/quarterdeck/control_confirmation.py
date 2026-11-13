"""A consumed control's continuation, stored by the existing request owner.

This module owns no queue, files or transport. The caller persists each returned state before acting and
never interprets an existing reservation as permission to type the command again.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
import hashlib
import json

from .harness.types import ControlAnswer
from .ship import ShipError


CONFIRM_SECONDS = 30
PHASES = frozenset({'reserved', 'submitted', 'answering', 'answered'})


def input_scope(seat, record) -> dict | None:
    """Complete input evidence as identities/digest only; never persist a native input body."""
    if record is None:
        return None
    try:
        before = record.stat()
        inputs = seat.harness.inputs(record)
        diagnostics = inputs.diagnostics()
        if type(diagnostics.get('total')) is not int or diagnostics['total'] != 0:
            return None
        rows = [(item.text, item.kind, item.at.isoformat() if item.at else None,
                 item.consumed_at.isoformat() if item.consumed_at else None) for item in inputs]
        after = record.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            return None
        digest = hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()
        return dict(record=str(record), device=after.st_dev, inode=after.st_ino,
                    input_count=len(rows), inputs_digest=digest)
    except (OSError, ValueError, TypeError, AttributeError):
        return None


@dataclass(frozen=True)
class Option:
    text: str
    value: str
    keys: tuple[str, ...]


def value_choice(control: str, value: str, options: tuple[Option, ...], identity) -> ControlAnswer | None:
    """Choose one adapter-proved option by its value/text, with no cursor input or guessed gesture."""
    wanted = identity(value)
    if wanted is None:
        return None
    matches = [option for option in options if option.text and option.keys and identity(option.value) == wanted]
    if len(matches) != 1:
        return None
    option = matches[0]
    return ControlAnswer(control, value, option.text, option.keys)


@dataclass(frozen=True)
class Continuation:
    control: str
    value: str
    origin: dict
    started: str
    deadline: str
    phase: str = 'reserved'
    asked: bool = False
    answered: bool = False

    def metadata(self) -> dict:
        return dict(version=1, **asdict(self))

    @classmethod
    def begin(cls, control: str, value: str, origin: dict, now: datetime) -> Continuation:
        if now.tzinfo is None:
            raise ShipError('control confirmation clock unknown; command not typed')
        return cls(control, value, dict(origin), now.isoformat(),
                   (now + timedelta(seconds=CONFIRM_SECONDS)).isoformat())

    @classmethod
    def from_metadata(cls, value: dict) -> Continuation:
        try:
            if type(value['version']) is not int or value['version'] != 1:
                raise ValueError('version')
            data = {key: value[key] for key in cls.__dataclass_fields__}
            if (not isinstance(data['control'], str) or not data['control']
                    or not isinstance(data['value'], str) or not data['value']
                    or not isinstance(data['origin'], dict) or data['phase'] not in PHASES
                    or type(data['asked']) is not bool or type(data['answered']) is not bool):
                raise ValueError('shape')
            if (data['phase'], data['asked'], data['answered']) not in {
                    ('reserved', False, False), ('submitted', False, False),
                    ('answering', True, False), ('answered', True, True)}:
                raise ValueError('phase evidence')
            started, deadline = datetime.fromisoformat(data['started']), datetime.fromisoformat(data['deadline'])
            if (started.tzinfo is None or deadline.tzinfo is None
                    or deadline - started != timedelta(seconds=CONFIRM_SECONDS)):
                raise ValueError('deadline')
            return cls(**data)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ShipError('control confirmation state unknown; retained without retyping') from None


@dataclass(frozen=True)
class Decision:
    state: Continuation
    action: str
    reason: str


def record_confirmation(harness, control, value, state, *, version=None) -> bool | None:
    """Model completion is record-only and uses adapter identity, never generic fuzzy aliases."""
    reported = getattr(state, control.name, None) if state is not None else None
    if reported is None:
        return None
    if control.name == 'model':
        wanted = harness.model_identity(value, version=version)
        actual = harness.model_identity(reported, version=version)
        return None if wanted is None or actual is None else wanted == actual
    return reported == value


def advance(state: Continuation, now: datetime, *, confirmed: bool | None,
            answer: ControlAnswer | None = None) -> Decision:
    """Choose the next observation/answer; an existing state can NEVER request command transport."""
    started, deadline = datetime.fromisoformat(state.started), datetime.fromisoformat(state.deadline)
    if now.tzinfo is None or now < started or now >= deadline:
        return Decision(state, 'unconfirmed', 'confirmation deadline expired or clock unknown; not retyped')
    if confirmed is True:
        return Decision(state, 'confirmed', 'record confirmed the requested value')
    if (answer is not None and answer.control == state.control and answer.value == state.value
            and answer.keys and state.phase == 'submitted'):
        return Decision(replace(state, phase='answering', asked=True), 'answer',
                        'program asked; the owned request may answer its value-matched option')
    return Decision(state, 'wait', 'waiting for record confirmation; command is not retyped')


def submitted(state: Continuation) -> Continuation:
    """Called only after this request's command transport returned a positively typed result."""
    return replace(state, phase='submitted')


def answered(state: Continuation, *, typed: bool) -> Continuation:
    """Only an observed typed result proves the ship answered; no ambiguous gesture is replayed."""
    return replace(state, phase='answered' if typed else 'answering', answered=typed)


def proof(state: Continuation) -> str:
    if state.asked and state.answered:
        return 'program asked; ship answered yes; record confirmed'
    if state.asked:
        return 'program asked; ship answer unconfirmed; record confirmed'
    return 'record confirmed'


def observe_control(seat, control, state: Continuation, now: datetime, *, persist, verify) -> Decision:
    """Advance a consumed request through its existing owner's persistence/identity callbacks.

    There is deliberately no command-delivery operation here. A resumed reservation can only observe
    the record or expire; only a positively submitted control can own its subsequent question.
    """
    from .door import Door

    if not verify():
        return Decision(state, 'unconfirmed', 'control source changed; command is not retyped')
    record = seat.record()
    try:
        reported = seat.harness.state(record) if record is not None else None
    except (OSError, ValueError, TypeError):
        reported = None
    launched = seat.launched()
    version = launched.version if launched else None
    confirmed = record_confirmation(seat.harness, control, state.value, reported, version=version)
    door = Door(seat)
    screen = door.screen() if confirmed is not True and state.phase == 'submitted' else None
    answer = (seat.harness.control_answer(control, state.value, screen, version=version)
              if screen is not None else None)
    decision = advance(state, now, confirmed=confirmed, answer=answer)
    if decision.action == 'answer':
        persist(decision.state)  # same request metadata; reserve the single gesture before any key
        delivery = door.answer_control(control, state.value, answer, verify)
        updated = answered(decision.state, typed=delivery.result == 'typed')
        persist(updated)
        return Decision(updated, 'wait', f'{delivery}; waiting for record confirmation')
    if (decision.action == 'wait' and screen is not None
            and getattr(screen.interruption, 'control', None) == control.name and answer is None):
        return Decision(state, 'wait', 'program asked; option/value or selection gesture unknown; no answer sent')
    return decision
