"""One owner for queued seat requests, their visible waits and terminal answers.

Readers never acquire the writer lock or create files. Writers serialize claim,
dispatch and settlement with cancellation and direct-request supersession.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import time

from . import durable, post
from .ship import ShipError


SEAT_KINDS = frozenset({'relaunch', 'control', 'select', 'stop', 'submit', 'press', 'type', 'set-aside'})
SUPERSEDING_KINDS = frozenset({'relaunch', 'select'})
QUICK_KINDS = frozenset({'submit', 'resend'})
NOTICE_RECEIPTS = 'request-notices.json'  # canonical trusted file; service alone writes it
RESTART_SETTLEMENT = 'settled without applying: the service that claimed it restarted'


def arrival_paths(runtime, *, kinds=None):
    """One in-memory scan, yielding after each file, then paths in arrival order.

    The caller may pause between yields; no writer lock spans that pause. Files
    remain the only queue. A missing or malformed entry still has its ordinary
    dispatch/error path in the full request step.
    """
    ordered, unranked = [], []
    for path in (runtime / 'requests').glob('*.json'):
        try:
            if kinds is None or load(path).get('kind') in kinds:
                try:
                    at = datetime.fromisoformat(created(path, metadata(path)))
                except (OSError, ValueError, TypeError, ShipError):
                    # Preserve a named error through the ordinary writer; sorting
                    # never settles or silently discards an unreadable request.
                    at = datetime.min.replace(tzinfo=timezone.utc)
                ordered.append((at, path.stat().st_mtime_ns, path.name, path))
        except (OSError, ValueError, TypeError, ShipError):
            if kinds is None:
                # The full writer still owns this path and its named outcome;
                # an unreadable sort key never discards or authorizes it.
                unranked.append(path)
            # Unknown kinds/order stay out of the duty-boundary slice.
        yield None  # directory/file inspection is one cooperative scan unit
    for _, _, _, path in sorted(ordered):
        yield path
    yield from unranked


class RequestHold(ShipError):
    """Unproved request ordering is a visible wait, never a failed-request retirement."""

    def __init__(self, dimension, reason):
        super().__init__(reason)
        self.dimension = dimension


def launch_binding(seat):
    launched = seat.launched()
    if launched is None:
        return None
    return dict(pane=launched.pane, pid=launched.pid, at=launched.at, harness=launched.harness,
                start=getattr(launched, 'start', None))


NOT_OWN = 'not the seat\'s own'
ANCESTRY_HOPS = 256  # far deeper than any shell, program and tool chain under a pane


def _parent(pid):
    """A process's parent, from /proc, or None when it cannot be read."""
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return int(fields[1])
    except (OSError, IndexError, ValueError, UnicodeError):
        return None


def own_proof(seat_name, origin):
    """The filing process's proof that it descends from the seat's own pane process, read from /proc now.

    The pane process is the launch's recorded pid with its recorded start time, so a reused pid never counts. A
    request that names the seat as its own caller is filed only with this proof; anything else is refused, with
    the reason, and nothing is filed.
    """
    from .seat import process_start
    if not isinstance(origin, dict) or type(origin.get('pid')) is not int or not origin.get('start'):
        raise ShipError(f'{seat_name}: refused as {NOT_OWN}: its launch records no pane process and start time to '
                        'prove the caller by; no request filed')
    pane, start = origin['pid'], origin['start']
    filer = os.getpid()
    hop = filer
    for _ in range(ANCESTRY_HOPS):
        if hop is None or hop <= 0:
            break
        if hop == pane:
            if process_start(hop) != start:
                break  # the pid is reused by a process with another start
            return dict(pane=pane, pane_start=start, filer=filer, filer_start=process_start(filer))
        if hop == 1:
            break
        hop = _parent(hop)
    raise ShipError(f'{seat_name}: refused as {NOT_OWN}: the filing process {filer} does not descend from '
                    f'{seat_name}\'s pane process {pane} started at {start}; no request filed')


def proved_own(seat_name, data, origin):
    """Whether a request is the seat's own: it names the seat as its caller, and was filed with the proof that its
    filer descended from the pane process of the launch the seat still runs."""
    proof = data.get('own')
    return (data.get('caller') == seat_name and isinstance(proof, dict) and isinstance(origin, dict)
            and data.get('origin') == origin and proof.get('pane') == origin.get('pid')
            and proof.get('pane_start') is not None and proof.get('pane_start') == origin.get('start'))


@contextmanager
def coordination(runtime, *, wait=True):
    """Bound CLI admission; service passes defer immediately when another owner is busy."""
    folder = runtime / 'requests'
    folder.mkdir(parents=True, exist_ok=True)
    from . import ship_locks
    path = ship_locks.path(runtime, 'request.lock')  # hidden from every jail
    ship_locks.prepare(path)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ShipError('request coordination is not a regular file; retained')
        from .servicectl import ANSWER_SECONDS
        deadline = time.monotonic() + ANSWER_SECONDS
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not wait:
                    yield False
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ShipError('request owner is busy; coordination wait expired; nothing applied') from None
                time.sleep(min(0.05, remaining))
        current = path.lstat()
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise ShipError('request coordination changed; retained')
        yield True
    finally:
        os.close(descriptor)


def load(path):
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or not isinstance(data.get('kind'), str):
        raise ShipError('request payload unsupported; retained')
    return data



def notice_path(runtime, seat):
    """Only the service's canonical trusted receipt file, never a filed request."""
    from . import trusted_files
    if (not isinstance(seat, str) or not seat or Path(seat).name != seat
            or seat.startswith('.') or '\\' in seat or not seat.isprintable()):
        raise ShipError('notice seat name unsupported')
    return trusted_files.path(runtime, seat, NOTICE_RECEIPTS)


def notice_stamp(runtime, seat):
    try:
        info = notice_path(runtime, seat).lstat()
    except FileNotFoundError:
        return (None,)
    if not stat.S_ISREG(info.st_mode):
        raise ShipError('notice receipt is not a regular file')
    return ((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode),)


def notice_records(runtime, seat):
    """Read-only terminal evidence; an absent receipt proves no completion."""
    try:
        data = json.loads(notice_path(runtime, seat).read_text())
    except FileNotFoundError:
        return {}
    if (not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != 1
            or set(data) != {'version', 'receipts'} or not isinstance(data['receipts'], dict)):
        raise ShipError('notice receipts unsupported')
    for key, row in data['receipts'].items():
        if (not isinstance(row, dict) or set(row) != {'request', 'seat', 'notice', 'state', 'at', 'stamp'}
                or row['seat'] != seat or row['notice'] != key or not isinstance(key, str) or not key
                or not isinstance(row['request'], str) or not row['request']
                or row['state'] not in ('applied', 'withdrawn', 'retired')
                or not isinstance(row['stamp'], list) or len(row['stamp']) != 6
                or any(type(value) is not int for value in row['stamp'])
                or not isinstance(row['at'], str) or datetime.fromisoformat(row['at']).tzinfo is None):
            raise ShipError('notice receipt record unsupported')
    return data['receipts']


def notice_state(runtime, identifier, seat, notice, *, records=None):
    records = notice_records(runtime, seat) if records is None else records
    row = records.get(notice)
    return row['state'] if row is not None and row['request'] == identifier and row['seat'] == seat else None


def record_notice_completion(path, data, meta, state):
    """Service-only completion writer. Clients leave a terminal intent for that service."""
    if data.get('kind') != 'relaunch' or not isinstance(meta.get('told'), str) or not meta['told']:
        return
    from . import post_dir, post_notices, trusted_files
    notice, seat = meta['told'], data.get('seat')
    if Path(notice).name != notice or notice in ('.', '..'):
        raise ShipError('notice name unsupported')
    runtime = path.parent.parent
    try:
        message = post.Message.from_json(post_dir.read_text(runtime / 'post' / 'store' / (notice + '.json')))
    except FileNotFoundError:
        return  # no published notice to bind; never invent a settlement
    if (message.id != notice or message.to != seat or post_notices.held_request(message) != path.stem
            or state not in ('applied', 'withdrawn', 'retired')):
        return  # only the service's exact notice about this request can acquire evidence
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ShipError('notice request is not a regular file')
    stamp = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode]
    records = notice_records(runtime, seat)
    old = records.get(notice)
    if old is not None and (old['request'], old['state'], old['stamp']) == (path.stem, state, stamp):
        return
    records[notice] = dict(request=path.stem, seat=seat, notice=notice, state=state,
                           at=post.now().isoformat(), stamp=stamp)
    trusted_files.make(runtime, seat)
    durable.replace_file(notice_path(runtime, seat), json.dumps(dict(version=1, receipts=records), sort_keys=True)+'\n')


def prune_notice_records(runtime, seat, launched, *, records):
    """Service-only pruning; a canonical later launch already settles every removed notice."""
    if launched is None:
        return
    kept = {key: row for key, row in records.items() if not datetime.fromisoformat(row['at']) < launched}
    if len(kept) == len(records):
        return
    path = notice_path(runtime, seat)
    if kept:
        durable.replace_file(path, json.dumps(dict(version=1, receipts=kept), sort_keys=True)+'\n')
    else:
        path.unlink()


def created(path, data):
    raw = data.get('created_at')
    if raw is None:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    at = datetime.fromisoformat(raw)
    if at.tzinfo is None:
        raise ValueError('request time has no timezone')
    return at.isoformat()


def state_path(path):
    return path.parent.parent / 'request-state' / path.with_suffix('.state').name


def metadata(path):
    try:
        data = json.loads(state_path(path).read_text())
    except FileNotFoundError:
        return dict(version=1, created_at=created(path, {}))
    if not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != 1:
        raise ShipError('request metadata unsupported; retained')
    return data


def save_metadata(path, data):
    target = state_path(path)
    data.setdefault('version', 1)
    if 'created_at' not in data:
        data['created_at'] = created(path, {})
    text = json.dumps(data, sort_keys=True) + '\n'
    if target.exists() and target.read_text() == text:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    durable.replace_file(target, text)


def clear_metadata(path):
    target = state_path(path)
    target.unlink(missing_ok=True)


def acknowledged(path):
    """Client acknowledgment must never wait for or replace its complete answer."""
    try:
        if not path.exists():
            clear_metadata(path)
    except OSError:
        pass  # service settlement also retires metadata; answer delivery stays unchanged


def claimed(path):
    try:
        path.with_suffix('.claimed').lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return None


def claim(path, raw, meta):
    """Keep the immutable payload claim, with its process identity recorded first."""
    from .servicectl import process_start
    pid = os.getpid()
    meta['claim_owner'] = dict(pid=pid, start=process_start(pid),
                               digest=hashlib.sha256(raw.encode()).hexdigest())
    save_metadata(path, meta)
    durable.replace_file(path.with_suffix('.claimed'), raw)


def stale_claim(path, meta=None, *, service_start=None):
    """Only the exact claim's proved-ended process can be settled; unknown stays held."""
    from .shutdown import process_gone
    try:
        if claimed(path) is not True:
            return False
        meta = metadata(path) if meta is None else meta
        claim = path.with_suffix('.claimed')
        raw = claim.read_bytes()
        if raw != path.read_bytes():
            return False
        if 'claim_owner' not in meta:
            # Only startup's newly owned lock supplies this wall-clock boundary.
            # Never infer a departed owner from an old mtime or a caller's PID.
            if service_start is None:
                return False
            info = claim.lstat()
            return (stat.S_ISREG(info.st_mode) and info.st_dev == service_start['device']
                    and 0 < info.st_ctime_ns < service_start['ctime_ns'] - post.POST_TIMESTAMP_TICK_NS)
        owner = meta.get('claim_owner')
        if (not isinstance(owner, dict) or type(owner.get('pid')) is not int or owner['pid'] <= 0
                or not isinstance(owner.get('start'), str) or not owner['start'].isdigit()):
            return False
        if owner.get('digest') != hashlib.sha256(raw).hexdigest():
            return False
        return process_gone(owner)
    except (OSError, ValueError, TypeError, ShipError):
        return False


def control_continuation(path, data=None, meta=None):
    """Only the original immutable payload/claim can resume its own consumed control."""
    from .control_confirmation import Continuation
    data = load(path) if data is None else data
    meta = metadata(path) if meta is None else meta
    if 'control_confirmation' not in meta:
        return None
    state = Continuation.from_metadata(meta['control_confirmation'])
    origin = state.origin
    launch, inputs, record = (origin.get(key) for key in ('launch', 'inputs', 'record'))
    digest = lambda value: (isinstance(value, str) and len(value) == 64
                            and all(char in '0123456789abcdef' for char in value))
    if (state.control not in {'model', 'effort'} or not isinstance(launch, dict) or not isinstance(inputs, dict)
            or not isinstance(record, dict) or not isinstance(origin.get('declaration'), list)
            or len(origin['declaration']) != 3 or not isinstance(record.get('display'), dict)
            or any(key not in inputs for key in ('record', 'device', 'inode', 'input_count', 'inputs_digest'))
            or any(key not in record for key in ('path', 'device', 'inode'))
            or any(not isinstance(launch.get(key), str) or not launch[key] for key in ('pane', 'at', 'harness'))
            or type(launch.get('pid')) is not int or launch['pid'] <= 0
            or type(record.get('offset')) is not int or record['offset'] < 0
            or not digest(record.get('prefix'))):
        raise ShipError('control continuation provenance unknown; retained without retyping')
    bound_record = (isinstance(record.get('path'), str) and bool(record['path'])
                    and all(type(record.get(key)) is int and record[key] >= 0 for key in ('device', 'inode')))
    pinned_inputs = (isinstance(inputs.get('record'), str) and bool(inputs['record'])
                     and all(type(inputs.get(key)) is int and inputs[key] >= 0
                             for key in ('device', 'inode', 'input_count'))
                     and digest(inputs.get('inputs_digest')) and bound_record
                     and record['path'] == inputs['record']
                     and all(record[key] == inputs[key] for key in ('device', 'inode')))
    first_inputs = (inputs.get('record') is None and inputs.get('device') is None and inputs.get('inode') is None
                    and type(inputs.get('input_count')) is int and inputs['input_count'] == 0
                    and inputs.get('inputs_digest') == hashlib.sha256(json.dumps([]).encode()).hexdigest()
                    and inputs.get('launch') == launch and record.get('first_record_launch') == launch
                    and record['offset'] == 0 and record['prefix'] == hashlib.sha256(b'').hexdigest()
                    and record['display'].get('synthetic') is True
                    and (bound_record or record.get('path') is None and record.get('device') is None
                         and record.get('inode') is None))
    if not pinned_inputs and (state.control != 'model' or not first_inputs):
        raise ShipError('control continuation record provenance unknown; retained without retyping')
    if (data.get('kind') != 'control' or data.get('name') != state.control
            or str(data.get('value')) != state.value or data.get('seat') != state.origin.get('seat')
            or claimed(path) is not True or path.read_bytes() != path.with_suffix('.claimed').read_bytes()):
        raise ShipError('control continuation owner unknown; retained without retyping')
    return state


# How long a queued model change suppresses the difference it is to settle. It waits for its seat's turn to end:
# of the crew's 16,547 turns from 10 to 17 November 2026 in the ledger, half ended within 28 s, 95% within 8.2 min and
# 99% within 26.3 min. Thirty minutes passes 99% of turns; a request queued longer is stuck, and the difference is told.
QUEUED_MODEL_SECONDS = 1800


def pending_model(seat, expected):
    """Read-only suppression of a declared-against-running model difference while a change to it is pending: a
    well-formed current request-owner continuation, or a well-formed model request for this seat and this value still
    queued, for QUEUED_MODEL_SECONDS from its arrival. Unknown state never suppresses."""
    from . import post
    for path in sorted((seat.ship.runtime / 'requests').glob('*.json')):
        try:
            state = control_continuation(path)
            if (state is not None and state.control == 'model' and state.value == expected
                    and state.origin.get('seat') == seat.name
                    and (state.origin['inputs']['record'] is not None or getattr(seat.harness, 'synthetic', False) is True)
                    and state.origin.get('launch') == launch_binding(seat)
                    and state.origin.get('declaration') == [seat.harness.name, seat.spec.model_mode, seat.spec.model]
                    and datetime.fromisoformat(state.started) <= post.now() < datetime.fromisoformat(state.deadline)):
                return True
            if state is None and _queued_model(seat, path, expected, post.now()):
                return True
        except (OSError, ValueError, TypeError, ShipError):
            continue  # unknown state never suppresses a difference
    return False


def _queued_model(seat, path, expected, now):
    """A model request for this seat, whose value is the declared model, waiting unclaimed and unsettled, and younger
    than QUEUED_MODEL_SECONDS. Its arrival is the request's own (created), the same for every reader and across a
    service's restart."""
    data = load(path)
    if (data.get('kind') != 'control' or data.get('name') != 'model' or data.get('seat') != seat.name
            or not isinstance(data.get('value'), str) or not data['value']):
        return False
    if claimed(path) is not False or 'terminal' in metadata(path):
        return False  # claimed (being applied, or its outcome unknown), settled, or its claim unreadable
    launched = seat.launched()
    if seat.harness.model_identity(data['value'], version=launched.version if launched else None) != expected:
        return False
    arrived = datetime.fromisoformat(created(path, data))
    return arrived <= now and (now - arrived).total_seconds() < QUEUED_MODEL_SECONDS


def view(path, data=None):
    """Operational metadata only, never control arguments or a conversation."""
    data = load(path) if data is None else data
    meta = metadata(path)
    waiting = meta.get('waiting') or {}
    reason = waiting.get('reason') if isinstance(waiting, dict) else None
    reason = reason if isinstance(reason, str) and reason else 'service has not supplied its decision yet'
    reason = ''.join(char for char in reason if char.isprintable())[:512]
    kind = data['kind']
    if kind == 'relaunch':
        kind = 'restart' if data.get('resume') is True else 'fresh' if data.get('resume') is False else 'relaunch'
    return dict(id=path.stem, seat=data.get('seat'), kind=kind, since=created(path, meta),
                reason=reason, dimension=waiting.get('dimension', 'unknown') if isinstance(waiting, dict) else 'unknown',
                claimed=claimed(path), continuation=control_continuation(path, data, meta) is not None)


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
    claim = ('; owned control awaiting confirmation' if row.get('continuation') else
             '; outcome unknown, already claimed' if row['claimed'] is True else
             '; claim state unknown' if row['claimed'] is None else '')
    target = row['seat'] or 'unknown seat'
    cancel = f'; cancel: qd restart {target} --cancel {row["id"]}' if row['seat'] and row['claimed'] is not True else ''
    return (f'{target}: request {row["id"]} {row["kind"]} since {row["since"] or "unknown"}; '
            f'waiting: {row["reason"]}{claim}{cancel}')


def headline(row):
    return f'{row["seat"] or "unknown seat"} {row["kind"]} request waits: {row["reason"]}'


def hold(path, data, dimension, reason):
    if 'created_at' not in data:
        data['created_at'] = created(path, {})
    prior = data.get('waiting')
    if not isinstance(prior, dict) or (prior.get('dimension'), prior.get('reason')) != (dimension, reason):
        data['waiting'] = dict(dimension=dimension, reason=reason, since=post.now().isoformat())
    save_metadata(path, data)


def answer(path, proof):
    """The original complete answer file is the client's first completion signal."""
    durable.replace_file(path.with_suffix('.done'), proof + '\n')


def retire(path, *, clear_claim=False):
    path.unlink()
    if clear_claim:
        path.with_suffix('.claimed').unlink(missing_ok=True)
    clear_metadata(path)


def settle(path, proof, *, clear_claim=False):
    answer(path, proof)
    retire(path, clear_claim=clear_claim)


def terminal(path, data, proof, *, notice_state='retired'):
    """Durable retirement intent, protected from both new and older service replay."""
    meta = metadata(path)
    claim(path, path.read_text(), meta)
    meta['terminal'] = proof
    if data.get('kind') == 'relaunch' and meta.get('told'):
        meta.setdefault('notice_terminal_state', notice_state)
    save_metadata(path, meta)
    if data.get('kind') == 'relaunch' and meta.get('told'):
        # A client may withdraw or supersede, but never write trusted completion.
        # The payload/intent survives acknowledgment until the service finishes it.
        answer(path, proof)
    else:
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
        meta = metadata(path)
        proof = meta.get('terminal') or f'{name}: {data["kind"]} request {path.stem} {reason}; never applied by this request'
        if claimed(path) is not False and not isinstance(meta.get('terminal'), str):
            if 'control_confirmation' in meta:
                raise ShipError(f'request {path.stem} not {reason}: its command may already be typed; '
                                'the service confirms it; retained')
            if reason != 'cancelled' or not stale_claim(path, meta):
                raise ShipError(f'request {path.stem} already claimed; outcome unknown; retained')
            proof += '; the earlier claim owner ended without a terminal result'
        paths.append((path, data, proof))
    if identifier is None and uncertain:
        raise ShipError('request inventory unreadable; no retirement proved; retained')
    if identifier is not None and not paths:
        raise ShipError(f'no pending request {identifier} for {name}; nothing cancelled')
    for path, data, proof in paths:
        terminal(path, data, proof, notice_state='withdrawn' if reason == 'cancelled' else 'retired')
    return [path.stem for path, _, _ in paths]


def cancel(runtime, name, identifier=None):
    with coordination(runtime):
        ids = _retire(runtime, name, 'cancelled', identifier)
    return f'{name}: cancelled request(s) {", ".join(ids)}; settled without applying' if ids else f'{name}: no pending relaunch; nothing cancelled'


@contextmanager
def direct(runtime, name, kind):
    """Retire and recheck working state here; transition only after leaving this scope."""
    with coordination(runtime):
        yield _retire(runtime, name, f'superseded by direct {kind}')


def supersede(runtime, path, data):
    """Service-owned retirement, before a pending relaunch or pool selection can dispatch.

    The caller holds writer coordination. Client publication never takes it,
    so a paused publication cannot block a healthy seat's request.
    """
    kind = data.get('kind')
    if kind not in SUPERSEDING_KINDS or not isinstance(data.get('seat'), str):
        return True
    candidates = []
    for other in sorted((runtime / 'requests').glob('*.json')):
        try:
            item = load(other)
        except FileNotFoundError:
            continue
        except (OSError, ValueError, ShipError):
            continue  # unrelated unreadable requests retain their own named outcome
        if item.get('kind') != kind or item.get('seat') != data['seat']:
            continue
        try:
            meta = metadata(other)
            if 'terminal' in meta:
                continue
            at = datetime.fromisoformat(created(other, meta))
        except (OSError, ValueError, TypeError, ShipError):
            raise RequestHold('order-unknown', f'{kind} order unknown; creation time unreadable; retained') from None
        candidates.append((at, other.stem, other, item, meta))
    if not candidates:
        return True
    latest = max(row[0] for row in candidates)
    newest = [row for row in candidates if row[0] == latest]
    if len(newest) != 1:
        raise RequestHold('order-unknown', f'{kind} order unknown; creation times are equal; retained')
    _, _, winner, _, meta = newest[0]
    older = [row for row in candidates if row[2] != winner]
    if not older:
        return True
    for _, _, old, _, _ in older:
        if claimed(old) is not False:
            raise RequestHold('outcome-unknown', f'request {old.stem} already claimed; outcome unknown; retained')
    meta['retired'] = sorted(set(meta.get('retired', [])) | {old.stem for _, _, old, _, _ in older})
    save_metadata(winner, meta)
    for _, _, old, item, _ in older:
        terminal(old, item, f'{data["seat"]}: {kind} request {old.stem} superseded by '
                 f'{winner.stem}; never applied by this request')
    return path == winner


def enqueue(runtime, kind, data, *, name=None):
    data = dict(data)
    context = {}
    if kind == 'relaunch':
        for key in ('caller', 'origin'):
            if key in data:
                context[key] = data.pop(key)
        data.pop('requested_at', None)  # never the filer's: the asked time is stamped here
        data.pop('own', None)  # the proof is made here, into the metadata alone; never carried in the body
        if context.get('caller') is not None and context['caller'] == data.get('seat'):
            context['own'] = own_proof(data['seat'], context.get('origin'))
    born = post.now().isoformat()  # the asked time, stamped at enqueue
    name = name or f'{datetime.now().strftime("%H%M%S")}-{kind}-{os.getpid()}'
    folder = runtime / 'requests'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f'{name}.json'
    if context:
        save_metadata(path, dict(version=1, created_at=born, **context))
    payload = json.dumps(dict(kind=kind, **data)) + '\n'
    if kind in SEAT_KINDS and isinstance(data.get('seat'), str):
        with post.admission(runtime, data['seat']):
            durable.replace_file(path, payload)
    else:
        durable.replace_file(path, payload)
    return path
