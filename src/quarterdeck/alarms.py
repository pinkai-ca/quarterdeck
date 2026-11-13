"""Durable alarm readings and post-based taking; never a delivery or lifecycle decision."""

from contextlib import ExitStack
from datetime import datetime, timezone
import json

from . import compaction, durable, post
from .ship import ShipError


ESCALATE_SECONDS = 600
UNKNOWN = object()
FILES = {'human': 'human-alert.json', 'detached': 'unconfirmed-delivery-alert.json',
         'selection': 'selection-alert.json', 'manual-fresh': 'manual-fresh-alert.json'}


def now():
    return datetime.now(timezone.utc)


def path(runtime, name, channel):
    return compaction.path(runtime, name).with_name(FILES[channel])


def load(runtime, name, channel):
    try:
        data = json.loads(path(runtime, name, channel).read_text())
    except FileNotFoundError:
        return dict(version=1, key=None, body=None, reports=[], first_at=None, taken=None,
                    published=[], escalations=[])
    except (OSError, ValueError) as error:
        raise ShipError('alarm journal unreadable; reports held') from error
    if (not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != 1
            or not {'key', 'body', 'reports'} <= data.keys() or not isinstance(data['reports'], list)
            or data['key'] is not None and not isinstance(data['body'], str)):
        raise ShipError('alarm journal unsupported; reports held')
    # Old manual-fresh journals predate escalation. Start the clock only when
    # their standing reading is next observed, never from a guessed old time.
    data.setdefault('first_at', None)
    data.setdefault('taken', None)
    data.setdefault('published', [])
    data.setdefault('escalations', [])
    try:
        if data['first_at'] is not None and datetime.fromisoformat(data['first_at']).tzinfo is None:
            raise ValueError('alarm time has no timezone')
        if data['taken'] is not None and not isinstance(data['taken'], str):
            raise ValueError('alarm taking identity invalid')
        if any(not isinstance(data[field], list) or any(not isinstance(item, str) for item in data[field])
               for field in ('published', 'escalations')):
            raise ValueError('alarm publication identities invalid')
        for intent in data['reports']:
            message = compaction.decode(intent)
            if message.kind != 'report' or message.sender != post.SHIP or message.re is not None:
                raise ValueError('alarm report identity invalid')
    except (ValueError, TypeError, ShipError) as error:
        raise ShipError('alarm evidence invalid; reports held') from error
    return data


def save(runtime, name, channel, data):
    target = path(runtime, name, channel)
    text = json.dumps(data, sort_keys=True) + '\n'
    if target.exists() and target.read_text() == text:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    durable.replace_file(target, text)


def systems(service, name, *, ship=None):
    ship = service.ship if ship is None else ship
    return [spec.name for spec in ship.seats.values()
            if spec.role == 'systems' and spec.name != name and service._up(spec.name)]


def recipients(service, name, human, *, elapsed=0, taken=False, ship=None, up=None, desk_now=False):
    ship = service.ship if ship is None else ship
    desks = [spec.name for spec in ship.seats.values() if spec.role == 'desk' and spec.name != name]
    up = systems(service, name, ship=ship) if up is None else up
    if not up:
        return desks
    return (up + [desk for desk in desks if desk not in up]
            if human or desk_now or elapsed >= ESCALATE_SECONDS and not taken else up)


def taking(service, reports, *, ship=None):
    """Only routing metadata: a systems sender anywhere below an alarm report.

    An acknowledgment takes the alarm without closing a human's order. Other
    kinds of reply count too; activity, launches and unrelated posts do not.
    """
    ship = service.ship if ship is None else ship
    senders = {spec.name for spec in ship.seats.values() if spec.role == 'systems'}
    store = service.store if isinstance(service.store, post.Store) else post.Store(ship.runtime)
    pending = [compaction.decode(intent) for intent in reports]
    visited = {message.id for message in pending}
    while pending:
        for message in store.replies(pending.pop()):
            if message.id in visited:
                continue
            visited.add(message.id)
            if message.sender in senders:
                return message.id
            pending.append(message)
    return None


def publish(service, runtime, name, channel, data, intent):
    from .compaction_alert import publish as publish_intent
    message = compaction.decode(intent)
    with post.fixed_report(service.store, message):
        publish_intent(runtime, intent)
    data['published'].append(message.id)
    save(runtime, name, channel, data)


def notify(service, name, human, key, body, *, channel='human', ship=None, up=None, desk_now=False,
           initial=None, recover_initial=True):
    """Observe once on the existing pass, saving identities before publication.

    Committed initial notifications survive a changed reading. A cleared or
    unknown reading cannot authorize a pending escalation. Published posts stay
    immutable, and unchanged readings reuse their saved notification identities.
    A bounded renewal wait may retire its obsolete unpublished attention notice.
    """
    ship = service.ship if ship is None else ship
    runtime = ship.runtime
    store = service.store if isinstance(service.store, post.Store) else post.Store(runtime)
    prior = load(runtime, name, channel)
    if not prior['reports'] and (key is UNKNOWN or key is None and prior['key'] is None):
        return
    # Recover initial reports through the existing sink, including across a
    # clear. Timed desk escalation is deliberately excluded from this recovery.
    names = {name, *(compaction.decode(intent).to for intent in prior['reports'])}
    with ExitStack() as held:
        for recipient in sorted(names):
            held.enter_context(post.admission(runtime, recipient))
        for intent in prior['reports']:
            identity = compaction.decode(intent).id
            if recover_initial and identity not in prior['published'] and identity not in prior['escalations']:
                publish(service, runtime, name, channel, prior, intent)
    if key is UNKNOWN:
        return
    if key is None and prior['key'] is None and not prior['reports']:
        return
    observed = now()
    if key != prior['key']:
        text = (body() if callable(body) else body) if key is not None else None
        prior = dict(version=1, key=key, body=text,
                     reports=[], first_at=observed.isoformat() if key is not None else None, taken=None,
                     published=[], escalations=[])
    if key is not None and prior['first_at'] is None:
        prior['first_at'] = observed.isoformat()
    if human is not None:
        prior['human'] = human
    if key is not None and not prior['taken'] and prior['reports']:
        prior['taken'] = taking(service, prior['reports'], ship=ship)
    elapsed = (observed - datetime.fromisoformat(prior['first_at'])).total_seconds() if key is not None else 0
    # Some notification views supply only an explicit recipient callback, with
    # no session metadata. Keep that port authoritative rather than inventing
    # whether its subject belongs to a human. Full seats supply the session.
    if key is None:
        eligible = []
    elif initial is not None:
        eligible = list(initial)
        up = [recipient for recipient in initial
              if recipient in {spec.name for spec in ship.seats.values() if spec.role == 'systems'}]
        if up and elapsed >= ESCALATE_SECONDS and not prior['taken']:
            eligible += [spec.name for spec in ship.seats.values()
                         if spec.role == 'desk' and spec.name != name and spec.name not in eligible]
    else:
        eligible = (list(up) if human is None and up is not None else
                    recipients(service, name, human, elapsed=elapsed, taken=prior['taken'] is not None,
                               ship=ship, up=up, desk_now=desk_now))
    names = {name, *eligible, *(compaction.decode(intent).to for intent in prior['reports'])}
    with ExitStack() as held:
        for recipient in sorted(names):
            held.enter_context(post.admission(runtime, recipient))
        told = {compaction.decode(intent).to for intent in prior['reports']}
        planned = {compaction.decode(intent).short for intent in prior['reports']}
        for recipient in eligible:
            if recipient not in told:
                while True:
                    message = store.prepare('report', post.SHIP, recipient, prior['body'])
                    if message.short not in planned:
                        break
                planned.add(message.short)
                prior['reports'].append(message.to_json())
                if (not human and not desk_now and elapsed >= ESCALATE_SECONDS
                        and recipient in {spec.name for spec in ship.seats.values() if spec.role == 'desk'}
                        and (up if up is not None else systems(service, name, ship=ship))):
                    prior['escalations'].append(message.id)
        save(runtime, name, channel, prior)
        for intent in prior['reports']:
            message = compaction.decode(intent)
            if message.to in eligible and message.id not in prior['published']:
                publish(service, runtime, name, channel, prior, intent)
