"""A crew seat whose provider refused its conversation starts fresh, once, without the refused message.

The refusal is read from the seat's own record, in the one native form its adapter classifies as a
refusal (law: record-first); an allowance wall, any other error or an unknown reading is never one.
The refused message is the single ship message the refused turn took. It stays open and unchanged in
the store, its sender is told, and it is never typed into the seat again: not by a tick, not at a
launch and not by a resend. Every other open message is carried by the ordinary fresh start (law:
context-is-cache). What cannot be proved is held where the status line shows it, never guessed.

``runtime/seats/<seat>/refusal.json`` keeps one event per refused turn, written before any notice or
relaunch, so a service that dies mid-way neither repeats a fresh start nor loses the quarantine.
"""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import TYPE_CHECKING

from . import durable, input_receipt, post
from .ship import ShipError
from .turn_failure import REFUSAL, current, describe

if TYPE_CHECKING:
    from .harness.types import TurnFailure
    from .seat import Seat
    from .service import Service

FILE = 'refusal.json'
FORMAT = 1
STAGES = ('claimed', 'launched', 'held')


class JournalError(ValueError):
    pass


def path(runtime: Path, seat: str) -> Path:
    return runtime / 'seats' / seat / FILE


def load(runtime: Path, seat: str) -> list[dict]:
    """The seat's refusal events; an unreadable or unfamiliar journal raises and holds."""
    try:
        text = path(runtime, seat).read_text()
    except FileNotFoundError:
        return []
    except OSError as error:
        raise JournalError(f'refusal journal unreadable: {error}') from None
    try:
        data = json.loads(text)
        if data.get('format') != FORMAT or data.get('seat') != seat or not isinstance(data.get('events'), list):
            raise JournalError('refusal journal of an unknown format')
        for event in data['events']:
            validate(event)
        return data['events']
    except (ValueError, TypeError, AttributeError, KeyError) as error:
        raise JournalError(f'refusal journal unreadable: {error}') from None


def validate(event: dict) -> None:
    if (not isinstance(event, dict) or event.get('stage') not in STAGES
            or not isinstance(event.get('launch'), dict) or not isinstance(event.get('turn'), (str, type(None)))
            or not isinstance(event.get('quarantined'), list) or not isinstance(event.get('notices'), list)
            or not all(isinstance(q, dict) and isinstance(q.get('message'), str) for q in event['quarantined'])
            or not all(isinstance(n, dict) and isinstance(n.get('id'), str) and isinstance(n.get('at'), str)
                       for n in event['notices'])):
        raise JournalError('malformed refusal event')


def save(runtime: Path, seat: str, events: list[dict]) -> None:
    target = path(runtime, seat)
    target.parent.mkdir(parents=True, exist_ok=True)
    # law: context-is-cache -- retain the quarantine/claim so refused text is not typed again.
    durable._replace_law_record(target, json.dumps({'format': FORMAT, 'seat': seat, 'events': events}, indent=2) + '\n')


def excluded(runtime: Path, seat: str) -> frozenset[str]:
    """Message ids never typed into this seat again (law: one-door). Read-only; an unreadable journal raises.

    A claim keeps its quarantine when it is later held: its sender has been told it will not be typed again.
    """
    return frozenset(q['message'] for event in load(runtime, seat) for q in event['quarantined'])


WITHHELD = 'withheld: refused by the provider · sender told'


def stub(message: post.Message) -> str:
    """The refused message as a notice for its addressee: no subject and no body (law: context-is-cache)."""
    return f"qd > {message.kind} from {message.sender} · {message.short} · {WITHHELD}"


def launch_key(seat: Seat) -> dict | None:
    launched = seat.launched()
    return {'pane': launched.pane, 'pid': launched.pid, 'at': launched.at} if launched else None


def _event_for(events: list[dict], launch: dict, failure: TurnFailure) -> dict | None:
    turn = failure.at.isoformat() if failure.at else None
    return next((e for e in events if e['launch'] == launch and e['turn'] == turn), None)


def state_note(seat: Seat) -> str:
    """What the ship does about the current refusal, for the door's reason and the status line."""
    if seat.session.human:
        return 'a human session: the desk starts it fresh'
    try:
        events = load(seat.ship.runtime, seat.name)
    except JournalError as error:
        return f'{error}; no automatic fresh'
    failure, launch = current(seat), launch_key(seat)
    event = _event_for(events, launch, failure) if failure and launch else None
    if event is None:
        return 'automatic fresh pending'
    if event['stage'] == 'held' and not event['quarantined']:
        return (f"held: {event['reason']}; the refused message is not identified and must be kept out by hand "
                f"before any qd fresh {seat.name}")
    if event['stage'] == 'held':
        return f"held: {event['reason']}; the refused message is withheld; inspect, then qd fresh {seat.name}"
    return 'automatic fresh claimed; waiting for the relaunch'


def culprit(seat: Seat, failure: TurnFailure, launch: dict, store: post.Store,
            attempts: post.Attempts, record: Path | None = None) -> tuple[post.Message | None, str | None]:
    """The one ship message the refused turn took, or why it cannot be named without guessing. Each message is read
    in the forms the receipt reads it in, from the seat's record."""
    if not failure.inputs:
        return None, 'the refused turn names no input'
    mine = [a for a in attempts.all() if a.seat == seat.name and a.launched_at == launch['at'] and a.pane is not None]
    named = set()
    for item in failure.inputs:
        found = set()
        for attempt in mine:
            try:
                message = store.get(attempt.message)
            except ShipError:
                continue
            forms, ignoring = input_receipt.read_forms(getattr(seat, 'harness', None), record,
                                                       post.delivered_forms(message), attempt.receipt_since)
            if (item.at is not None and item.at >= attempt.receipt_since
                    and input_receipt.same_input(item.text, forms, ignoring)):
                found.add(message.id)
        if len(found) != 1:
            return None, ('an input of the refused turn is not one message the door typed on this launch'
                          if not found else 'an input of the refused turn matches several messages')
        named |= found
    if len(named) != 1:
        return None, f'the refused turn took {len(named)} messages; the refused one is not guessed'
    return store.get(named.pop()), None


def _after_auto_fresh(events: list[dict], launch: dict, state) -> bool:
    """This launch is the successor of an automatic fresh and has not yet completed a turn normally."""
    return (any(e['stage'] == 'launched' and e.get('successor') == launch['at'] for e in events)
            and not any(t.failure is None for t in state.completed_turns))


def notice(event: dict, entry: dict, seat: str) -> post.Message:
    body = (f"{seat}'s provider refused its conversation at your {entry['kind']} {post.short_of(entry['message'])} "
            f"({event['code'] or 'refusal'}, {event['turn'] or 'time unknown'}). The ship will not type it to {seat} "
            f"again: a fresh start of {seat} carries only a notice of it. It stays open: send a reworded message, "
            f"or close it.")  # the same words at every stage, so a republication matches the published notice
    record = next(n for n in event['notices'] if n['message'] == entry['message'])
    return post.Message(record['id'], 'report', post.SHIP, entry['sender'], datetime.fromisoformat(record['at']),
                        body, re=entry['message'])


def _publish(store: post.Store, message: post.Message) -> None:
    """Publish the fixed intent once; an existing identical file is the earlier publication."""
    target = store.dir / f'{message.id}.json'
    if target.exists():
        if post.Message.from_json(target.read_text()) != message:
            raise ShipError(f'{message.id}: refusal notice conflicts with an immutable post')
        return
    store.write(message)


def run(service: Service) -> None:
    """Each seat's step on its own: a seat's refusal failure (its journal, a notice, its files) is logged once
    while it stands, and never stops the next seat."""
    logged = service.__dict__.setdefault('refusal_errors', {})
    for seat in service.resolved_seats():
        if seat.session.human:
            continue  # a human's own seats stay with the desk
        try:
            step(service, seat)
        except (JournalError, ShipError, OSError) as error:  # anything else is a fault for the tick, as before
            text = str(error)
            if logged.get(seat.name) != text:
                logged[seat.name] = text
                service.log('error', step='refusals', seat=seat.name, error=text)
            continue
        logged.pop(seat.name, None)


def step(service: Service, seat: Seat) -> None:
    runtime = service.runtime
    events = load(runtime, seat.name)
    launch = launch_key(seat)
    for event in events:
        if event['stage'] == 'held':
            _tell(service, seat, event)  # completes a notice a crash left between the durable hold and its publication
    for event in events:
        if event['stage'] != 'claimed':
            continue
        if launch is not None and launch != event['launch']:
            event['stage'], event['successor'] = 'launched', launch['at']  # a later launch is adopted, never repeated
            save(runtime, seat.name, events)
            service.log('refusal', seat=seat.name, stage='launched', successor=launch['at'])
        elif launch is None:
            _tell(service, seat, event)
            event['stage'], event['reason'] = 'held', 'the automatic fresh start was interrupted; qd up'
            save(runtime, seat.name, events)
            from . import relaunch_intent  # the hold keeps the seat down until a person acts
            relaunch_intent.clear(runtime, seat.name, 'its refusal hold')
        else:
            relaunch(service, seat, events, event)
        return
    if launch is None or not seat.is_up:
        return
    failure = current(seat)
    if failure is None or failure.kind != REFUSAL or _event_for(events, launch, failure) is not None:
        return
    record = seat.record()
    try:
        state = seat.harness.state(record)
    except OSError:
        return
    if state.turn_open is not False or state.queued or state.pending_tools:
        return  # still ending: an open turn, queued input or a running tool; the door holds meanwhile
    event = {'launch': launch, 'record': str(record), 'turn': failure.at.isoformat() if failure.at else None,
             'code': failure.code, 'words': describe(failure), 'quarantined': [], 'notices': [],
             'stage': 'held', 'reason': None, 'successor': None}
    # A named refused message is quarantined and its sender told whether or not the fresh start may proceed;
    # what cannot be named is never quarantined on a guess (law: context-is-cache).
    message, why = culprit(seat, failure, launch, service.store, service.attempts, record)
    if message is not None:
        event['quarantined'] = [{'message': message.id, 'sender': message.sender, 'kind': message.kind}]
        if message.sender in service.ship.seats:
            intent = service.store.prepare('report', post.SHIP, message.sender, '', re=message.id)
            event['notices'] = [{'message': message.id, 'id': intent.id, 'at': intent.at.isoformat()}]
    if message is None:
        event['reason'] = why
    elif state.queue_error or state.pending_tools is None:
        event['reason'] = 'its queue or tool set is unknown in the record; an empty one is not proved'
    elif _after_auto_fresh(events, launch, state):
        event['reason'] = 'refused again before the fresh conversation completed a turn; not started fresh twice'
    else:
        event['stage'] = 'claimed'
    events.append(event)
    save(runtime, seat.name, events)  # the quarantine and the claim are durable before any notice or relaunch
    service.log('refusal', seat=seat.name, stage=event['stage'], reason=event['reason'],
                quarantined=[q['message'] for q in event['quarantined']])
    if event['stage'] == 'claimed':
        relaunch(service, seat, events, event)
    else:
        _tell(service, seat, event)


def _tell(service: Service, seat: Seat, event: dict) -> None:
    """Publish each notice not yet published. One already published is recognized by its id and never rebuilt,
    so a later change of the notice's words never conflicts with a notice an earlier version wrote."""
    for entry in event['quarantined']:
        record = next((n for n in event['notices'] if n['message'] == entry['message']), None)
        if record is not None and not (service.store.dir / f"{record['id']}.json").exists():
            _publish(service.store, notice(event, entry, seat.name))


def relaunch(service: Service, seat: Seat, events: list[dict], event: dict) -> None:
    _tell(service, seat, event)
    try:
        from . import relaunch_intent
        with relaunch_intent.section(service, seat, 'refusal', resume=False):
            proof = seat.fresh()  # law: context-is-cache -- every open message but the refused one, in full
    except ShipError as error:
        # Held, never retried: the quarantine and the notice stand, and a person or the systems seat starts it fresh.
        event['stage'], event['reason'] = 'held', f'the automatic fresh start failed: {error}'
        save(service.runtime, seat.name, events)
        relaunch_intent.clear(service.runtime, seat.name, 'its refusal hold')
        service.log('error', step='refusals', seat=seat.name, error=str(error), stage='held')
        return
    event['stage'], event['successor'] = 'launched', (launch_key(seat) or {}).get('at')
    save(service.runtime, seat.name, events)
    service.log('fresh', seat=seat.name, proof=proof, reason='provider refusal')
