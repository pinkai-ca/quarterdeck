"""Receiver-fetched immutable posts. Every writer stays in its own runtime.

Outbound offers are readiness markers, not deliveries. A receiver publishes its
own copy and fetched receipt; the sender observes both read-only. No transport
receipt stands in for the local door/watch's receipt.
"""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat

from . import durable, ship as shipmod
from .fleet import peers
from .ship import ShipError


def component(value):
    return (isinstance(value, str) and bool(value) and not value.startswith('.')
            and value not in ('.', '..') and not any(c in value for c in '/\\@\x00\n\r')
            and value == value.strip())


def address(value, local):
    """A bare name is local; a qualified address always names a ship folder."""
    parts = value.split('@')
    if len(parts) == 1 and component(parts[0]):
        return parts[0], local
    if len(parts) == 2 and all(component(p) for p in parts):
        return tuple(parts)
    raise ShipError(f'invalid post address {value!r}; use seat or seat@ship')


def owner(runtime):
    folder = Path(runtime).parent
    return folder.name if (folder / 'ship.toml').is_file() else None


def stable_bytes(path):
    """One complete regular file, with unchanged descriptor and directory identity."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ShipError('post transport source is not a regular file')
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            data = stream.read()
        after, current = os.fstat(descriptor), path.lstat()
        stamp = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if stamp(before) != stamp(after) or stamp(after) != stamp(current) or len(data) != after.st_size:
            raise ShipError('post transport source changed during its read')
        return data
    finally:
        os.close(descriptor)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def publish(path, data):
    """Durable no-clobber publication. An equal existing file is the same intent."""
    missing, parent = [], path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for folder in reversed(missing):
        folder.mkdir(exist_ok=True)
        durable.sync_folder(folder.parent)
    if path.exists():
        if stable_bytes(path) != data:
            raise ShipError(f'post transport conflict at {path.name}; retained')
        return
    temporary = path.with_name('.' + path.name + '.' + secrets.token_hex(8) + '.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(descriptor, 'wb', closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(descriptor)
        try:
            os.link(temporary, path)
        except FileExistsError:
            if stable_bytes(path) != data:
                raise ShipError(f'post transport conflict at {path.name}; retained') from None
        durable.sync_folder(path.parent)
    finally:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)


def encoded(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False) + '\n').encode('utf-8')


def import_path(runtime, identifier):
    return runtime / 'post' / 'imports' / (identifier + '.json')


def provenance(runtime, identifier):
    try:
        return json.loads(stable_bytes(import_path(runtime, identifier)))
    except FileNotFoundError:
        return None


def local_view(runtime, message, *, data=None):
    """Present local parties as bare names without changing one stored byte."""
    here = owner(runtime)
    if here is None:
        return message
    imported = provenance(runtime, message.id)
    if imported and data is not None and imported['sha256'] != digest(data):
        raise ShipError('imported post differs from its immutable source; retained')
    origin = imported['origin'] if imported else here

    def view(value):
        seat, ship = address(value, origin)
        return seat if ship == here else f'{seat}@{ship}'

    return replace(message, sender=view(message.sender), to=view(message.to))


def context_only(runtime, identifier):
    imported = provenance(runtime, identifier)
    if not imported or not imported['context']:
        return False
    try:
        value = json.loads(stable_bytes(runtime / 'post' / 'addressed' / (identifier + '.json')))
    except FileNotFoundError:
        return True
    if value != dict(format=1, id=identifier, sha256=imported['sha256']):
        raise ShipError('addressed import marker conflicts with its source; retained')
    return False


def validate_target(ship, value):
    seat, target = address(value, ship.folder.name)
    fleet = peers(ship)
    if target not in fleet:
        raise ShipError(f'no ship {target!r} in this project')
    destination = fleet[target]
    if seat not in destination.seats and seat not in (destination.operator.lower(), 'ship'):
        raise ShipError(f'no seat {seat!r} in ship {target}')
    return seat, target


def wire_parties(runtime, sender, to):
    here = owner(runtime)
    if here is None:
        if '@' in to or '@' in sender:
            raise ShipError('cross-ship post needs a declared ship')
        return sender, to
    sending, origin = address(sender, here)
    if origin != here:
        raise ShipError('a ship posts only as its own sender')
    seat, target = address(to, here)
    if target == here:
        return sending, seat
    return f'{sending}@{here}', f'{seat}@{target}'


def _source(ship, identifier):
    from . import post
    if not component(identifier):
        # Generated cross-ship ids contain @ in their sender; it is a filename, not a path.
        if not isinstance(identifier, str) or not identifier or identifier.startswith('.') or any(c in identifier for c in '/\\\x00\n\r'):
            raise ShipError('invalid post transport id')
    data = stable_bytes(ship.runtime / 'post' / 'store' / (identifier + '.json'))
    message = post.Message.from_json(data.decode('utf-8'))
    if message.id != identifier:
        raise ShipError('post transport filename and id differ')
    return message, data


def _chain(ship, message):
    """Required ancestry, oldest first. Each entry names its actual byte source."""
    chain, seen = [], set()
    while True:
        if message.id in seen:
            raise ShipError('post transport ancestry has a cycle')
        seen.add(message.id)
        raw, data = _source(ship, message.id)
        imported = provenance(ship.runtime, message.id)
        chain.append(dict(id=message.id, origin=imported['origin'] if imported else ship.folder.name,
                          size=len(data), sha256=digest(data)))
        if not raw.re:
            return list(reversed(chain))
        message, _ = _source(ship, raw.re)


def offer(ship, message, target):
    chain = _chain(ship, message)
    value = dict(format=1, source=ship.folder.name, target=target, message=message.id, files=chain)
    path = ship.runtime / 'post' / 'outbound' / target / (message.id + '.json')
    publish(path, encoded(value))
    return value


def export(ship, store):
    """Write offers only beside this ship's own source files."""
    from . import post
    errors = []
    for message in store.all():
        # Only locally authored posts produce offers; imported copies do not bounce.
        if provenance(ship.runtime, message.id):
            continue
        try:
            for target in destinations(ship, store, message):
                offer(ship, message, target)
        except (OSError, ValueError, KeyError, ShipError) as error:
            errors.append(f'{post.short_of(message.id)}: outbound held ({type(error).__name__})')
    return errors


def destinations(ship, store, message):
    _, target = address(message.to, ship.folder.name)
    targets = {target} - {ship.folder.name}
    # A report closes its addressee's order even when addressed to the silent ship sink.
    if message.kind == 'report' and message.re:
        order = store.get(message.re)
        if order.kind == 'order' and message.sender == order.to:
            _, origin = address(order.sender, ship.folder.name)
            targets |= {origin} - {ship.folder.name}
    return targets


def receipt_path(ship, source, identifier):
    return ship.runtime / 'post' / 'fetched' / source / (identifier + '.json')


def _offer(source, target, path):
    value = json.loads(stable_bytes(path))
    if (value.get('format') != 1 or value.get('source') != source.folder.name
            or value.get('target') != target or path.name != value.get('message', '') + '.json'
            or not isinstance(value.get('files'), list) or not value['files']
            or value['files'][-1]['id'] != value['message']):
        raise ShipError('post transport offer is incomplete')
    files = []
    for entry in value['files']:
        if not component(entry['origin']):
            raise ShipError('post transport origin is invalid')
        message, data = _source(source, entry['id'])
        if len(data) != entry['size'] or digest(data) != entry['sha256']:
            raise ShipError('post transport offer no longer matches its source')
        files.append((entry, message, data))
    for previous, current in zip(files, files[1:]):
        if current[1].re != previous[1].id:
            raise ShipError('post transport ancestry is incomplete')
    if files[0][1].re is not None:
        raise ShipError('post transport ancestry is incomplete')
    return value, files


def fetch(ship, source, path):
    """Read a peer; publish and acknowledge only in the receiver's own runtime."""
    from . import post
    value, files = _offer(source, ship.folder.name, path)
    message = files[-1][1]
    recipient, destination = address(message.to, files[-1][0]['origin'])
    direct = destination == ship.folder.name
    if direct and recipient not in ship.seats and recipient not in (ship.operator.lower(), post.SHIP):
        raise ShipError('post transport addressee is no longer declared')
    # One local import coordinator also serializes different offers sharing ancestors.
    with post.admission(ship.runtime, '@fleet-import'):
        for entry, raw, data in files:
            is_direct = raw.id == message.id and direct
            metadata = dict(format=1, origin=entry['origin'], sha256=entry['sha256'], context=True)
            local = ship.runtime / 'post' / 'store' / (raw.id + '.json')
            imported = provenance(ship.runtime, raw.id)
            if imported:
                if imported != metadata:
                    raise ShipError('post transport origin conflicts with existing import')
            elif local.exists():
                # A returned ancestor belongs to this ship and needs no import metadata.
                if entry['origin'] != ship.folder.name or stable_bytes(local) != data:
                    raise ShipError('post transport conflicts with an existing local message')
            else:
                publish(import_path(ship.runtime, raw.id), encoded(metadata))
            with post.admission(ship.runtime, recipient if is_direct else '@fleet-context'):
                publish(local, data)  # law: post-is-files -- same bytes and id in the receiving ship
                if is_direct:
                    publish(ship.runtime / 'post' / 'addressed' / (raw.id + '.json'),
                            encoded(dict(format=1, id=raw.id, sha256=entry['sha256'])))
                # Recover publication cut before its directory sync, before acknowledging it.
                durable.sync_folder(local.parent)
        publish(receipt_path(ship, source.folder.name, message.id), encoded(value))
    return message.id


def fetched(source, destination, value):
    """A remote receipt is evidence only alongside every matching destination byte."""
    try:
        saved = json.loads(stable_bytes(receipt_path(destination, source.folder.name, value['message'])))
        if saved != value:
            return False
        for entry in value['files']:
            _, data = _source(destination, entry['id'])
            if len(data) != entry['size'] or digest(data) != entry['sha256']:
                return False
            if entry['origin'] != destination.folder.name:
                metadata = provenance(destination.runtime, entry['id'])
                if metadata != dict(format=1, origin=entry['origin'], sha256=entry['sha256'], context=True):
                    return False
        _, last = _source(destination, value['message'])
        from . import post
        root = post.Message.from_json(last.decode('utf-8'))
        if address(root.to, value['files'][-1]['origin'])[1] == destination.folder.name:
            if context_only(destination.runtime, root.id):
                return False
        return True
    except (OSError, ValueError, KeyError, ShipError):
        return False


def run(ship, store):
    """One service tick: offer local outbound, fetch peers, retain safe operational holds."""
    errors = export(ship, store)
    for name, source in peers(ship).items():
        if source.folder == ship.folder:
            continue
        directory = source.runtime / 'post' / 'outbound' / ship.folder.name
        try:
            for path in sorted(directory.glob('*.json')):
                try:
                    value, _ = _offer(source, ship.folder.name, path)
                    if not fetched(source, ship, value):
                        fetch(ship, source, path)
                        store._cache = None
                except (OSError, ValueError, KeyError, TypeError, ShipError) as error:
                    errors.append(f'{name}: incoming post held ({type(error).__name__})')
        except OSError as error:
            errors.append(f'{name}: outbound unreadable ({type(error).__name__})')
    return sorted(set(errors))


def waiting(ship, store=None):
    """Read-only outbound state, including messages not offered yet by a down sender."""
    from . import post
    store = store or post.Store(ship.runtime)
    fleet, pending = peers(ship), {}
    for message in store.all():
        if provenance(ship.runtime, message.id):
            continue
        for target in destinations(ship, store, message):
            try:
                path = ship.runtime / 'post' / 'outbound' / target / (message.id + '.json')
                value, _ = _offer(ship, target, path)
                done = target in fleet and fetched(ship, fleet[target], value)
            except (OSError, ValueError, KeyError, TypeError, ShipError):
                done = False
            if not done:
                pending.setdefault(target, []).append(message)
    return [f'post to {name}: {len(messages)} waiting to be fetched, oldest '
            f'{max(0, int(max((post.now() - m.at).total_seconds() for m in messages) // 60))}m'
            for name, messages in sorted(pending.items())]


def status(ship, store=None):
    try:
        lines = waiting(ship, store)
        try:
            lines += json.loads(stable_bytes(ship.runtime / 'post' / 'transport-holds.json'))
        except FileNotFoundError:
            pass
        return lines
    except (OSError, ValueError, KeyError, TypeError, ShipError):
        return ['cross-ship post state unreadable; messages retained']
