"""Finished delivery evidence, moved unchanged out of the service's working folders.

Retired attempts remain in Attempts.all through immutable batch metadata. Original
files are retained verbatim; named readers resolve either location. A batch index
is published before its first rename, so process interruption preserves history.
Power-loss persistence is left to the operating system; retirement forces no sync.
"""

from __future__ import annotations

from dataclasses import asdict
import json
import hashlib
from pathlib import Path
import secrets
import time

from . import post_dir
from .ship import ShipError

# These are finished outcomes, not a pending input or an unresolved receipt.
FINISHED = frozenset({'delivered', 'watch', 'launch', 'withheld', 'cancelled', 'abandoned',
                      'not-in-closed-record', 'unacted'})
SETTLED = frozenset({'delivered', 'watch'})
JOURNALS = frozenset({'delivered', 'cancelled', 'abandoned', 'unacted'})
BATCH = 1000
SECONDS = 1.0
HEARTBEAT = 0.1
INTERVAL = 60.0


def retired(path: Path, runtime: Path) -> Path:
    """The retained location of one original attempt or pending journal citation."""
    relative = path.relative_to(runtime / 'post')
    if (relative.parts[0] not in ('attempts', 'pending') or '..' in relative.parts
            or len(relative.parts) != (2 if relative.parts[0] == 'attempts' else 3)):
        raise ShipError('not a delivery evidence path')
    return runtime / 'post' / 'retired' / relative


def resolve(path: Path, runtime: Path) -> Path:
    """Prefer an original still in place; do not enumerate historical folders."""
    return path if path.exists() else retired(path, runtime)


def row(attempt):
    return {key: value for key, value in asdict(attempt).items() if key != 'path'}


def _folders(paths):
    """Create the batch's archive layout without forcing it to storage."""
    for path in paths:
        post_dir.ensure_directory(path)


def _replace_file(path: Path, text: str):
    """Publish a whole index or completion record, without a storage sync."""
    post_dir.replace_bytes(path, text.encode('utf-8'))


def move(source: Path, destination: Path) -> bool:
    """Rename one whole file on the same filesystem, without a storage sync."""
    try:
        if not post_dir.exists(source):
            return False
    except FileNotFoundError:
        return False
    try:
        if post_dir.exists(destination):
            raise ShipError('retired delivery destination already exists; both files retained')
    except FileNotFoundError:
        pass
    try:
        post_dir.rename(source, destination)
    except ShipError as error:
        if 'already exists' in str(error):
            raise ShipError('retired delivery destination already exists; both files retained') from error
        raise
    return True


def _files(runtime, data):
    seat = data['seat']
    for name, digest in data['journals'].items():
        yield runtime / 'post' / 'pending' / seat / name, digest
    for name in data['attempts']:
        yield runtime / 'post' / 'attempts' / name, None


def _resume(service, path, data, progress, counts):
    """Replay a prepublished batch; a done marker follows its whole-file moves."""
    runtime = service.runtime
    for source, digest in _files(runtime, data):
        progress()
        destination = retired(source, runtime)
        if post_dir.exists(source):
            if digest is not None:
                if hashlib.sha256(post_dir.read_bytes(source)).hexdigest() != digest:
                    raise ShipError('retirement journal changed; original retained')
            else:
                if json.loads(post_dir.read_text(source)) != data['attempts'][source.name]:
                    raise ShipError('retirement attempt changed; original retained')
            if move(source, destination):
                counts['journals' if digest is not None else 'attempts'] += 1
        elif not post_dir.exists(destination):
            raise ShipError('retirement evidence missing; batch remains unfinished')
    done = runtime / 'post' / 'retired' / 'completed' / path.name
    if not post_dir.exists(done):
        _replace_file(done, json.dumps(dict(format=1, batch=path.name)) + '\n')
    return counts


def _recover(service, progress, counts):
    """Finish one published intent before authorizing another batch."""
    from . import lifecycle, post
    root = service.runtime / 'post' / 'retired'
    for path in post_dir.json_paths(root / 'index'):
        progress()
        if post_dir.exists(root / 'completed' / path.name):
            continue
        data = json.loads(post_dir.read_text(path))
        if 'seat' not in data:
            continue  # older immutable metadata without a batch intent
        name = data['seat']
        if Path(name).name != name or name in ('.', '..'):
            raise ShipError('retirement batch seat invalid; originals retained')
        try:
            with lifecycle.claim(service.runtime, name), post.admission(service.runtime, name), service.attempts._locked():
                messages = _settled(service.store, service.attempts.all(progress=progress), progress)
                if any(message not in messages for message in data['messages']):
                    return {**counts, 'held': 'batch delivery unsettled'}
                return _resume(service, path, data, progress, counts)
        except lifecycle.Busy:
            continue
    return None


def _batch(service, name, mine, proofs, progress, counts, known):
    runtime = service.runtime
    root = runtime / 'post' / 'retired'
    progress()
    _folders([root / 'index', root / 'completed', root / 'attempts', root / 'pending' / name])
    message_ids = {a.message for a in mine.values()}
    message_ids.update(known[n].message for p in proofs for n in p.data['attempts'])
    data = dict(format=1, seat=name, messages=sorted(message_ids),
                attempts={n: row(a) for n, a in mine.items()},
                journals={p.path.name: hashlib.sha256(post_dir.read_bytes(p.path)).hexdigest() for p in proofs})
    path = root / 'index' / (secrets.token_hex(16) + '.json')
    # Exactly one immutable index publication per pass, before its first rename.
    _replace_file(path, json.dumps(data, sort_keys=True) + '\n')
    return _resume(service, path, data, progress, counts)


class SliceEnded(Exception):
    """Ordinary progress: the next scheduled pass resumes from intact files."""


def _settled(store, records, progress):
    latest = {}
    for index, attempt in enumerate(records):
        if index % 200 == 0:
            progress()
        latest[attempt.message] = attempt.result
    # One immutable post snapshot. Repeated Store.closing calls would re-read
    # every message while a newly written directory is inside its timestamp bucket.
    messages = store.all()
    by_id = {m.id: m for m in messages}
    closed = set()
    for index, message in enumerate(messages):
        if index % 200 == 0:
            progress()
        parent = by_id.get(message.re)
        if parent is not None and (parent.kind == 'question' and message.kind == 'answer'
                or parent.kind == 'order' and message.kind == 'report' and message.sender == parent.to):
            closed.add(parent.id)
    return {m.id for m in messages if latest.get(m.id) in SETTLED
            and (not m.obligation or m.id in closed)}


def run(service, *, limit=BATCH, seconds=SECONDS):
    """Service duty, after accounting: catch-up and current owners always go first.

    Lifecycle -> recipient admission -> attempts is the same order as delivery.
    All attempts for a message must be finished. A terminal journal only moves
    after all its attempts are eligible, and moves before those attempt files.
    """
    counts = dict(attempts=0, journals=0)
    start = time.monotonic()
    last_beat = start - HEARTBEAT
    def progress():
        nonlocal last_beat
        now = time.monotonic()
        if now - last_beat >= HEARTBEAT:
            beat = getattr(service, 'heartbeat', None)
            if beat is not None:
                beat()
            last_beat = now
        if now - start >= seconds:
            raise SliceEnded()
    try:
        progress()
        return _run(service, limit, counts, progress)
    except SliceEnded:
        return {**counts, 'paused': True}


def _run(service, limit, counts, progress):
    from . import lifecycle, pending, post, receipt_refresh
    if service.ledger.catching_up():
        return {**counts, 'held': 'ledger catch-up'}
    recovered = _recover(service, progress, counts)
    if recovered is not None:
        return recovered
    records = service.attempts.all(progress=progress)
    settled = _settled(service.store, records, progress)
    grouped = {}
    for attempt in records:
        grouped.setdefault(attempt.message, []).append(attempt)
    eligible = {a.path.name for group in grouped.values()
                if group[0].message in settled and all(a.result in FINISHED for a in group)
                for a in group}
    names = sorted({a.seat for a in records if a.path.parent == service.attempts.dir
                    and a.path.name in eligible}
                   | set(post_dir.directory_names(service.runtime / 'post' / 'pending')))
    cursor = getattr(service, 'post_retirement_cursor', None)
    if cursor in names:
        position = names.index(cursor)
        names = names[position:] + names[:position]
    for position, name in enumerate(names):
        progress()
        if counts['attempts'] >= limit:
            break
        try:
            with lifecycle.claim(service.runtime, name), post.admission(service.runtime, name):
                # Reload beneath admission; message closure and a delivery can no
                # longer change between the eligibility read and the rename.
                with service.attempts._locked():
                    if service.ledger.catching_up():
                        return {**counts, 'held': 'ledger catch-up'}
                    current = service.attempts.all(progress=progress)
                    launch_path = service.runtime / 'seats' / name / 'launch.json'
                    try:
                        launch = json.loads(launch_path.read_text())
                        live_join = (launch['pane'], launch['pid'], launch['at'])
                    except FileNotFoundError:
                        live_join = None
                    except (OSError, ValueError, KeyError, TypeError):
                        continue  # current ownership is unknown, so preserve this seat's files
                    def old(a):
                        return live_join is None or (a.pane, a.pid, a.launched_at) != live_join

                    settled = _settled(service.store, current, progress)
                    all_names = {a.path.name: a for a in current}
                    groups = {}
                    for a in current:
                        groups.setdefault(a.message, []).append(a)
                    mine = {a.path.name: a for a in current if a.seat == name
                            and a.path.parent == service.attempts.dir
                            and old(a) and a.message in settled
                            and all(v.result in FINISHED and old(v) for v in groups[a.message])}
                    proofs, errors = pending._journals(service.runtime, name, progress=progress)
                    if errors:
                        continue  # incomplete journal inventory cannot authorize a move
                    blocked, finished = set(), []
                    for proof in proofs:
                        progress()
                        selected = set(proof.data['attempts'])
                        if (proof.data['phase'] not in JOURNALS or receipt_refresh.unfinished(proof.data)
                                or not selected <= all_names.keys()
                                or any(all_names[n].message not in settled
                                       or any(v.result not in FINISHED or not old(v)
                                              for v in groups[all_names[n].message]) for n in selected)):
                            blocked.update(selected)
                        else:
                            finished.append(proof)
                    # A recorded service boundary still cites its original path.
                    pointer = service.runtime / 'service.delivery'
                    if pointer.exists():
                        try:
                            data = json.loads(pointer.read_text())
                            relative = data['journal']
                            inflight = service.runtime / relative
                            own = next((p for p in proofs if p.path == inflight), None)
                            if own:
                                blocked.update(own.data['attempts'])
                            elif not isinstance(relative, str) or not inflight.is_file():
                                continue
                        except (OSError, ValueError, KeyError, TypeError):
                            continue
                    mine = {n: a for n, a in mine.items() if n not in blocked}
                    mine = dict(list(mine.items())[:max(0, limit - counts['attempts'])])
                    selected = [proof for proof in finished
                                if not set(proof.data['attempts']) & blocked][:limit]
                    if mine or selected:
                        return _batch(service, name, mine, selected, progress, counts, all_names)
        except lifecycle.Busy:
            continue
        finally:
            # A bounded pass must not repeatedly spend its whole slice scanning
            # already-settled early seats while later seats never get a turn.
            service.post_retirement_cursor = names[(position + 1) % len(names)]
    return counts


def due(service):
    now = time.monotonic()
    if now < getattr(service, 'post_retirement_due', 0):
        return
    result = run(service)
    service.post_retirement_due = now + INTERVAL
    if result['attempts'] or result['journals']:
        service.log('post-retired', **result)
