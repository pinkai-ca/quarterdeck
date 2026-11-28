"""Service-produced JSON status, bounded by the reading's age and owner."""

from dataclasses import fields
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
import time
import tomllib

from . import durable, lock_holders, output, servicectl

NAME = 'status-reading.json'
PUBLIC = 'status-public.json'
DEMAND = 'status-demand.json'
MAX_AGE_NS = 60_000_000_000  # two ordinary 30 s whole-reading periods
PUBLIC_MAX_AGE_NS = 180_000_000_000
DEMAND_NS = 300_000_000_000
MAX_BYTES = 32 * 1024 * 1024
MAX_POLICY_BYTES = 64 * 1024
ENVELOPE = {'format', 'wall', 'monotonic', 'pid', 'process_start', 'ship', 'framework', 'reading'}
PUBLIC_ENVELOPE = ENVELOPE | {'title_clock'}
PUBLIC_TOP = {'service', 'observed_at', 'open_orders', 'seats', 'programs', 'withheld_titles', 'title_coverage'}
PUBLIC_SEAT = {'name', 'activity', 'harness', 'model', 'pool', 'order'}
PUBLIC_PROGRAM = {'harness', 'working_now', 'last_turn_ended'}
ORDER_HASH = re.compile(r'[0-9a-f]{64}')
TOP = frozenset(('service', 'seats', 'programs', 'programs_error', 'check_outcomes', 'check_outcome_errors',
                 'open_orders', 'requests', 'facts', 'usage', 'pools', 'harnesses'))


def path(runtime, name=NAME):
    return Path(runtime) / 'status' / name


def _write(runtime, name, value):
    folder = path(runtime).parent
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    durable.replace_file(folder / name, json.dumps(value, allow_nan=False) + '\n')


def _bounded_bytes(file, limit, *, flags=0):
    descriptor = os.open(file, os.O_RDONLY | os.O_NONBLOCK | flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return None
        with os.fdopen(os.dup(descriptor), 'rb') as stream:
            raw = stream.read(limit + 1)
        return raw if len(raw) <= limit else None
    finally:
        os.close(descriptor)


def _unique_object(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError('duplicate status field')
    return value


def _read(runtime, name, limit=MAX_BYTES):
    raw = _bounded_bytes(path(runtime, name), limit, flags=os.O_NOFOLLOW)
    return None if raw is None else json.loads(raw, object_pairs_hook=_unique_object if name == PUBLIC else None)


def _fresh(data, bound):
    if not isinstance(data, dict) or type(data.get('wall')) is not int or type(data.get('monotonic')) is not int:
        return False
    age, elapsed = time.time_ns() - data['wall'], time.monotonic_ns() - data['monotonic']
    return 0 <= age < bound and 0 <= elapsed < bound and abs(age - elapsed) <= 50_000_000


def request(ship):
    """A JSON caller asks for five minutes of headless periodic readings.

    Inside a jail the stamp cannot reach the service and the served reading
    is absent. A caller of ordinary status can still read independently.
    """
    try:
        _write(ship.runtime, DEMAND, dict(wall=time.time_ns(), monotonic=time.monotonic_ns()))
    except OSError:
        return False
    return True


def requested(ship):
    runtime = getattr(ship, 'runtime', None)
    if runtime is None:
        return False
    try:
        return _fresh(_read(runtime, DEMAND, 4096), DEMAND_NS)
    except (OSError, ValueError, TypeError, UnicodeError):
        return False


def invalidate(runtime):
    """Invalidate the ordinary reading. The public reading expires on its own."""
    path(runtime).unlink(missing_ok=True)


def _complete(reading):
    from .watch import Facts, SeatReading
    if not isinstance(reading, dict) or not TOP <= reading.keys():
        return False
    required_facts = {field.name for field in fields(Facts)} - {'oldest_waiting_seat'}
    if not isinstance(reading['facts'], dict) or not required_facts <= reading['facts'].keys():
        return False
    if any(not isinstance(reading[name], list) for name in
           ('seats', 'programs', 'check_outcomes', 'check_outcome_errors', 'open_orders', 'requests', 'usage', 'pools', 'harnesses')):
        return False
    expected = {field.name for field in fields(SeatReading)}
    return all(isinstance(seat, dict) and expected <= seat.keys() for seat in reading['seats'])


def post_generation(runtime):
    """Bounded metadata for the immutable post and its address projections."""
    from .service_status import fingerprint

    post = runtime / 'post'
    try:
        parent = fingerprint(post)
        if parent is None or not stat.S_ISDIR(parent[2]):
            return None
        folders = [fingerprint(post / name) for name in ('store', 'imports', 'forwarded', 'addressed')]
        if folders[0] is None or any(entry is not None and not stat.S_ISDIR(entry[2]) for entry in folders):
            return None
        # Only the identity of the parent matters. Attempts and drops can make
        # new directories there without changing the message graph.
        return (parent[:3], *folders)
    except OSError:
        return None


def publish(ship, reading, *, wall, monotonic, framework, stamp, post_observation=None):
    from . import presentation
    value = output.value(reading)
    beat = servicectl.read_heartbeat(ship.runtime)
    start = servicectl.process_start(os.getpid())
    if (not _complete(value) or value['service'] != 'ok' or beat is None or beat[0] != os.getpid()
            or not servicectl.alive(ship.runtime) or start is None
            or presentation.fingerprint(ship.file) != stamp):
        invalidate(ship.runtime)
        return
    envelope = dict(format=1, wall=wall, monotonic=monotonic, pid=os.getpid(), process_start=start,
                    ship=stamp, framework=framework)
    _write(ship.runtime, NAME, dict(envelope, reading=value))
    try:
        titles, coverage, clock = _title_proof(ship, value, post_observation, envelope)
        public = _projection(value, wall, titles, coverage)
        publication = dict(envelope, reading=public, title_clock=clock)
        if value['facts']['service'] == 'ok' and _public_complete(publication):
            _write(ship.runtime, PUBLIC, publication)
    except (OSError, KeyError, TypeError):
        pass  # an unavailable companion does not stop ordinary status


def _title_proof(ship, value, observation, envelope):
    unavailable = (None, None, None)
    if observation is None or value['facts']['post_held']:
        return unavailable
    messages, before, after = observation
    if before is None or after is None or any(
            old is not None and (new is None or old[:3] != new[:3]) for old, new in zip(before, after)):
        return unavailable
    if (lists := _title_lists(ship, messages)) is None:
        return unavailable
    if before == after:
        titles, hashes = lists
        clock = {key: envelope[key] for key in ('wall', 'monotonic')}
        return titles, dict(observed_at=_observed_at(clock['wall']), order_hashes=hashes), clock
    # A directory can gain mail during a whole reading. Reuse only a proved
    # pair from this service/declaration/framework and keep its original time.
    try:
        previous = _served_document(ship, PUBLIC, stamp=envelope['ship'])
        if (previous is not None and previous['framework'] == envelope['framework']
                and _public_complete(previous) and _fresh(previous['title_clock'], PUBLIC_MAX_AGE_NS)):
            reading = previous['reading']
            return reading['withheld_titles'], reading['title_coverage'], previous['title_clock']
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        pass
    return unavailable


def _observed_at(wall):
    return datetime.fromtimestamp(wall / 1_000_000_000, timezone.utc).isoformat()


def labelled(reading, mode, wall):
    at = _observed_at(wall)
    label = f'served: service reading at {at}' if mode == 'served' else f'cold: independent reading at {at}'
    return {**output.value(reading), 'reading': dict(mode=mode, at=at, label=label)}


def _served_document(ship, name, *, stamp=None):
    from . import presentation, stop_barrier

    if stop_barrier.status(ship.runtime):
        return None
    data = _read(ship.runtime, name)
    bound = PUBLIC_MAX_AGE_NS if name == PUBLIC else MAX_AGE_NS
    if (not isinstance(data, dict) or type(data.get('format')) is not int or data['format'] != 1
            or not _fresh(data, bound) or (stamp is not None and data.get('ship') != stamp)):
        return None
    beat = servicectl.read_heartbeat(ship.runtime)
    if (beat is None or type(data.get('pid')) is not int or data['pid'] <= 0 or beat[0] != data['pid']
            or not 0 <= beat[1] <= servicectl.STALE_SECONDS or not servicectl.alive(ship.runtime)
            or lock_holders.reading(servicectl.lock_path(ship.runtime)) != (True, data['pid'])
            or data.get('process_start') is None or servicectl.process_start(data['pid']) != data['process_start']
            or data.get('ship') != presentation.fingerprint(ship.file)
            or data.get('framework') != presentation.framework_stamp()):
        return None
    return data


def served(ship):
    try:
        data = _served_document(ship, NAME)
        if (data is not None and _complete(reading := data.get('reading')) and reading['service'] == 'ok'
                and reading['facts']['service'] == 'ok' and _fresh(data, MAX_AGE_NS)):
            return labelled(reading, 'served', data['wall'])
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, OverflowError):
        return None


def _projection(full, wall, titles, coverage):
    seats = [dict(name=row['name'], activity=row['activity']['state'], harness=row['harness'],
                  model=row['model'], pool=row['pool'],
                  order=row['order'] if _order_origin(row['order']) is not None else None)
             for row in full['seats']]
    history_available = full['programs_error'] is None
    programs = [dict(harness=row['harness'], working_now=row['working_now'],
                     last_turn_ended=row['last_turn_ended'] if history_available else None)
                for row in full['programs']]
    return dict(service=full['service'], observed_at=_observed_at(wall), open_orders=len(full['open_orders']),
                seats=seats, programs=programs, withheld_titles=titles, title_coverage=coverage)


def _order_origin(order):
    from .fleet_post import component
    from .post import id_sender

    if not isinstance(order, str) or not order.startswith('ord-') or not order[4:17].isascii():
        return None
    sender = id_sender(order)
    if sender is None:
        return None
    seat, at, ship = sender.casefold().partition('@')
    if component(seat) and (not at or component(ship)):
        return seat, ship


def _order_hash(order):
    return sha256(order.encode('utf-8')).hexdigest()


def _title_lists(ship, messages):
    """Find covered orders and those involving local Security in the same graph."""
    from .fleet_post import address
    from .ship import ShipError

    here = ship.folder.name.casefold()
    security = {seat.name.casefold() for seat in ship.seats.values() if seat.role == 'security'}

    def security_party(value):
        seat, folder = address(value, here)
        return folder.casefold() == here and seat.casefold() in security

    by_id = {message.id: message for message in messages}
    if len(by_id) != len(messages):
        return None
    # Memoize the root of every branch. Missing ancestry and cycles cannot
    # support a complete answer, even if the affected messages are private.
    roots = {}
    for identifier in by_id:
        trail = []
        while identifier not in roots:
            message = by_id.get(identifier)
            if message is None:
                return None
            roots[identifier] = None
            trail.append(identifier)
            if message.re is None:
                roots[identifier] = identifier
                break
            if not isinstance(message.re, str):
                return None
            identifier = message.re
        root = roots[identifier]
        if root is None:
            return None
        roots.update((member, root) for member in trail)

    try:
        blocked = {roots[message.id] for message in messages
                   if security_party(message.sender) or security_party(message.to)}
    except (AttributeError, ShipError):
        return None
    orders = [message.id for message in messages if message.kind == 'order']
    if not all(_order_origin(order) is not None for order in orders):
        return None
    return (sorted(order for order in orders if roots[order] in blocked),
            sorted({_order_hash(order) for order in orders}))


def _public_titles(titles, coverage, clock, snapshot):
    if titles is None or coverage is None or clock is None:
        return titles is coverage is clock is None
    if (not isinstance(titles, list) or not all(_order_origin(order) is not None for order in titles)
            or titles != sorted(set(titles))
            or not isinstance(clock, dict) or clock.keys() != {'wall', 'monotonic'}
            or any(type(clock[key]) is not int or clock[key] > snapshot[key] for key in clock)
            or not isinstance(coverage, dict) or coverage.keys() != {'observed_at', 'order_hashes'}
            or coverage['observed_at'] != _observed_at(clock['wall'])):
        return False
    hashes = coverage['order_hashes']
    return (isinstance(hashes, list) and all(isinstance(value, str) and ORDER_HASH.fullmatch(value) for value in hashes)
            and hashes == sorted(set(hashes)) and {_order_hash(order) for order in titles} <= set(hashes))


def _public_seats(rows):
    from .fleet_post import component
    from .harness import NAMES
    from .harness.types import AUTHENTICATION

    states = {'unknown', 'off', 'idle', 'working', 'needs a human', 'stalled', 'failed', 'refused',
              'screening', AUTHENTICATION}
    names = set()
    for row in rows:
        if (not isinstance(row, dict) or row.keys() != PUBLIC_SEAT or not component(row['name'])
                or not isinstance(row['activity'], str) or row['activity'] not in states
                or row['harness'] not in (*NAMES, 'unknown')
                or not isinstance(row['pool'], str) or not row['pool'].isprintable()
                or (row['model'] is not None
                    and (not isinstance(row['model'], str) or not row['model'].isprintable()))
                or (row['order'] is not None and _order_origin(row['order']) is None)):
            return False
        names.add(row['name'].casefold())
    return len(names) == len(rows)


def _public_programs(rows):
    from .harness import NAMES
    from .program_use import instant

    names = set()
    for row in rows:
        if (not isinstance(row, dict) or row.keys() != PUBLIC_PROGRAM or row['harness'] not in NAMES
                or (row['working_now'] is not None and type(row['working_now']) is not bool)
                or (row['last_turn_ended'] is not None and instant(row['last_turn_ended']) is None)):
            return False
        names.add(row['harness'])
    return len(names) == len(rows)


def _public_complete(data):
    return (data.keys() == PUBLIC_ENVELOPE and isinstance(reading := data['reading'], dict)
            and reading.keys() == PUBLIC_TOP and reading['service'] == 'ok'
            and reading['observed_at'] == _observed_at(data['wall'])
            and type(reading['open_orders']) is int and reading['open_orders'] >= 0
            and isinstance(reading['seats'], list) and _public_seats(reading['seats'])
            and isinstance(reading['programs'], list) and _public_programs(reading['programs'])
            and _public_titles(reading['withheld_titles'], reading['title_coverage'], data['title_clock'], data))


def _policy(file):
    from .fleet_post import component

    if not file.is_absolute() or (raw := _bounded_bytes(file, MAX_POLICY_BYTES)) is None:
        raise ValueError('policy unavailable')
    policy = tomllib.loads(raw.decode('utf-8'))
    if policy.keys() != {'office_seats', 'private_ships'}:
        raise ValueError('invalid post privacy policy')
    for key, entries in policy.items():
        if not isinstance(entries, list) or not all(component(entry) for entry in entries):
            raise ValueError('invalid privacy names')
        normalized = {entry.casefold() for entry in entries}
        if len(normalized) != len(entries):
            raise ValueError('duplicate privacy names')
        policy[key] = normalized
    return policy


def public_served(folder, policy_file):
    """Read only the minimum publication, with a policy supplied by the dashboard."""
    from . import presentation, ship as shipmod

    try:
        policy = _policy(policy_file)
        # Bind the name loaded for privacy to the same declaration as the service reading.
        stamp = presentation.fingerprint(folder / 'ship.toml')
        ship = shipmod.load(folder)
        if not isinstance(ship.name, str) or not request(ship):
            return None
        data = _served_document(ship, PUBLIC, stamp=stamp)
        if data is None or not _public_complete(data):
            return None
        reading = data['reading']
        if {row['name'] for row in reading['seats']} != ship.seats.keys():
            return None
        office, private = policy['office_seats'], policy['private_ships']
        here = ship.folder.name.casefold()
        private_ship = bool({here, ship.name.casefold()} & private)

        def public_order(order):
            sender, origin = _order_origin(order)
            return sender not in office and origin not in private and not (private_ship and origin in ('', here))

        for row in reading['seats']:
            if private_ship or row['name'].casefold() in office:
                del row['order']
            elif row['order'] is not None and not public_order(row['order']):
                row['order'] = None
        if (titles := reading['withheld_titles']) is not None:
            if _fresh(data['title_clock'], PUBLIC_MAX_AGE_NS):
                reading['withheld_titles'] = [order for order in titles if public_order(order)]
            else:
                reading['withheld_titles'] = reading['title_coverage'] = None
        return reading if _fresh(data, PUBLIC_MAX_AGE_NS) else None
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError, shipmod.ShipError):
        return None
