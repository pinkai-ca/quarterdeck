"""Recorded forwarding routes; original posts and their addresses remain bytes."""

from dataclasses import replace
from functools import lru_cache
import json
from pathlib import Path

from . import ship as shipmod
from .ship import ShipError


@lru_cache(maxsize=64)
def _declaration(folder, stamp):
    data, place = shipmod.toml_table((Path(folder) / 'ship.toml').read_text())
    if data is None:  # a ValueError as the parser's was; never its message, which can quote the file
        raise ValueError(f'ship.toml is not valid TOML at {place}')
    return shipmod._forward(data.get('forward', {}))


def declared(runtime):
    path = runtime.parent / 'ship.toml'
    try:
        s = path.stat()
    except FileNotFoundError:
        return {}
    return _declaration(str(path.parent), (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns))


def path(runtime, identifier):
    return runtime / 'post' / 'forwarded' / (identifier + '.json')


_UNREAD = object()  # the message's import record, not read by the caller: read it here


def recorded(runtime, identifier, *, imported=_UNREAD):
    """The message's saved route, or None. ``imported``: its import record when the caller has read it already
    (None: it has none), so it is not read again."""
    from . import fleet_post as transport, post
    def read():
        record = transport.provenance(runtime, identifier) if imported is _UNREAD else imported
        if record and record.get('forward'):
            return record['forward']
        if post.reading_missing(runtime / 'post' / 'forwarded', identifier + '.json'):
            return None
        try:
            return json.loads(transport.stable_bytes(path(runtime, identifier)))
        except FileNotFoundError:
            return None
    return post.reading_value('forward', (runtime.absolute(), identifier), read)


def resolve(ship, value):
    from .fleet import peers
    from .fleet_post import address
    fleet, trail = peers(ship), []
    seat, name = address(value, ship.folder.name)
    current = f'{seat}@{name}'
    while current not in trail:
        trail.append(current)
        seat, name = address(current, ship.folder.name)
        if name not in fleet:
            raise ShipError(f'forward destination {name} unavailable; message retained')
        target_ship = fleet[name]
        target = getattr(target_ship, 'forward', {}).get(seat)
        if target is None:
            if seat not in target_ship.seats and seat not in (target_ship.operator.lower(), 'ship'):
                raise ShipError(f'forward destination {current} has no seat; message retained')
            return trail
        current = target
    raise ShipError('forward route has a loop; message retained')


def projected(runtime, message, *, open_obligation=False, imported=_UNREAD):
    """Only a saved route changes historical party identity; a declaration hides old dispatch now. ``imported`` as
    for ``recorded``."""
    from .fleet_post import address, owner
    here = owner(runtime)
    if here is None:
        return message
    saved = recorded(runtime, message.id, imported=imported)
    if saved:
        seat, name = address(message.to, here)
        if f'{seat}@{name}' == saved['from']:
            target, destination = address(saved['to'], here)
            return replace(message, to=target if destination == here else saved['to'])
        return message
    target = declared(runtime).get(message.to) if open_obligation else None
    if target:
        from . import compaction
        reserved, transition = compaction.withheld(runtime, message.to)
        if message.id in reserved or transition:
            return message
    return replace(message, to=target) if target else message


def stamp(runtime):
    """A route/declaration change invalidates an existing Store's derived views."""
    result = []
    for item in (runtime.parent / 'ship.toml', runtime / 'post' / 'forwarded'):
        try:
            s = item.stat()
            result.append((s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns))
        except FileNotFoundError:
            result.append(None)
    return tuple(result)


def pin(ship, message):
    from . import fleet_post as transport, post
    raw, data = transport._source(ship, message.id)
    trail = resolve(ship, raw.to)
    saved = recorded(ship.runtime, message.id)
    if saved:
        if saved['via'] != trail or saved['sha256'] != transport.digest(data):
            raise ShipError('forward route changed after publication; original route retained')
        return saved
    if len(trail) == 1:
        return None
    if not post.Store(ship.runtime).is_open(message):
        return None
    from .fleet import peers
    fleet = peers(ship)

    def available(runtime, seat):
        from . import compaction
        reserved, transition = compaction.withheld(runtime, seat)
        if message.id in reserved or transition:
            raise ShipError('old address has a reserved successor handoff or transition; forwarding held')
        latest = post.Attempts(runtime).for_message(message.id)
        resolved = {'delivered', 'watch', 'launch', 'carried', 'abandoned'}
        if latest and latest[-1].result not in resolved:
            raise ShipError('old address has an unresolved delivery; forwarding held')

    for old in trail[:-1]:
        seat, name = transport.address(old, ship.folder.name)
        available(fleet[name].runtime, seat)
    seat, origin = transport.address(raw.to, ship.folder.name)
    if origin == ship.folder.name:
        with post.admission(ship.runtime, seat):
            available(ship.runtime, seat)
            saved = dict(format=1, id=message.id, sha256=transport.digest(data),
                         **{'from': trail[0], 'to': trail[-1], 'via': trail})
            transport.publish(path(ship.runtime, message.id), transport.encoded(saved))
    else:
        saved = dict(format=1, id=message.id, sha256=transport.digest(data),
                     **{'from': trail[0], 'to': trail[-1], 'via': trail})
        transport.publish(path(ship.runtime, message.id), transport.encoded(saved))
    return saved


def target(ship, message, store):
    saved = recorded(ship.runtime, message.id)
    if saved:
        return saved['to']
    if not store.is_open(message):
        return message.to
    if '@' not in message.to and message.to not in getattr(ship, 'forward', {}):
        return message.to
    return resolve(ship, message.to)[-1]


def prepare(runtime, message):
    if message.to in declared(runtime):
        from .fleet_post import prepare_outbound
        prepare_outbound(runtime)


def receive(runtime, entry, raw, data, *, adopt=False, source=None):
    """Validate the route carried with this immutable file before making it visible."""
    from . import fleet_post as transport
    saved = entry.get('forward')
    if not saved:
        return
    seat, origin = transport.address(raw.to, entry['origin'])
    if (saved.get('format') != 1 or saved.get('id') != raw.id
            or saved.get('sha256') != transport.digest(data)
            or saved.get('from') != f'{seat}@{origin}'
            or not isinstance(saved.get('via'), list) or len(saved['via']) < 2
            or saved['via'][0] != saved['from'] or saved['via'][-1] != saved.get('to')
            or len(set(saved['via'])) != len(saved['via'])):
        raise ShipError('forward provenance does not match the original post')
    for value in saved['via']:
        transport.address(value, entry['origin'])
    # The first-address ship declares the route independently of the post's author.
    # Reading that declaration also permits an author's first outbound offer before
    # the declaring ship has ever fetched the file. No peer can invent its route.
    from .fleet import peers
    ship = shipmod.load(runtime.parent)
    authority = peers(ship).get(origin)
    if authority is None or resolve(authority, saved['from']) != saved['via']:
        raise ShipError('forward route differs from the declaring ship; retained')
    pinned = recorded(authority.runtime, raw.id)
    publisher = source or ship.folder.name
    if ((pinned is not None and pinned != saved)
            or (publisher == origin and pinned != saved)
            or (publisher not in (origin, entry['origin']) and pinned != saved)):
        raise ShipError("forward route lacks the declaring ship's matching pin; retained")
    if adopt:
        transport.publish(path(runtime, raw.id), transport.encoded(saved))
