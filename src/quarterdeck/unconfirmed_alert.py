"""Standing prior-launch delivery alarms, independent of the current door's frame.

Only notification state lives here. Delivery journals and the watch remain the
authorities for the hold; reporting it never resolves or conceals it.
"""

from contextlib import ExitStack
import json

from . import compaction, compaction_alert, durable, post
from .ship import ShipError


PREFIX = 'prior launch has an unconfirmed delivery'


def path(runtime, name):
    return compaction.path(runtime, name).with_name('unconfirmed-delivery-alert.json')


def load(runtime, name):
    try:
        data = json.loads(path(runtime, name).read_text())
    except FileNotFoundError:
        return dict(version=1, key=None, reports=[])
    except (OSError, ValueError) as error:
        raise ShipError('unconfirmed delivery alert journal unreadable; reports held') from error
    if (not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != 1
            or not {'key', 'reports'} <= data.keys() or not isinstance(data['reports'], list)
            or data['key'] is not None and (not isinstance(data['key'], list) or not data['key']
                or any(not isinstance(reason, str) or not reason.startswith(PREFIX) for reason in data['key']))):
        raise ShipError('unconfirmed delivery alert journal unsupported; reports held')
    for intent in data['reports']:
        if not isinstance(intent, str):
            raise ShipError('unconfirmed delivery alert intent invalid; reports held')
        message = compaction.decode(intent)
        if (message.kind != 'report' or message.sender != post.SHIP
                or message.to == name or message.re is not None):
            raise ShipError('unconfirmed delivery alert recipient invalid; reports held')
    return data


def save(runtime, name, data):
    target = path(runtime, name)
    text = json.dumps(data, sort_keys=True) + '\n'
    if target.exists() and target.read_text() == text:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    durable.replace_file(target, text)


def step(service, seat, detached):
    """Report once per recipient/reading, including across service restarts.

    Transient door obstructions and new launches do not clear an old submission.
    An unreadable detached journal cannot prove a clear either. The existing
    delivery pass supplies these readings; no timer or receipt decision is added.
    """
    runtime = service.runtime
    prior = load(runtime, seat.name)
    known = all(reason.startswith(PREFIX) for reason in detached)
    key = sorted(set(detached)) or None
    if known and key is None and prior['key'] is None and not prior['reports']:
        return
    recipients = ([name for name in service.alarm_recipients(seat, key[0]) if name != seat.name]
                  if known and key else [])
    names = {seat.name, *recipients, *(compaction.decode(intent).to for intent in prior['reports'])}
    with ExitStack() as held:
        for name in sorted(names):
            held.enter_context(post.admission(runtime, name))
        # Recover committed intents even if a later observation changed or cleared.
        for intent in prior['reports']:
            compaction_alert.publish(runtime, intent)
        if not known:
            return
        if key != prior['key']:
            prior = dict(version=1, key=key, reports=[])
        told = {compaction.decode(intent).to for intent in prior['reports']}
        planned = {compaction.decode(intent).short for intent in prior['reports']}
        for name in recipients:
            if name not in told:
                while True:
                    message = service.store.prepare('report', post.SHIP, name,
                                                    f'{seat.name} obstructed, ' + '; '.join(key))
                    if message.short not in planned:
                        break
                planned.add(message.short)
                prior['reports'].append(message.to_json())
        if prior['key'] is None and not path(runtime, seat.name).exists():
            return
        save(runtime, seat.name, prior)  # fixed identities and bytes precede publication
        for intent in prior['reports']:
            compaction_alert.publish(runtime, intent)
