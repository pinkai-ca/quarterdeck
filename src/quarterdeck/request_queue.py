"""One owner for queued seat requests, their visible waits and terminal answers.

Readers never acquire the writer lock or create files. Writers serialize claim,
dispatch and settlement with cancellation and direct-request supersession.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import secrets
import stat

from . import durable, post
from .ship import ShipError


SEAT_KINDS = frozenset({'relaunch', 'control', 'select', 'stop'})


def launch_binding(seat):
    launched = seat.launched()
    if launched is None:
        return None
    return dict(pane=launched.pane, pid=launched.pid, at=launched.at, harness=launched.harness)


@contextmanager
def coordination(runtime):
    folder = runtime / 'requests'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / 'queue.lock'
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ShipError('request coordination is not a regular file; retained')
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        current = path.lstat()
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise ShipError('request coordination changed; retained')
        yield
    finally:
        os.close(descriptor)


def load(path):
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or not isinstance(data.get('kind'), str):
        raise ShipError('request payload unsupported; retained')
    return data


def created(path, data):
    raw = data.get('created_at')
    if raw is None:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    at = datetime.fromisoformat(raw)
    if at.tzinfo is None:
        raise ValueError('request time has no timezone')
    return at.isoformat()


def claimed(path):
    try:
        path.with_suffix('.claimed').lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return None


def view(path, data=None):
    """Operational metadata only, never control arguments or a conversation."""
    data = load(path) if data is None else data
    waiting = data.get('waiting') or {}
    reason = waiting.get('reason') if isinstance(waiting, dict) else None
    reason = reason if isinstance(reason, str) and reason else 'service has not supplied its decision yet'
    reason = ''.join(char for char in reason if char.isprintable())[:512]
    kind = data['kind']
    if kind == 'relaunch':
        kind = 'restart' if data.get('resume') is True else 'fresh' if data.get('resume') is False else 'relaunch'
    return dict(id=path.stem, seat=data.get('seat'), kind=kind, since=created(path, data),
                reason=reason, dimension=waiting.get('dimension', 'unknown') if isinstance(waiting, dict) else 'unknown',
                claimed=claimed(path))


def pending(runtime, name=None):
    rows = []
    for path in sorted((runtime / 'requests').glob('*.json')):
        data = None
        try:
            before = path.stat()
            data = load(path)
            if data['kind'] not in SEAT_KINDS or not isinstance(data.get('seat'), str):
                continue
            if name is not None and data['seat'] != name:
                continue
            row = view(path, data)
            after = path.stat()
            if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                row.update(reason='request changed while reading; hold unknown', dimension='unknown')
            rows.append(row)
        except FileNotFoundError:
            continue  # a settled request disappeared; never write to repair a reading
        except (OSError, ValueError, TypeError, ShipError):
            target = data.get('seat') if isinstance(data, dict) and isinstance(data.get('seat'), str) else None
            if name is None or target == name:
                rows.append(dict(id=path.stem, seat=target, kind='unknown', since=None,
                                 reason='request metadata unreadable', dimension='unknown', claimed=claimed(path)))
    return rows


def describe(row):
    claim = ('; outcome unknown, already claimed' if row['claimed'] is True else
             '; claim state unknown' if row['claimed'] is None else '')
    target = row['seat'] or 'unknown seat'
    cancel = f'; cancel: qd restart {target} --cancel {row["id"]}' if row['seat'] and row['claimed'] is not True else ''
    return (f'{target}: request {row["id"]} {row["kind"]} since {row["since"] or "unknown"}; '
            f'waiting: {row["reason"]}{claim}{cancel}')


def hold(path, data, dimension, reason):
    data.setdefault('created_at', created(path, data))
    prior = data.get('waiting')
    if not isinstance(prior, dict) or (prior.get('dimension'), prior.get('reason')) != (dimension, reason):
        data['waiting'] = dict(dimension=dimension, reason=reason, since=post.now().isoformat())
    durable.replace_file(path, json.dumps(data, sort_keys=True) + '\n')


def settle(path, proof, *, clear_claim=False):
    """Under coordination: the done file precedes retirement of the active name."""
    durable.replace_file(path.with_suffix('.done'), proof + '\n')
    path.unlink()
    durable.sync_folder(path.parent)
    if clear_claim:
        path.with_suffix('.claimed').unlink(missing_ok=True)
        durable.sync_folder(path.parent)


def terminal(path, data, proof):
    """Durable retirement intent, protected from both new and older service replay."""
    durable.replace_file(path.with_suffix('.claimed'), json.dumps(data) + '\n')
    data['terminal'] = proof
    durable.replace_file(path, json.dumps(data, sort_keys=True) + '\n')
    settle(path, proof, clear_claim=True)


def _retire(runtime, name, reason, identifier=None):
    paths = []
    uncertain = False
    for path in sorted((runtime / 'requests').glob('*.json')):
        try:
            data = load(path)
        except (OSError, ValueError, ShipError):
            if identifier == path.stem:
                raise ShipError(f'request {identifier} metadata unknown; retained') from None
            uncertain = True
            continue
        if data.get('seat') != name or data['kind'] not in SEAT_KINDS:
            continue
        if identifier is not None and path.stem != identifier:
            continue
        if identifier is None and data['kind'] != 'relaunch':
            continue
        if claimed(path) is not False and not isinstance(data.get('terminal'), str):
            raise ShipError(f'request {path.stem} already claimed; outcome unknown; retained')
        paths.append((path, data))
    if identifier is None and uncertain:
        raise ShipError('request inventory unreadable; no retirement proved; retained')
    if identifier is not None and not paths:
        raise ShipError(f'no pending request {identifier} for {name}; nothing cancelled')
    for path, data in paths:
        proof = data.get('terminal') or f'{name}: {data["kind"]} request {path.stem} {reason}; never applied by this request'
        terminal(path, data, proof)
    return [path.stem for path, _ in paths]


def cancel(runtime, name, identifier=None):
    with coordination(runtime):
        ids = _retire(runtime, name, 'cancelled', identifier)
    return f'{name}: cancelled request(s) {", ".join(ids)}; settled without applying' if ids else f'{name}: no pending relaunch; nothing cancelled'


@contextmanager
def direct(runtime, name, kind):
    """Retirement and the existing direct act share the same writer coordination."""
    with coordination(runtime):
        yield _retire(runtime, name, f'superseded by direct {kind}')


def enqueue(runtime, kind, data):
    with coordination(runtime):
        retired = _retire(runtime, data['seat'], 'superseded by a newer direct relaunch') if kind == 'relaunch' else []
        name = f'{datetime.now().strftime("%H%M%S")}-{kind}-{os.getpid()}'
        folder = runtime / 'requests'
        path = folder / f'{name}.json'
        while any(path.with_suffix(suffix).exists() for suffix in ('.json', '.done', '.claimed')):
            path = folder / f'{name}-{secrets.token_hex(4)}.json'
        payload = dict(kind=kind, **data, created_at=post.now().isoformat())
        if retired:
            payload['retired'] = retired
        if kind in SEAT_KINDS and isinstance(data.get('seat'), str):
            with post.admission(runtime, data['seat']):
                durable.replace_file(path, json.dumps(payload) + '\n')
        else:
            durable.replace_file(path, json.dumps(payload) + '\n')
        return path
