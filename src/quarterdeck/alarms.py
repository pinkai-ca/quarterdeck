"""Durable alarm readings and post-based taking; never a delivery or lifecycle decision."""

from contextlib import ExitStack
from datetime import datetime, timezone
import json
import re

from . import compaction, durable, post, post_dir
from .ship import ShipError


ESCALATE_SECONDS = 600
UNKNOWN = object()
FILES = {'human': 'human-alert.json', 'detached': 'unconfirmed-delivery-alert.json',
         'selection': 'selection-alert.json', 'manual-fresh': 'manual-fresh-alert.json',
         'write-bound-floor': 'write-bound-floor-alert.json'}


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
    if not hasattr(ship, 'seats'):
        return None  # a callback-only view supplies no role evidence for taking
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


REPORT_ID = re.compile(r'rep-[A-Za-z0-9-]{1,80}')  # a report's plain name; an existing one is only compared


def _rebuilt(service, runtime, name, channel, prior, intent, current, allowed, accepts=None) -> str | None:
    """The report to publish for a saved one: rebuilt by the service, or None when refused (logged once).

    The journal sits in the seat's folder, which its jail can write. So a saved report not yet filed is published
    only as the service builds it now: from the file it keeps only the id and the recipient, each checked (an id of
    the store's shape for a report from ship, and a recipient the ship file names for alarms); the body is this
    pass's own reading of the alarm. One already in the store is only compared, never filed again.
    """
    message = compaction.decode(intent)
    if post_dir.exists(post.Store(runtime).dir / f'{message.id}.json'):
        return intent  # filed before: the store's own bytes decide, and nothing new is filed
    reason = ('not a plain report from ship' if (message.kind != 'report' or message.sender != post.SHIP
                                                 or message.re is not None or message.subject is not None
                                                 or message.due is not None or message.speed is not None
                                                 or REPORT_ID.fullmatch(message.id) is None) else
              f'recipient {message.to!r} is not one the ship file names for alarms' if message.to not in allowed else
              None)
    if reason is not None:
        refused = service.__dict__.setdefault('alarm_refused', set())
        if (name, channel, message.id) not in refused:
            refused.add((name, channel, message.id))
            service.log('alarm-refused', seat=name, channel=channel, message=message.id, reason=reason)
        return None
    if current is None:
        return None  # no reading of this alarm stands on this pass: nothing of the file's words is published
    if message.body == current or (accepts is not None and accepts(message.body)):
        return intent  # the very words the service builds for this standing reading
    stamp = message.at if abs((now() - message.at).total_seconds()) < 86400 else now()
    rebuilt = post.Message(message.id, 'report', post.SHIP, message.to, stamp, current).to_json()
    if rebuilt != intent:
        prior['reports'][prior['reports'].index(intent)] = rebuilt
        save(runtime, name, channel, prior)  # the service's own bytes, saved before they are filed
    return rebuilt


def publish(service, runtime, name, channel, data, intent):
    from .compaction_alert import publish as publish_intent
    message = compaction.decode(intent)
    with post.fixed_report(service.store, message):
        publish_intent(runtime, intent)
    data['published'].append(message.id)
    save(runtime, name, channel, data)


def notify(service, name, human, key, body, *, channel='human', ship=None, up=None, desk_now=False,
           initial=None, recover_initial=True, accepts=None):
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
    # The journal sits in the seat's folder, which its jail can write: a saved report is published only as the
    # service would write it now. Its body must be this pass's own reading of the same alarm, and its recipient one
    # the ship file names for alarms (a systems seat or the desk, or the caller's own list). Anything else is
    # refused, logged once, and never published.
    current = (body() if callable(body) else body) if key is not UNKNOWN and key is not None else None
    allowed = set(initial or ()) | {spec.name for spec in getattr(ship, 'seats', {}).values()
                                     if spec.role in ('systems', 'desk') and spec.name != name}
    # Recover initial reports through the existing sink while their reading stands: across a changed or cleared
    # reading the old words are only in the file, which its seat's jail can write, so they are not published.
    # Timed desk escalation is deliberately excluded from this recovery.
    names = {name, *(compaction.decode(intent).to for intent in prior['reports'])}
    standing = current if key == prior['key'] else None
    with ExitStack() as held:
        for recipient in sorted(names & (allowed | {name})):
            held.enter_context(post.admission(runtime, recipient))
        for intent in list(prior['reports']):
            identity = compaction.decode(intent).id
            if recover_initial and identity not in prior['published'] and identity not in prior['escalations']:
                if (rebuilt := _rebuilt(service, runtime, name, channel, prior, intent, standing, allowed, accepts)):
                    publish(service, runtime, name, channel, prior, rebuilt)
    if key is UNKNOWN:
        return
    if key is None and prior['key'] is None and not prior['reports']:
        return
    observed = now()
    if key != prior['key']:
        text = current
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
        # The existing callback also supports views without a role declaration;
        # their supplied audience stays authoritative, with no invented roles.
        declared = getattr(ship, 'seats', {})
        up = [recipient for recipient in initial
              if recipient in {spec.name for spec in declared.values() if spec.role == 'systems'}]
        if up and elapsed >= ESCALATE_SECONDS and not prior['taken']:
            eligible += [spec.name for spec in declared.values()
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
            if (message.to in eligible and message.id not in prior['published']
                    and (rebuilt := _rebuilt(service, runtime, name, channel, prior, intent, current, allowed, accepts))):
                publish(service, runtime, name, channel, prior, rebuilt)
