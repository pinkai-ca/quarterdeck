"""Local push observations and one durable post after a quiet twenty minutes.

The service is the only writer. No network, credentials or GitHub client is used.
"""

from datetime import datetime, timedelta, timezone
import json
import re

from . import durable, install, post, post_dir
from .ship import ShipError


DELAY = timedelta(minutes=20)
REF = 'refs/remotes/origin/main'
TIP = re.compile(r'[0-9a-f]{40}|[0-9a-f]{64}')


def now():
    return datetime.now(timezone.utc)


def path(runtime):
    return runtime / 'post' / 'push-reminder.json'


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError('push reminder time has no timezone')
    return parsed


def load(runtime):
    try:
        data = json.loads(post_dir.read_text(path(runtime)))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise ShipError('push reminder journal unreadable; reminder held') from error
    try:
        if (not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != 1
                or set(data) != {'version', 'last_tip', 'pending', 'due', 'intent'}
                or data['last_tip'] is not None and TIP.fullmatch(data['last_tip']) is None
                or not isinstance(data['pending'], list)
                or any(not isinstance(tip, str) or TIP.fullmatch(tip) is None for tip in data['pending'])
                or bool(data['pending']) != (data['due'] is not None)):
            raise ValueError('unsupported push reminder state')
        if data['due'] is not None:
            timestamp(data['due'])
        if data['intent'] is not None:
            message = post.Message.from_json(data['intent'])
            if (message.kind != 'order' or message.sender != post.SHIP or message.to != 'una'
                    or not message.id or '/' in message.id or message.id in {'.', '..'}
                    or message.re is not None or message.due is None):
                raise ValueError('invalid push reminder intent')
            timestamp(message.due)
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise ShipError('push reminder journal invalid; reminder held') from error
    return data


def save(runtime, data):
    target = path(runtime)
    post_dir.ensure_directory(target.parent)
    durable.replace_file(target, json.dumps(data, sort_keys=True) + '\n')


def current_tip(ship):
    """Read exactly the local tracking ref; an absent ref is different from a failed read."""
    repo = install.source_repo(ship)
    rows = install.git(repo, 'for-each-ref', '--format=%(refname) %(objectname)', REF).decode().splitlines()
    for row in rows:
        name, tip = row.split()
        if name == REF:
            if TIP.fullmatch(tip) is None:
                raise ShipError('push reminder ref is not a complete object name')
            return tip
    return None


def publish(service, data):
    """A saved immutable intent is replayed by identity, including a cut after the post write."""
    intent = data['intent']
    if intent is None:
        return
    message = post.Message.from_json(intent)
    target = service.store.dir / (message.id + '.json')
    try:
        existing = target.read_text()
    except FileNotFoundError:
        service.store.write(message)
    else:
        if existing != intent:
            raise ShipError('push reminder immutable post conflict; reminder held')
    data['intent'] = None
    save(service.runtime, data)


def run(service):
    """One service pass; wall-clock deadlines persist across a service restart."""
    if 'una' not in service.ship.seats:
        return
    data = load(service.runtime)
    if data is not None:
        publish(service, data)
    at = now()
    try:
        tip = current_tip(service.ship)
    except (ShipError, OSError, ValueError) as error:
        # An unavailable ref cannot erase a batch or prevent its already-noted reminder.
        reading = (type(error).__name__, str(error))
        if service.__dict__.get('push_reminder_ref_error') != reading:
            service.log('error', step='push-reminder-ref', error=str(error))
            service.push_reminder_ref_error = reading
        if data is None:
            return
        tip = None
    else:
        if service.__dict__.pop('push_reminder_ref_error', None) is not None:
            service.log('push-reminder-ref-available')
    if data is None:
        save(service.runtime, dict(version=1, last_tip=tip, pending=[], due=None, intent=None))
        return  # first reading is a baseline, not an inferred push
    if tip is not None and tip != data['last_tip']:
        data['last_tip'] = tip
        if tip not in data['pending']:
            data['pending'].append(tip)
        data['due'] = (at + DELAY).isoformat()
        save(service.runtime, data)
    if data['due'] is None or at < timestamp(data['due']):
        return
    body = ('Read GitHub\'s run on each of these pushed main tips:\n'
            + ''.join(f'- {tip}\n' for tip in data['pending'])
            + '\nReport a failed run to the captain at once, with its cause where the log shows it. '
            'If a run is still going, read it to its end with the ship\'s key: '
            '`gh run watch RUN_ID --exit-status` blocks until it ends; no seat polls. '
            'You remain the only reader of GitHub\'s runs.')
    message = service.store.prepare('order', post.SHIP, 'una', body,
                                    subject='Read GitHub runs after pushes of main', due=data['due'])
    data.update(pending=[], due=None, intent=message.to_json())
    save(service.runtime, data)  # immutable publication identity precedes the post
    publish(service, data)
