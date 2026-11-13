"""A detached delivery reading uses the common durable alarm owner."""

from . import alarms


PREFIX = 'prior launch has an unconfirmed delivery'


def path(runtime, name):
    return alarms.path(runtime, name, 'detached')


def load(runtime, name):
    return alarms.load(runtime, name, 'detached')


def step(service, seat, detached):
    known = all(reason.startswith(PREFIX) for reason in detached)
    key = (sorted(set(detached)) or None) if known else alarms.UNKNOWN
    alarms.notify(service, seat.name, seat.session.human, key,
                  f'{seat.name} obstructed, ' + '; '.join(sorted(set(detached))), channel='detached')
