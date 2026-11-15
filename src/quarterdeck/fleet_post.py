"""Receiver-fetched immutable posts. Every writer stays in its own runtime.

Outbound offers are readiness markers, not deliveries. A receiver publishes its
own copy and fetched receipt; the sender observes both read-only. No transport
receipt stands in for the local door/watch's receipt.
"""

from __future__ import annotations

from dataclasses import replace
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import time

from . import trusted_files
from . import post_dir
from .fleet import peers
from .ship import ShipError


def component(value):
    return (isinstance(value, str) and bool(value) and not value.startswith('.')
            and value not in ('.', '..') and not any(c in value for c in '/\\@\x00\n\r')
            and value.isprintable() and value == value.strip())


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


def _hold_line(label, error):
    """Ordinary holds keep the exception type. A refused link also names its folder."""
    reason = str(error)
    if reason.startswith(post_dir.LINK_HELD):
        return f'{label} ({type(error).__name__}): {reason}'
    return f'{label} ({type(error).__name__})'


def stable_bytes(path):
    """One complete regular file, with unchanged descriptor and directory identity.

    A path under runtime/post is read by the shared directory walk and does not follow a link.
    """
    path = Path(path)
    if not post_dir.under_post(path):
        return _stable_following(path)
    try:
        return post_dir.read_bytes(path)
    except post_dir.NotRegular:
        raise post_dir.NotRegular('post transport source is not a regular file') from None
    except ShipError as error:
        if str(error).startswith(post_dir.CHANGED):
            raise ShipError('post transport source changed during its read') from None
        raise


def _stable_following(path):
    """The read outside runtime/post: the final name is not followed."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise post_dir.NotRegular('post transport source is not a regular file')
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


def _rename_new(temporary, path, source_fd=-100, destination_fd=-100):
    """Linux's atomic rename without replacement; never an exists/replace race."""
    library = ctypes.CDLL(None, use_errno=True)
    rename = getattr(library, 'renameat2', None)
    if rename is None:
        raise OSError(errno.ENOSYS, 'atomic no-clobber rename is unavailable')
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(source_fd, os.fsencode(temporary), destination_fd, os.fsencode(path), 1) != 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), str(path))


def publish(path, data):
    """Atomic no-clobber publication, without syncs. Equal bytes are the same intent.

    Under runtime/post, missing parents are real directories opened by the shared
    walk. A link on the way is a hold and is not followed or removed.
    """
    path = Path(path)
    if not post_dir.under_post(path):
        _publish_following(path, data)
        return
    name = path.name
    if name in ('', '.', '..') or any(c in name for c in '/\\\x00'):
        raise ShipError(f'invalid post path component {name!r}')
    parent = post_dir.open_directory(path.parent, create=True)
    try:
        _publish_at(parent, name, data, path)
    finally:
        os.close(parent)


def _publish_at(parent_fd, name, data, path):
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        info = None
    if info is not None and stat.S_ISLNK(info.st_mode):
        raise ShipError(f'{post_dir.LINK_HELD}{path}')
    if info is not None:
        if stable_bytes(path) != data:
            raise ShipError(f'post transport conflict at {name}; retained')
        return
    temporary = '.' + name + '.' + secrets.token_hex(8) + '.tmp'
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600,
                         dir_fd=parent_fd)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(descriptor, view):]
        try:
            _rename_new(temporary, name, parent_fd, parent_fd)
        except FileExistsError:
            if stable_bytes(path) != data:
                raise ShipError(f'post transport conflict at {name}; retained') from None
    finally:
        os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass


def _publish_following(path, data):
    missing, parent = [], path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for folder in reversed(missing):
        folder.mkdir(exist_ok=True)
    if path.exists():
        if stable_bytes(path) != data:
            raise ShipError(f'post transport conflict at {path.name}; retained')
        return
    temporary = path.with_name('.' + path.name + '.' + secrets.token_hex(8) + '.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(descriptor, view):]
        try:
            _rename_new(temporary, path)
        except FileExistsError:
            if stable_bytes(path) != data:
                raise ShipError(f'post transport conflict at {path.name}; retained') from None
    finally:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)


def encoded(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False) + '\n').encode('utf-8')


def prepare_outbound(runtime):
    """Establish the local transport index before its first pending post or hold."""
    directory = runtime / 'post' / 'outbound'
    if not directory.exists():
        directory.mkdir(parents=True, exist_ok=True)


def replace_status(path, text):
    """A disposable local status file, atomically replaced without a sync."""
    temporary = path.with_name('.' + path.name + '.' + secrets.token_hex(8) + '.tmp')
    try:
        temporary.write_text(text)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def import_path(runtime, identifier):
    return runtime / 'post' / 'imports' / (identifier + '.json')


def provenance(runtime, identifier):
    from . import post
    def read():
        if post.reading_missing(runtime / 'post' / 'imports', identifier + '.json'):
            return None
        try:
            return json.loads(stable_bytes(import_path(runtime, identifier)))
        except FileNotFoundError:
            return None
    return post.reading_value('import', (runtime.absolute(), identifier), read)


def local_view(runtime, message, *, data=None):
    """Present local parties as bare names without changing one stored byte."""
    here = owner(runtime)
    if here is None:
        return message
    imported = provenance(runtime, message.id)
    if imported and data is not None and imported['sha256'] != digest(data):
        raise ShipError('imported post differs from its immutable source; retained')
    origin = imported['origin'] if imported else here
    if imported is None and '@' not in message.sender and '@' not in message.to:
        from .forward_post import projected
        return projected(runtime, message)

    def view(value):
        seat, ship = address(value, origin)
        return seat if ship == here else f'{seat}@{ship}'

    from .forward_post import projected
    return projected(runtime, replace(message, sender=view(message.sender), to=view(message.to)))


def context_only(runtime, identifier):
    imported = provenance(runtime, identifier)
    if not imported or not imported['context']:
        from .forward_post import recorded
        route = recorded(runtime, identifier)
        if route is None or address(route['to'], owner(runtime))[1] != owner(runtime):
            return False
        imported = dict(sha256=route['sha256'])
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
    if seat not in destination.seats and seat not in getattr(destination, 'forward', {}) and seat not in (destination.operator.lower(), 'ship'):
        raise ShipError(f'no seat {seat!r} in ship {target}')
    return seat, target


def wire_parties(runtime, sender, to):
    here = owner(runtime)
    if here is None:
        if '@' in to or '@' in sender:
            raise ShipError('cross-ship post needs a declared ship')
        return sender, to
    if '@' not in sender and '@' not in to:
        return sender, to
    sending, origin = address(sender, here)
    if origin != here:
        raise ShipError('a ship posts only as its own sender')
    seat, target = address(to, here)
    if target == here:
        return sending, seat
    return f'{sending}@{here}', f'{seat}@{target}'


def _source(ship, identifier, *, directory=None):
    from . import post
    if not component(identifier):
        # Generated cross-ship ids contain @ in their sender; it is a filename, not a path.
        if not isinstance(identifier, str) or not identifier or identifier.startswith('.') or any(c in identifier for c in '/\\\x00\n\r'):
            raise ShipError('invalid post transport id')
    path = ship.runtime / 'post' / 'store' / (identifier + '.json')
    data = (stable_bytes(path) if directory is None else
            post_dir._read_nofollow(directory, path.name, path))
    message = post.Message.from_json(data.decode('utf-8'))
    if message.id != identifier:
        raise ShipError('post transport filename and id differ')
    return message, data


def _reference_bytes(destination, entry):
    """An origin reads its own ancestor; a reference never imports its words."""
    if destination is None or entry['origin'] != destination.folder.name:
        raise ShipError('ancestor reference does not name its destination origin')
    message, data = _source(destination, entry['id'])
    if (len(data) != entry['size'] or digest(data) != entry['sha256']
            or address(message.sender, entry['origin'])[1] != entry['origin']):
        raise ShipError('destination ancestor differs from its reference; descendant retained')
    return message, data


def _chain(ship, message, target):
    """Required ancestry, oldest first. Each entry names its actual byte source."""
    chain, seen = [], set()
    while True:
        if message.id in seen:
            raise ShipError('post transport ancestry has a cycle')
        seen.add(message.id)
        raw, data = _source(ship, message.id)
        imported = provenance(ship.runtime, message.id)
        _ownership(ship, message.id, imported['origin'] if imported else ship.folder.name,
                   raw, data, root=not chain)
        entry = dict(id=message.id, origin=imported['origin'] if imported else ship.folder.name,
                     size=len(data), sha256=digest(data))
        reference = bool(chain) and entry['origin'] == target
        if reference:
            _reference_bytes(peers(ship).get(target), entry)
            entry['reference'] = True
        _refusal_clear(ship, raw, entry['origin'], reference=reference)
        chain.append(entry)
        from .forward_post import recorded
        if saved := recorded(ship.runtime, message.id):
            chain[-1]['forward'] = saved
        if not raw.re:
            return list(reversed(chain))
        message, _ = _source(ship, raw.re)


def offer(ship, message, target):
    from .forward_post import pin
    pin(ship, message)
    chain = _chain(ship, message, target)
    version = 2 if any('forward' in entry for entry in chain) else 1
    value = dict(format=version, source=ship.folder.name, target=target, message=message.id, files=chain)
    path = ship.runtime / 'post' / 'outbound' / target / (message.id + '.json')
    publish(path, encoded(value))
    return value


def _refusal_clear(ship, message, origin, *, reference=False):
    """A refused ancestor holds its descendant; no refused words cross as context."""
    from . import refusal
    recipient, name = address(message.to, origin)
    destination = peers(ship).get(name)
    if destination is None:
        raise ShipError('ancestor refusal evidence unavailable; descendant retained')
    try:
        excluded = refusal.excluded(destination.runtime, recipient)
    except refusal.JournalError:
        raise ShipError('ancestor refusal evidence unreadable; descendant retained') from None
    if message.id in excluded and not reference:
        raise ShipError('refused ancestor; descendant retained without copying its words')


def _ownership(source, identifier, origin, message, data, *, root):
    if address(message.sender, origin)[1] != origin:
        raise ShipError('message author and claimed origin differ')
    imported = provenance(source.runtime, identifier)
    if imported:
        if imported.get('origin') != origin or imported.get('sha256') != digest(data):
            raise ShipError('foreign-origin posts cross only as verified imported context')
        if root:
            # A declared forward is the sole exception to the imported-root hold.
            # It keeps the author's origin and requires this ship's own exact pin.
            from .forward_post import recorded, receive
            saved = recorded(source.runtime, identifier)
            if not saved or address(saved['from'], origin)[1] != source.folder.name:
                raise ShipError('only the declaring ship forwards an imported root')
            receive(source.runtime, dict(origin=origin, forward=saved), message, data)
    elif origin != source.folder.name or address(message.sender, origin)[1] != source.folder.name:
        raise ShipError('a ship offers as origin only the posts it authored')


class _LocalReports:
    """One classification's original-byte chain proofs; never transport evidence.

    A no-re report has only its own address. Otherwise require an addressee's
    report on an order, then prove its entire ancestry local. Unknown, missing
    or cyclic history stays a candidate. The one no-follow directory descriptor
    and memoized reads last only this build; export's before/after directory key
    must still match before any excluded report can be skipped on another pass.
    """

    def __init__(self, ship):
        self.ship = ship
        self.raw = {}
        self.local = {}
        self.forwards = None
        self.directory = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            if self.directory is not None:
                os.close(self.directory)
        finally:
            self.directory = None
            self.raw.clear()
            self.local.clear()
            self.forwards = None

    def source(self, identifier):
        if identifier not in self.raw:
            if self.directory is None:
                self.directory = post_dir.open_directory(self.ship.runtime / 'post' / 'store', create=False)
            self.raw[identifier] = _source(self.ship, identifier, directory=self.directory)[0]
        return self.raw[identifier]

    def proves(self, message):
        try:
            if owner(self.ship.runtime) != self.ship.folder.name:
                return False
            original = self.source(message.id)
            if original.kind != 'report':
                return False
            if original.re:
                parent = self.source(original.re)
                if parent.kind != 'order' or original.sender != parent.to:
                    return False
            return self._chain_local(original.id)
        except (OSError, ValueError, KeyError, TypeError, ShipError):
            return False

    def _chain_local(self, identifier):
        from .forward_post import declared, recorded
        if self.forwards is None:
            self.forwards = set(getattr(self.ship, 'forward', {})) | set(declared(self.ship.runtime))
        current, trail, seen = identifier, [], set()
        while current not in self.local:
            if current in seen:
                result = False
                break
            seen.add(current)
            trail.append(current)
            message = self.source(current)
            if ('@' in message.sender or '@' in message.to
                    or message.sender in self.forwards or message.to in self.forwards
                    or provenance(self.ship.runtime, current) is not None
                    or recorded(self.ship.runtime, current) is not None
                    or post_dir.exists(self.ship.runtime / 'post' / 'addressed' / (current + '.json'))):
                result = False
                break
            address(message.sender, self.ship.folder.name)
            address(message.to, self.ship.folder.name)
            if not message.re:
                result = True
                break
            current = message.re
        else:
            result = self.local[current]
        self.local.update((value, result) for value in trail)
        return result


class ExportWorklist:
    """Only the local-message classification is reusable; transport evidence is not.

    Local reports prove their original-byte re-chain (or their own no-re
    address) in one read-only classification. Post, import and forwarding files
    are immutable, so their directory stamps cover that whole proof. Mutable declarations and compaction reservations
    have their own identities. A recent observation never ages into a certificate.
    """

    def __init__(self):
        self.certificate = None
        self.messages = ()

    def stamp(self, ship):
        result = []
        folders = [ship.runtime / 'post' / name
                   for name in ('', 'store', 'imports', 'forwarded', 'addressed')]
        files = [ship.folder / 'ship.toml']
        files += [trusted_files.path(ship.runtime, name, trusted_files.COMPACTION)
                  for name in getattr(ship, 'forward', {})]
        try:
            for path in folders + files:
                try:
                    if path in folders:
                        descriptor = post_dir.open_directory(path, create=False)
                        try:
                            info = os.fstat(descriptor)
                        finally:
                            os.close(descriptor)
                    else:
                        info = path.lstat()
                        if not stat.S_ISREG(info.st_mode):
                            return None
                except FileNotFoundError:
                    result.append(None)
                    continue
                result.append((info.st_dev, info.st_ino, info.st_mode, info.st_size,
                               info.st_mtime_ns, info.st_ctime_ns))
        except (OSError, ShipError):
            return None
        # Also cover declarations supplied in memory (including service test ships).
        declarations = (str(ship.folder), repr(getattr(ship, 'forward', {})),
                        repr(getattr(ship, 'groups', {})))
        return declarations, tuple(result)

    def retained(self, ship, message, imported, reports=None):
        from .forward_post import recorded
        if imported is not None:
            return True
        if message.kind == 'report':
            if reports is not None:
                return not reports.proves(message)
            with _LocalReports(ship) as proof:
                return not proof.proves(message)
        return ('@' in message.to or '@' in message.sender
                or message.to in getattr(ship, 'forward', {})
                or recorded(ship.runtime, message.id) is not None)


def export(ship, store, *, worklist=None):
    """Classify in one read-only view; publish offers outside it, on every pass.

    Only local classification is certified. An unproved report stays in the
    worklist; no receipt, offer, peer or transport observation is cached here.
    The retained list bounds offer reads on the classifying pass too.
    """
    from . import post
    started = time.time_ns()
    before = worklist.stamp(ship) if worklist is not None else None
    certificate = worklist.certificate if worklist is not None else None
    reuse = (before is not None and certificate is not None
             and certificate[0] == before and certificate[1] <= started)
    if worklist is not None:
        worklist.certificate = None  # any failure, including an exception, certifies nothing
    if worklist is None:
        retained = store.all()
    elif reuse:
        retained = list(worklist.messages)
    else:
        with post.reading_pass(store=store), _LocalReports(ship) as reports:
            messages = store.all()
            retained = []
            for message in messages:
                try:
                    keep = worklist.retained(ship, message, provenance(ship.runtime, message.id), reports)
                except (OSError, ValueError, KeyError, TypeError, ShipError):
                    keep = True  # uncertainty cannot remove work; ordinary export reads afresh below
                if keep:
                    retained.append(message)
    errors = []
    for message in retained:
        # Imported posts produce offers only through this ship's declared forward.
        try:
            imported = provenance(ship.runtime, message.id)
            if imported:
                raw, _ = _source(ship, message.id)
                seat, here = address(raw.to, imported['origin'])
                if here != ship.folder.name or seat not in getattr(ship, 'forward', {}):
                    continue
            for target in destinations(ship, store, message):
                offer(ship, message, target)
        except (OSError, ValueError, KeyError, ShipError) as error:
            errors.append(_hold_line(f'{post.short_of(message.id)}: outbound held', error))
    if worklist is not None and not errors and before is not None:
        after = worklist.stamp(ship)
        finished = time.time_ns()
        times = [max(s[-2:]) for s in before[1] if s is not None]
        if (before == after and started <= finished
                and (not times or max(times) + post.POST_TIMESTAMP_TICK_NS < started)):
            worklist.messages = tuple(retained)
            worklist.certificate = before, started
    return errors


def destinations(ship, store, message):
    from .forward_post import target as forwarded_target
    _, target = address(forwarded_target(ship, message, store), ship.folder.name)
    targets = {target} - {ship.folder.name}
    if target == ship.folder.name:
        from .forward_post import recorded
        if recorded(ship.runtime, message.id) or address(message.to, ship.folder.name)[1] != ship.folder.name:
            targets.add(target)  # a declared route can return to a different seat on its origin ship
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
    if (value.get('format') not in (1, 2) or value.get('source') != source.folder.name
            or value.get('target') != target or path.name != value.get('message', '') + '.json'
            or not isinstance(value.get('files'), list) or not value['files']
            or value['files'][-1]['id'] != value['message']):
        raise ShipError('post transport offer is incomplete')
    files = []
    for index, entry in enumerate(value['files']):
        if not component(entry['origin']):
            raise ShipError('post transport origin is invalid')
        root = index == len(value['files']) - 1
        if 'reference' in entry:
            if entry['reference'] is not True or root:
                raise ShipError('only an ancestor can be an origin reference')
            message, data = _reference_bytes(peers(source).get(target), entry)
        else:
            message, data = _source(source, entry['id'])
        _ownership(source, entry['id'], entry['origin'], message, data,
                   root=root)
        _refusal_clear(source, message, entry['origin'], reference=entry.get('reference', False))
        if len(data) != entry['size'] or digest(data) != entry['sha256']:
            raise ShipError('post transport offer no longer matches its source')
        if 'forward' in entry and value['format'] != 2:
            raise ShipError('forwarded posts need a version 2 offer')
        from .forward_post import receive
        receive(source.runtime, entry, message, data)
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
    routed = files[-1][0].get('forward', {}).get('to', message.to)
    recipient, destination = address(routed, files[-1][0]['origin'])
    direct = destination == ship.folder.name
    if direct and recipient not in ship.seats and recipient not in (ship.operator.lower(), post.SHIP):
        raise ShipError('post transport addressee is no longer declared')
    # One local import coordinator also serializes different offers sharing ancestors.
    with post.admission(ship.runtime, '@fleet-import'):
        for entry, raw, data in files:
            if entry.get('reference'):
                _reference_bytes(ship, entry)
                from .forward_post import receive
                receive(ship.runtime, entry, raw, data, adopt=True, source=source.folder.name)
                continue  # retain its verified route, never copy ancestor bytes or import metadata
            is_direct = raw.id == message.id and direct
            metadata = dict(format=1, origin=entry['origin'], sha256=entry['sha256'], context=True)
            from .forward_post import receive
            receive(ship.runtime, entry, raw, data, adopt=True, source=source.folder.name)
            local = ship.runtime / 'post' / 'store' / (raw.id + '.json')
            imported = provenance(ship.runtime, raw.id)
            if imported:
                if imported != metadata:
                    raise ShipError('post transport origin conflicts with existing import')
            elif post_dir.exists(local):
                # A returned ancestor belongs to this ship and needs no import metadata.
                if entry['origin'] != ship.folder.name or stable_bytes(local) != data:
                    raise ShipError('post transport conflicts with an existing local message')
            else:
                publish(import_path(ship.runtime, raw.id), encoded(metadata))
            with post.admission(ship.runtime, recipient if is_direct else '@fleet-context'):
                if is_direct and context_only(ship.runtime, raw.id):
                    # Import uses the same recipient-admission hooks as local
                    # publication before making new work eligible for the seat.
                    from . import compaction
                    viewed = local_view(ship.runtime, raw, data=data)
                    compaction.protect_closure(ship.runtime, viewed)
                    compaction.admitting(ship.runtime, viewed)
                publish(local, data)  # law: post-is-files -- same bytes and id in the receiving ship
                if is_direct:
                    publish(ship.runtime / 'post' / 'addressed' / (raw.id + '.json'),
                            encoded(dict(format=1, id=raw.id, sha256=entry['sha256'])))
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
                expected = dict(format=1, origin=entry['origin'], sha256=entry['sha256'], context=True)
                if metadata != expected:
                    return False
            if 'forward' in entry:
                from .forward_post import recorded
                if recorded(destination.runtime, entry['id']) != entry['forward']:
                    return False
        _, last = _source(destination, value['message'])
        from . import post
        root = post.Message.from_json(last.decode('utf-8'))
        routed = value['files'][-1].get('forward', {}).get('to', root.to)
        if address(routed, value['files'][-1]['origin'])[1] == destination.folder.name:
            if context_only(destination.runtime, root.id):
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError, ShipError):
        return False


def run(ship, store, *, worklist=None):
    """One service tick: offer local outbound, fetch peers, retain safe operational holds."""
    completed = False
    try:
        errors = _run(ship, store, worklist=worklist)
        completed = not errors
        return errors
    finally:
        if worklist is not None and not completed:
            worklist.certificate = None


def _run(ship, store, *, worklist=None):
    try:
        errors = export(ship, store) if worklist is None else export(ship, store, worklist=worklist)
    except (OSError, ShipError) as error:
        # The store listing is the no-follow walk. A linked store is a hold, not a failed tick.
        errors = [_hold_line('outbound held', error)]
    for name, source in peers(ship).items():
        directory = source.runtime / 'post' / 'outbound' / ship.folder.name
        try:
            for path in post_dir.json_paths(directory):
                try:
                    value, _ = _offer(source, ship.folder.name, path)
                    if source.folder == ship.folder and value['format'] != 2:
                        raise ShipError('only recorded forwarding routes have a local fetch')
                    if not fetched(source, ship, value):
                        fetch(ship, source, path)
                        store._cache = None
                except FileNotFoundError as error:
                    if not post_dir.gone(path):
                        errors.append(_hold_line(f'{name}: incoming post held', error))
                except (OSError, ValueError, KeyError, TypeError, ShipError) as error:
                    errors.append(_hold_line(f'{name}: incoming post held', error))
        except (OSError, ShipError) as error:
            errors.append(_hold_line(f'{name}: outbound unreadable', error))
    return sorted(set(errors))


def waiting(ship, store=None):
    """Read-only outbound state, including messages not offered yet by a down sender."""
    if not (ship.runtime / 'post' / 'outbound').exists():
        return []
    from . import post
    store = store or post.Store(ship.runtime)
    fleet, pending = peers(ship), {}
    for message in store.all():
        imported = provenance(ship.runtime, message.id)
        if imported:
            raw, _ = _source(ship, message.id)
            seat, here = address(raw.to, imported['origin'])
            if here != ship.folder.name or seat not in getattr(ship, 'forward', {}):
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
    if not (ship.runtime / 'post' / 'outbound').exists():
        return []
    try:
        lines = waiting(ship, store)
        try:
            lines += json.loads(stable_bytes(ship.runtime / 'post' / 'transport-holds.json'))
        except FileNotFoundError:
            pass
        return lines
    except (OSError, ValueError, KeyError, TypeError, ShipError):
        return ['cross-ship post state unreadable; messages retained']
