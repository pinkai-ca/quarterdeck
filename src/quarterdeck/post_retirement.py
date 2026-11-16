"""Finished delivery evidence, moved unchanged out of the service's working folders.

Retired attempts remain in Attempts.all through immutable batch metadata. Original
files are retained verbatim; named readers resolve either location. A batch index
is published before its first rename, so process interruption preserves history.
Power-loss persistence is left to the operating system; retirement forces no sync.
"""

from __future__ import annotations

from dataclasses import asdict
from contextlib import contextmanager
import json
import hashlib
from pathlib import Path
import secrets
import stat
import time

from . import trusted_files
from . import durable, post_dir
from .ship import ShipError

# These are finished outcomes, not a pending input or an unresolved receipt.
FINISHED = frozenset({'delivered', 'watch', 'launch', 'withheld', 'cancelled', 'abandoned',
                      'not-in-closed-record', 'unacted'})
SETTLED = frozenset({'delivered', 'watch'})
JOURNALS = frozenset({'delivered', 'cancelled', 'abandoned', 'unacted'})
BATCH = 1000
SECONDS = 1.0
HEARTBEAT = 0.1
INTERVAL = 300.0
STALLED_AFTER = 300.0
STATUS = 'retirement-status.json'
PHASES = frozenset({'start', 'recovery', 'ledger catch-up', 'attempts read', 'store read', 'eligibility',
                    'seat selection', 'journals read', 'batch', 'moves'})


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
    progress.phase = 'moves'
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
        try:
            data = json.loads(post_dir.read_text(path))
        except FileNotFoundError:
            if not post_dir.gone(path):
                raise
            continue
        if 'seat' not in data:
            continue  # older immutable metadata without a batch intent
        name = data['seat']
        if Path(name).name != name or name in ('.', '..'):
            raise ShipError('retirement batch seat invalid; originals retained')
        try:
            with lifecycle.claim(service.runtime, name), post.admission(service.runtime, name), service.attempts._locked():
                messages = _settled(service.store, progress.read('attempts read', service.attempts.all, progress=progress), progress)
                if any(message not in messages for message in data['messages']):
                    return {**counts, 'held': 'batch delivery unsettled'}
                return _resume(service, path, data, progress, counts)
        except lifecycle.Busy:
            progress.blocked = True
            continue
    return None


def _batch(service, name, mine, proofs, progress, counts, known):
    runtime = service.runtime
    root = runtime / 'post' / 'retired'
    progress()
    _folders([root / 'index', root / 'completed', root / 'attempts', root / 'pending' / name])
    journals = {}
    for proof in proofs:
        try:
            journals[proof.path.name] = hashlib.sha256(post_dir.read_bytes(proof.path)).hexdigest()
        except FileNotFoundError:
            if not post_dir.gone(proof.path):
                raise
    proofs = [proof for proof in proofs if proof.path.name in journals]
    message_ids = {a.message for a in mine.values()}
    message_ids.update(known[n].message for p in proofs for n in p.data['attempts'])
    data = dict(format=1, seat=name, messages=sorted(message_ids),
                attempts={n: row(a) for n, a in mine.items()},
                journals=journals)
    path = root / 'index' / (secrets.token_hex(16) + '.json')
    # Exactly one immutable index publication per pass, before its first rename.
    _replace_file(path, json.dumps(data, sort_keys=True) + '\n')
    return _resume(service, path, data, progress, counts)


class SliceEnded(Exception):
    """Ordinary progress: the next scheduled pass resumes from intact files."""


def _settled(store, records, progress):
    from .pending_lifecycle import REPORTED
    latest = {}
    for index, attempt in enumerate(records):
        if index % 200 == 0:
            progress()
        latest[attempt.message] = attempt
    # One immutable post snapshot. Repeated Store.closing calls would re-read
    # every message while a newly written directory is inside its timestamp bucket.
    messages = progress.read('store read', store.all)
    progress.phase = 'eligibility'
    by_id = {m.id: m for m in messages}
    closed = set()
    reported = set()
    for index, message in enumerate(messages):
        if index % 200 == 0:
            progress()
        parent = by_id.get(message.re)
        if parent is not None and message.kind == 'report' and message.sender == parent.to:
            reported.add(parent.id)
        if parent is not None and (parent.kind == 'question' and message.kind == 'answer'
                or parent.kind == 'order' and message.kind == 'report' and message.sender == parent.to):
            closed.add(parent.id)
    return {m.id for m in messages if (a := latest.get(m.id)) is not None
            and (a.result in SETTLED or m.id in reported and a.seat == m.to
                 and a.result == 'abandoned' and a.detail == REPORTED)
            and (not m.obligation or m.id in closed)}


class _Progress:
    """The service fits complete mandatory inventories around its work allowance."""

    def __init__(self, service, seconds, complete_reads, prior_names=()):
        self.service = service
        self.start = time.monotonic()
        self.deadline = self.start + seconds
        self.last_beat = self.start - HEARTBEAT
        self.complete_reads = complete_reads
        self.reading = False
        self.read_seconds = 0.0
        self.phase = 'start'
        self.candidates = None
        self.retirable = 0
        self.blocked = False
        self.names = None
        self.visited = set()
        self.prior_names = frozenset(prior_names)

    @contextmanager
    def examining(self, name):
        yield
        self.visited.add(name)  # an interrupted examination never reaches this line

    def __call__(self):
        now = time.monotonic()
        if now - self.last_beat >= HEARTBEAT:
            beat = getattr(self.service, 'heartbeat', None)
            if beat is not None:
                beat()
            self.last_beat = now
        if not self.reading and now >= self.deadline:
            raise SliceEnded()

    def read(self, phase, reader, *args, **kwargs):
        self.phase = phase
        start = time.monotonic()
        self.reading = self.complete_reads
        try:
            return reader(*args, **kwargs)
        finally:
            elapsed = max(0.0, time.monotonic() - start)
            self.read_seconds += elapsed
            if self.complete_reads:
                self.deadline += elapsed
            self.reading = False


def run(service, *, limit=BATCH, seconds=SECONDS, complete_reads=False, prior_names=()):
    """Fresh locked evidence always decides; service inventories finish before yielding.

    Direct callers retain the wall-time slice. The service separately accounts for
    complete inventories using its persistent readers, leaving the ordinary work
    allowance for selection and whole-file moves. No partial reading is authority.
    """
    counts = dict(attempts=0, journals=0)
    progress = _Progress(service, seconds, complete_reads, prior_names)
    result = None
    try:
        progress()
        result = _run(service, limit, counts, progress)
    except SliceEnded:
        result = {**counts, 'paused': True}
    finally:
        service._post_retirement_progress = progress
        service.post_retirement_counts = dict(counts)
        service.post_retirement_observation = dict(
            seconds=time.monotonic() - progress.start, phase=progress.phase,
            read_seconds=progress.read_seconds, candidates=progress.candidates,
            retirable=progress.retirable, blocked=progress.blocked)
    return result


def _run(service, limit, counts, progress):
    from . import lifecycle, pending, post, receipt_refresh
    if service.ledger.catching_up():
        progress.phase = 'ledger catch-up'
        return {**counts, 'held': 'ledger catch-up'}
    progress.phase = 'recovery'
    recovered = _recover(service, progress, counts)
    if recovered is not None:
        return recovered
    records = progress.read('attempts read', service.attempts.all, progress=progress)
    settled = _settled(service.store, records, progress)
    grouped = {}
    for attempt in records:
        grouped.setdefault(attempt.message, []).append(attempt)
    eligible = {a.path.name for group in grouped.values()
                if group[0].message in settled and all(a.result in FINISHED for a in group)
                for a in group}
    progress.candidates = sum(a.path.parent == service.attempts.dir and a.path.name in eligible for a in records)
    progress.phase = 'seat selection'
    names = sorted({a.seat for a in records if a.path.parent == service.attempts.dir
                    and a.path.name in eligible}
                   | set(post_dir.directory_names(service.runtime / 'post' / 'pending')))
    progress.names = frozenset(names)
    cursor = getattr(service, 'post_retirement_cursor', None)
    if cursor in names:
        position = names.index(cursor)
        names = names[position:] + names[:position]
    for position, name in enumerate(names):
        if name in progress.prior_names:
            continue  # completed under this same stable sweep input, never permission for a move
        progress()
        if counts['attempts'] >= limit:
            break
        try:
            with progress.examining(name), lifecycle.claim(service.runtime, name), post.admission(service.runtime, name):
                # Reload beneath admission; message closure and a delivery can no
                # longer change between the eligibility read and the rename.
                with service.attempts._locked():
                    if service.ledger.catching_up():
                        return {**counts, 'held': 'ledger catch-up'}
                    current = progress.read('attempts read', service.attempts.all, progress=progress)
                    launch_path = trusted_files.path(service.runtime, name, trusted_files.LAUNCH)
                    try:
                        launch = json.loads(launch_path.read_text())
                        live_join = (launch['pane'], launch['pid'], launch['at'])
                    except FileNotFoundError:
                        live_join = None
                    except (OSError, ValueError, KeyError, TypeError):
                        progress.blocked = True
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
                    proofs, errors = progress.read('journals read', pending._journals,
                                                   service.runtime, name, progress=progress)
                    progress.phase = 'eligibility'
                    if errors:
                        progress.blocked = True
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
                                progress.blocked = True
                            elif not isinstance(relative, str) or not inflight.is_file():
                                progress.blocked = True
                                continue
                        except (OSError, ValueError, KeyError, TypeError):
                            progress.blocked = True
                            continue
                    mine = {n: a for n, a in mine.items() if n not in blocked}
                    mine = dict(list(mine.items())[:max(0, limit - counts['attempts'])])
                    selected = [proof for proof in finished
                                if not set(proof.data['attempts']) & blocked][:limit]
                    if mine or selected:
                        progress.retirable += len(mine) + len(selected)
                        progress.phase = 'batch'
                        return _batch(service, name, mine, selected, progress, counts, all_names)
        except lifecycle.Busy:
            progress.blocked = True
            continue
        finally:
            # A bounded pass must not repeatedly spend its whole slice scanning
            # already-settled early seats while later seats never get a turn.
            service.post_retirement_cursor = names[(position + 1) % len(names)]
    if not progress.blocked:
        progress.candidates = 0  # a complete examination found no movable candidate
    return counts


def reading(runtime):
    """Derived display evidence only; never a retirement eligibility decision."""
    try:
        path = runtime / STATUS
        if path.stat().st_size > 4096:
            return 'retirement status unreadable'
        data = json.loads(path.read_text())
        if (not isinstance(data, dict) or set(data) != {'format', 'since', 'phase', 'misses'}
                or type(data['format']) is not int or data['format'] != 2
                or type(data['misses']) is not int or not 1 <= data['misses'] <= 2
                or type(data['since']) not in (int, float) or data['phase'] not in PHASES):
            return 'retirement status unreadable'
        age = time.time() - data['since']
        if not 0 <= age < float('inf'):
            return 'retirement status unreadable'
        return f"retirement stalled: {data['phase']}" if age >= STALLED_AFTER and data['misses'] >= 2 else None
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, KeyError):
        return 'retirement status unreadable'


def _status(service, result, observation):
    path = service.runtime / STATUS
    waiting = (result.get('paused') or observation['retirable']
               or observation['candidates'] and observation['blocked'])
    complete_empty = (not result.get('paused') and not result.get('held')
                      and observation['candidates'] == 0 and not observation['blocked'])
    if result['attempts'] or result['journals'] or complete_empty:
        path.unlink(missing_ok=True)
        return
    if not waiting and not path.exists():
        return  # no existing stall and no new evidence of one
    since, misses = time.time(), 0
    try:
        previous = json.loads(path.read_text())
        if reading(service.runtime) != 'retirement status unreadable':
            since, misses = previous['since'], previous['misses']
    except (OSError, ValueError, TypeError, KeyError):
        pass
    if result.get('held') != 'ledger catch-up':
        misses = min(2, misses + 1)
    durable.replace_file(path, json.dumps(dict(format=2, since=since, phase=observation['phase'],
                                             misses=max(1, misses))) + '\n')


def activity_stamp(runtime):
    """A few fixed directory observations for scheduling, never delivery evidence."""
    if not isinstance(runtime, Path):
        return None
    found = []
    for path in (runtime / 'post/store', runtime / 'post/attempts', runtime / 'requests'):
        try:
            info = path.lstat()
        except FileNotFoundError:
            found.append(None)
            continue
        except OSError:
            return None
        if not stat.S_ISDIR(info.st_mode):
            return None
        found.append((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns))
    return tuple(found)


def due(service, *, idle=False):
    now = time.monotonic()
    gate = None
    if getattr(service, 'ship', None) is not None and getattr(service, 'ledger', None) is not None:
        from .retirement_gate import Gate
        if getattr(service, 'post_retirement_gate', None) is None:
            service.post_retirement_gate = Gate()
        gate = service.post_retirement_gate
        if not gate.begin(service, now):
            return
    elif not idle and now < getattr(service, 'post_retirement_due', 0):
        return
    result = None
    try:
        result = (run(service, complete_reads=True, prior_names=gate.prior()) if gate is not None
                  else run(service, complete_reads=True))
    finally:
        service.post_retirement_due = time.monotonic() + INTERVAL
        observation = service.post_retirement_observation
        if result is None:
            result = {**getattr(service, 'post_retirement_counts', dict(attempts=0, journals=0)),
                      'paused': True, 'failed': True}
        if gate is not None:
            gate.finish(service, result, getattr(service, '_post_retirement_progress', None), INTERVAL)
        _status(service, result, observation)
        # Catch-up gates retirement before any inventory/pass can begin.
        # Actual passes, including failures and zero moves, always have a line.
        if result.get('held') != 'ledger catch-up':
            service.log('post-retired', **result, **observation)
