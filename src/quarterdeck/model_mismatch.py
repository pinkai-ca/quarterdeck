"""Record-first model differences and fixed, deduplicated systems notifications.

Only canonical model identities and prepared operational report intents are journaled. Native record or
screen payloads never enter this journal. Pending-control suppression comes from the one request owner.
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
import json
from pathlib import Path

from . import durable, post, record_owner
from .door import Door
from .harness.base import RecordError
from .ship import ShipError


@dataclass(frozen=True)
class Reading:
    status: str
    declared: str | None = None
    running: str | None = None
    source: str | None = None
    reason: str = ''


def compare(harness, declared, running, *, version=None, source=None) -> Reading:
    """Only the adapter's identity rule equates names; no generic prefix/punctuation aliases."""
    expected = harness.model_identity(declared, version=version)
    actual = harness.model_identity(running, version=version)
    if expected is None or actual is None:
        return Reading('unknown', reason='model identity is not proved by the adapter')
    return Reading('match' if expected == actual else 'mismatch', expected, actual, source)


def observe(seat) -> Reading:
    if seat.spec.model_mode == 'program-default' or not seat.spec.model:
        return Reading('not-declared', reason='the program chooses its model')
    try:
        launched, pane = seat.launched(), seat.pane()
        if (launched is None or pane is None or pane.dead
                or record_owner.launch_reader(launched) != seat.harness.name):
            return Reading('unknown', reason='current model launch is not proved')
        record = seat.record()
        try:
            model = seat.harness.record_model(record) if record is not None else None
        except (OSError, ValueError, TypeError, RecordError):
            model = None
        source = 'record'
        if model is None:
            screen = Door(seat).screen()
            model = screen.shows.get('model') if screen is not None else None
            source = 'screen'
        current = seat.pane()
        if (seat.launched() != launched or current is None or current.dead
                or (current.id, current.pid) != (pane.id, pane.pid)):
            return Reading('unknown', reason='model source changed while reading')
        return compare(seat.harness, seat.spec.model, model, version=launched.version, source=source)
    except (OSError, ValueError, TypeError, ShipError):
        return Reading('unknown', reason='model source unreadable')


def path(runtime, name):
    return runtime / 'seats' / name / 'model-mismatch.json'


def report_intent(intent):
    try:
        message = post.Message.from_json(intent)
        if (not isinstance(message.id, str) or not message.id or Path(message.id).name != message.id
                or message.kind != 'report' or message.sender != post.SHIP
                or not isinstance(message.to, str) or not message.to
                or message.re is not None or message.speed is not None or message.at.tzinfo is None):
            raise ValueError('report intent')
        return message
    except (ValueError, TypeError, AttributeError, KeyError):
        raise ShipError('model mismatch report intent unsupported; retained') from None


def load(runtime, name):
    try:
        data = json.loads(path(runtime, name).read_text())
    except FileNotFoundError:
        return dict(version=1, key=None, active=False, reports=[])
    except (OSError, ValueError) as error:
        raise ShipError('model mismatch notification state unreadable; reports held') from error
    if (not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != 1
            or type(data.get('active')) is not bool or not isinstance(data.get('reports'), list)
            or 'key' not in data
            or data.get('key') is not None and (not isinstance(data['key'], list) or len(data['key']) != 3
                                               or any(not isinstance(value, str) for value in data['key']))):
        raise ShipError('model mismatch notification state unsupported; reports held')
    key = data['key']
    if (data['active'] and key is None or key is not None and any(
            not value or len(value) > 200 or not all(char.isprintable() for char in value) for value in key)):
        raise ShipError('model mismatch identity unsupported; reports held')
    bodies = ({f'{name}: model mismatch; ship declares {key[1]}, program reports {key[2]} in its {source}'
               for source in ('record', 'screen')} if key else set())
    for intent in data['reports']:
        message = report_intent(intent)
        if message.to == name or message.body not in bodies:
            raise ShipError('model mismatch notification recipient unsupported; reports held')
    return data


def save(runtime, name, data):
    target = path(runtime, name)
    text = json.dumps(data, sort_keys=True) + '\n'
    if target.exists() and target.read_text() == text:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    durable.replace_file(target, text)


def publish(store, intent):
    """Recover the same immutable report under recipient admission, without generating another ID."""
    message = report_intent(intent)
    target = store.dir / (message.id + '.json')
    if target.exists():
        if target.read_text() != intent:
            raise ShipError('model mismatch immutable report conflict; retained')
    else:
        from . import compaction
        compaction.admitting(store.dir.parent.parent, message)
        store._write(message)


def step(service, seat, pending_model):
    """The service owns this journal; pending_model is supplied by the existing request owner."""
    runtime = seat.ship.runtime
    prior = load(runtime, seat.name)
    recipients = service.systems_recipients(seat)
    names = {seat.name, *recipients, *(post.Message.from_json(intent).to for intent in prior['reports'])}
    with ExitStack() as held:
        for name in sorted(names):
            held.enter_context(post.admission(runtime, name))
        for intent in prior['reports']:
            publish(service.store, intent)
        launched = seat.launched()
        expected = seat.harness.model_identity(seat.spec.model, version=launched.version if launched else None)
        if expected is not None and pending_model(seat, expected) is True:
            if prior['active']:
                prior['active'] = False
                save(runtime, seat.name, prior)
            return  # a proved matching pending control suppresses even an unreadable current model
        reading = observe(seat)
        if reading.status == 'unknown':
            return  # unknown cannot clear a previously observed difference
        active = reading.status == 'mismatch'
        if active:
            key = [seat.harness.name, reading.declared, reading.running]
            if key != prior['key']:
                prior = dict(version=1, key=key, active=True, reports=[])
            prior['source'] = reading.source
            prior['active'] = True
            told = {post.Message.from_json(intent).to for intent in prior['reports']}
            planned = {post.Message.from_json(intent).short for intent in prior['reports']}
            body = (f'{seat.name}: model mismatch; ship declares {reading.declared}, '
                    f'program reports {reading.running} in its {reading.source}')
            for name in recipients:
                if name not in told:
                    while True:
                        message = service.store.prepare('report', post.SHIP, name, body)
                        if message.short not in planned:
                            break
                    prior['reports'].append(message.to_json())
                    told.add(name)
                    planned.add(message.short)
        else:
            prior['active'] = False
        if not path(runtime, seat.name).exists() and not active:
            return
        save(runtime, seat.name, prior)  # fixed intents precede all publication, including after restart
        for intent in prior['reports']:
            publish(service.store, intent)


def unrouted(ship, selected, pending_model=None):
    """Status uses the operational journal, including named differences when no systems seat is up."""
    systems = {seat.name for seat in selected if seat.spec.role == 'systems' and seat.is_up}
    lines = []
    for seat in selected:
        if systems - {seat.name}:
            continue
        try:
            data = load(ship.runtime, seat.name)
            key = data['key']
            launched = seat.launched()
            expected = seat.harness.model_identity(seat.spec.model, version=launched.version if launched else None)
            if (expected is not None and pending_model is not None
                    and pending_model(seat, expected) is True):
                continue
            if (data['active'] and key and seat.spec.model_mode != 'program-default'
                    and key[0] == seat.harness.name and key[1] == expected):
                lines.append(f'{seat.name}: model mismatch {key[2]} / declared {key[1]}; no systems seat up')
        except (OSError, ValueError, TypeError, ShipError):
            lines.append(f'{seat.name}: model mismatch reading unknown; no systems seat up')
    return lines
