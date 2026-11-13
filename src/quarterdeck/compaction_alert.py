"""Notify systems of an idle manual-fresh reading; never authorize a lifecycle action.

The existing compaction pass calls this once. Fixed report intents survive service
restart independently of the compaction generation that the controller may retire.
"""

import json

from . import compaction, durable, post
from .ship import ShipError


RELEASED = frozenset({
    'new incoming work; prepare a new handoff and renew readiness',
    'readiness stale; prepare a new handoff and renew readiness',
    'attestation stale; prepare a new handoff and renew readiness',
    'readiness cancelled; handoffs remain open',
    'declaration changed; renew readiness',
    'ordinary lifecycle action; prepare a current handoff',
})
UNKNOWN = object()


def path(runtime, name):
    return compaction.path(runtime, name).with_name('manual-fresh-alert.json')


def load(runtime, name):
    try:
        data = json.loads(path(runtime, name).read_text())
    except FileNotFoundError:
        return dict(version=1, key=None, body=None, reports=[])
    except (OSError, ValueError) as error:
        raise ShipError('manual fresh alert journal unreadable; reports held') from error
    if (not isinstance(data, dict) or not {'version', 'key', 'body', 'reports'} <= data.keys()
            or type(data.get('version')) is not int or data['version'] != 1
            or not isinstance(data.get('reports'), list)
            or data.get('key') is not None and not isinstance(data['key'], dict)):
        raise ShipError('manual fresh alert journal unsupported; reports held')
    key = data.get('key')
    if key is not None and (not isinstance(key.get('generation'), str)
                            or not isinstance(key.get('source'), dict)
                            or not isinstance(key.get('reading'), str)
                            or not isinstance(data['body'], str)):
        raise ShipError('manual fresh alert reading invalid; reports held')
    for intent in data['reports']:
        if not isinstance(intent, str):
            raise ShipError('manual fresh alert intent invalid; reports held')
        message = compaction.decode(intent)
        if (message.kind != 'report' or message.sender != post.SHIP
                or message.to == name and not data.get('human')):
            raise ShipError('manual fresh alert recipient invalid; reports held')
    return data


def save(runtime, name, data):
    target = path(runtime, name)
    text = json.dumps(data, sort_keys=True) + '\n'
    if target.exists() and target.read_text() == text:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    durable.replace_file(target, text)


def observation(seat):
    """Return a safe standing key, a proved clear, or UNKNOWN (which cannot rearm).

    Unknown tools are admissible for this notification only. An unknown record or
    turn is never screen-idle proof, and none of this supplies fresh clearance.
    """
    if not seat.spec.fresh_after_compactions or not seat.spec.up or not seat.is_up:
        return None, None
    data = compaction.load(seat.ship.runtime, seat.name)
    if data is None or data.get('error') or data.get('reading_unknown') or data.get('count') is None:
        return UNKNOWN, None
    if data.get('disabled') or data.get('transition') or data['count'] < seat.spec.fresh_after_compactions:
        return None, data
    record, reading, launched = compaction.current(seat)
    source = dict(record=str(record), harness=seat.spec.harness, conversation=reading.conversation)
    if (not compaction.live_identity(seat) or data['source'] != source
            or data['source_launch'] != compaction.key_for(launched)
            or (data['count'], data.get('extent'), data.get('digest')) !=
               (reading.count, reading.extent, reading.digest)):
        return UNKNOWN, data
    manual = (data.get('manual') or '').startswith('ready for manual fresh:')
    released = not data.get('ready') and not data.get('attestation') and data['reason'] in RELEASED
    if not (manual or released):
        return None, data
    state = reading.state
    if state.turn_open is None or state.queue_error or state.queued is None:
        return UNKNOWN, data
    if (state.turn_open is not False or state.queued or state.open_tool is not None
            or state.pending_tools not in ((), None)):
        return UNKNOWN, data  # activity does not clear a standing compaction reading
    return dict(generation=data['generation'], source=source,
                reading=compaction.status(seat, alerts_only=True)), data


def handoffs(store, name, data):
    """Open self-handoffs by routing metadata only; inventory is never a dedup key."""
    requests = set(data['protocol_requests'])
    return [message.id for message in store.open_for(name)
            if message.kind == 'order' and message.sender == name and message.re in requests]


def publish(runtime, intent):
    message = compaction.decode(intent)
    target = post.Store(runtime).dir / (message.id + '.json')
    if not target.exists():
        # The caller owns sorted subject/recipient admission. A newly published
        # alarm is ordinary incoming work for its recipient; recovery is not.
        compaction.admitting(runtime, message)
        sink = post.report_sink(message, post.Store(runtime))
        with post.fixed_report(sink, message):
            sink.new(message.kind, message.sender, message.to, message.body)
    else:
        compaction.publish(runtime, intent)


def step(service, seat):
    from . import alarms
    key, data = observation(seat)
    if key is UNKNOWN:
        key = alarms.UNKNOWN

    def body():
        open_handoffs = ', '.join(handoffs(service.store, seat.name, data)) or 'none open'
        return (f'Observed {seat.name}: idle in its record; {key["reading"]}. '
                f'Open compaction handoff(s) at observation: {open_handoffs}. '
                'Notification only; execution may remain unknown. The outside controller '
                "must choose the idle break under the ship's orders.")
    active = key is not None and key is not alarms.UNKNOWN
    session = getattr(seat, 'session', None)
    human = session.human if active and session is not None else None
    up = service.systems_recipients(seat) if active else None
    alarms.notify(service, seat.name, human, key, body, channel='manual-fresh', ship=seat.ship, up=up)


def unrouted(ship, selected):
    """Named fallback alarms from journals, without probing native records or screens."""
    up_systems = {seat.name for seat in selected
                  if seat.spec.role == 'systems' and seat.is_up}
    lines = []
    for seat in selected:
        if up_systems - {seat.name}:
            continue
        try:
            key = load(ship.runtime, seat.name)['key']
            data = compaction.load(ship.runtime, seat.name)
            if (key and data and seat.spec.fresh_after_compactions
                    and key['generation'] == data['generation'] and key['source'] == data['source']
                    and key['reading'] == compaction.status(seat, alerts_only=True)):
                lines.append(f'{seat.name}: {key["reading"]}; no systems seat up')
        except (OSError, ShipError):
            lines.append(f'{seat.name}: manual fresh alert unknown; no systems seat up')
    return lines
