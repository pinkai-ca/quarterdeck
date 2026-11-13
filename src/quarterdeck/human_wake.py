"""One fixed human-seat wake through Door, with durable once-per-watch evidence.

The waiting post is never transported or receipted here. Only the fixed wake has
an input receipt; the ordinary inbox watch still delivers the actual messages.
"""

from __future__ import annotations

import json
from datetime import datetime

from . import durable, input_receipt, pending, post, recovery
from .harness.base import record_fingerprint
from .turn_failure import metadata
from .ship import ShipError

LINE = ('qd > ship: messages are waiting for you. Run `qd inbox --wait` now and follow what it prints; '
        'if your program can keep a command running in the background, keep it running there.')
WAITING = 'human wake confirmed; waiting for watch registration or ended turn'
PHASES = {'prepared', 'pasting', 'submitting', 'submitted', 'queued', 'delivered', 'cancelled'}


def load(seat) -> pending.Proof | None:
    path = seat.folder / 'human-wake.json'
    try:
        data = json.loads(metadata(path))
    except FileNotFoundError:
        return None
    identity = data['identity']
    if (type(data.get('format')) is not int or data['format'] != 1 or data['seat'] != seat.name or data['payload'] != LINE
            or data['phase'] not in PHASES or data['attempts'] != []
            or type(data.get('ended', False)) is not bool
            or not isinstance(identity['pane'], str) or type(identity['pid']) is not int
            or identity['pid'] <= 0 or not isinstance(identity['harness'], str)
            or not isinstance(data['generation'], (str, type(None)))
            or data['generation'] is not None and (len(data['generation']) != 32
                or any(c not in '0123456789abcdef' for c in data['generation']))
            or not isinstance(data['record'], (str, type(None)))
            or not isinstance(data['shown'], (str, type(None)))):
        raise ValueError('invalid human wake reservation')
    for stamp in (data['at'], data['since'], identity['launch']):
        if datetime.fromisoformat(stamp).tzinfo is None:
            raise ValueError('untimed human wake reservation')
    return pending.Proof(path, data)


def episode(proof, watch) -> str | None:
    """Read-only episode state; only a receipt plus a later arm releases a wake."""
    if proof is None or proof.data['phase'] == 'cancelled':
        return None
    if proof.data['phase'] != 'delivered':
        return 'human wake receipt uncertain; never resent; inspect human-wake.json'
    if watch.generation == proof.data['generation']:
        if watch.armed:
            return 'human wake confirmed but watch rearm generation unproved; person must intervene'
        return ('human wake confirmed but watch not armed; person must intervene'
                if proof.data.get('ended') else WAITING)
    return None


def record_stamp(seat) -> dict:
    """Operational pointer and file metadata only, never conversation bytes."""
    try:
        name = metadata(seat.folder / 'record').strip()
    except FileNotFoundError:
        return {'record': None, 'fingerprint': None}
    if not name:
        raise ValueError('empty record pointer')
    from pathlib import Path
    return {'record': name, 'fingerprint': json.loads(json.dumps(record_fingerprint(Path(name))))}


def status(seat) -> str | None:
    watch = post.watch_reading(seat.ship.runtime, seat.name)
    if watch.armed is None:
        return watch.reason
    try:
        proof = load(seat)
        if reason := episode(proof, watch):
            return reason
        waiting = post.Attempts(seat.ship.runtime).undelivered(post.Store(seat.ship.runtime), seat.name)
        if watch.armed or not waiting:
            return None
        try:
            data = json.loads(metadata(seat.folder / 'human-wake-status.json'))
        except FileNotFoundError:
            return 'human wake awaiting service observation'
        if (data['format'] != 1 or not isinstance(data['reason'], (str, type(None)))
                or data['reason'] is not None and len(data['reason']) > 1024):
            raise ValueError('invalid wake observation')
        if data['identity'] != pending.identity(seat) or data['source'] != record_stamp(seat):
            return 'human wake source changed; awaiting service observation'
        return data['reason']
    except (OSError, ValueError, KeyError, TypeError):
        return 'human wake evidence unreadable; no waking input'


def observe(seat, reason, identity, source) -> None:
    data = dict(format=1, identity=identity, source=source, reason=reason)
    path = seat.folder / 'human-wake-status.json'
    text = json.dumps(data, sort_keys=True) + '\n'
    try:
        previous = metadata(path)
    except FileNotFoundError:
        previous = None
    if text != previous:
        path.parent.mkdir(parents=True, exist_ok=True)
        durable.replace_file(path, text)


def prior_receipt(seat, record, since) -> str | None:
    """An old identical input must not qualify as this fixed line's fresh receipt."""
    if record is None:
        return None
    try:
        stamp = record_fingerprint(record)
        if seat.record() != record:
            return 'human wake prior receipt ownership unknown; no waking input'
        inputs = seat.harness.inputs(record)
        diagnostics = getattr(inputs, 'diagnostics', None)
        if diagnostics is not None and diagnostics()['total']:
            return 'human wake prior receipt parse incomplete; no waking input'
        result = input_receipt.confirmed(seat.harness, record, LINE, since)
        if record_fingerprint(record) != stamp or seat.record() != record:
            return 'human wake prior receipt source changed; no waking input'
        if result is not None:
            return 'human wake receipt-time boundary ambiguous; no waking input'
    except (OSError, ValueError, KeyError, TypeError):
        return 'human wake prior receipt unknown; no waking input'
    return None


def eligible(seat, screen, proof=None) -> str | None:
    """Positive empty screen, authoritative record vetoes, and no hidden defaults."""
    watch = post.watch_reading(seat.ship.runtime, seat.name)
    if watch.armed is not False:
        return watch.reason or 'inbox watch is armed; its watch delivers'
    if proof is not None and watch.generation != proof.data['generation']:
        return 'inbox watch rearmed during wake; no new input authorized'
    if not post.Attempts(seat.ship.runtime).undelivered(post.Store(seat.ship.runtime), seat.name):
        return 'no waiting message; no waking input'
    if recovery.held(seat):
        return 'human wake recovery held; person must intervene'
    if pending.detached(seat):
        return 'prior launch has an unconfirmed delivery; human wake held'
    launch, pane = seat.launched(), seat.pane()
    if launch is None or pane is None or pane.dead or pane.in_mode:
        return 'human wake launch/pane identity unavailable or pane in copy mode'
    joined = seat.join()
    if joined.record is None:
        if joined.reason != 'no record yet':
            return 'human wake owned record unknown; no waking input'
    else:
        try:
            stamp = record_fingerprint(joined.record)
            state = seat.harness.state(joined.record)
            if record_fingerprint(joined.record) != stamp:
                return 'human wake record changed during reading; no waking input'
        except (OSError, ValueError, TypeError):
            return 'human wake record unreadable'
        if state.turn_open is True or state.queued or state.pending_tools:
            return 'human wake waiting for recorded work to end'
        if state.queue_error:
            return 'human wake record queue unknown'
        if state.failure:
            return 'human wake recorded turn error; person must intervene'
        if proof is not None:
            if reason := prior_receipt(seat, joined.record, datetime.fromisoformat(proof.data['since'])):
                return reason
    if (screen is None or screen.box is None or screen.unknown or screen.interruption
            or screen.starting):
        return 'human wake box unknown or obstructed; untouched'
    if screen.box != '':
        return 'human wake box not empty; untouched'
    if screen.working or screen.queued:
        return 'human wake waiting for screen work to end'
    return None


def authorized(seat, proof, text) -> bool:
    """Only the persisted fixed reservation can enter the human exception."""
    if not seat.session.human or proof is None or text != LINE:
        return False
    current = load(seat)
    return (current is not None and current.path == proof.path and current.data == proof.data
            and proof.data['phase'] == 'prepared' and proof.same_launch(seat))


def run(seat) -> str | None:
    """Called by the serialized service (or its throwaway check), never by a sender."""
    if not seat.session.human:
        raise ShipError('human wake requires a human-owned session')
    with post.admission(seat.ship.runtime, seat.name):
        before, identity = record_stamp(seat), pending.identity(seat)
        reason = _run(seat)
        after, current = record_stamp(seat), pending.identity(seat)
        if before != after or identity != current:
            reason = 'human wake source changed; awaiting service observation'
        observe(seat, reason, current, after)
        return reason


def _run(seat) -> str | None:
    from .door import Door

    watch = post.watch_reading(seat.ship.runtime, seat.name)
    if watch.armed is None:
        return watch.reason
    try:
        proof = load(seat)
    except (OSError, ValueError, KeyError, TypeError):
        return 'human wake reservation unreadable; never resent'
    if proof is not None and proof.data['phase'] in ('pasting', 'submitting', 'submitted', 'queued'):
        # Receipt recovery uses the same pinned record and original time, even
        # after a service restart. No input is repeated to obtain a receipt.
        if proof.confirmed(seat) == 'delivered':
            proof.finish('delivered')
    if proof is not None and proof.data['phase'] == 'delivered' and not proof.data.get('ended') and proof.same_launch(seat):
        record = seat.record()
        if record is not None and str(record) == proof.data['record']:
            try:
                ended = seat.harness.state(record).turn_open is False
            except (OSError, ValueError, TypeError):
                ended = False  # no ended-turn claim; the episode remains reserved
            if ended:
                proof.save(ended=True)
    if reason := episode(proof, watch):
        return reason
    if watch.armed or not post.Attempts(seat.ship.runtime).undelivered(post.Store(seat.ship.runtime), seat.name):
        return None
    door = Door(seat)
    screen = door.screen()
    if reason := eligible(seat, screen):
        return reason
    reading = door._reading(screen)
    if not reading.open:
        return 'human wake ordinary door obstructed; no waking input'
    identity = pending.identity(seat)
    record = seat.record()
    since = post.now()
    if reason := prior_receipt(seat, record, since):
        return reason
    at = since.isoformat()
    proof = pending.Proof(seat.folder / 'human-wake.json', {})
    proof.save(format=1, seat=seat.name, identity=identity, at=at, since=at,
               payload=LINE, phase='prepared', record=str(record) if record else None,
               shown=None, attempts=[], generation=watch.generation, ended=False)
    # Keep admission through submission: watch registration and ordinary post
    # transitions cannot race this reservation. Door still verifies each input.
    delivered = door.deliver(LINE, proof=proof, human_wake=True)
    proof.finish(delivered.result)
    return None if delivered.result == 'delivered' else f'human wake {delivered.result}; inspect retained reservation'
