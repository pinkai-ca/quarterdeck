"""Explicit compaction handoffs. Optional runtime intent; immutable posts stay authoritative.

Lock order is lifecycle, sorted recipient admission, attempts. Admission never spans native startup.
Only a recorded, unchanged turn plus the seat's own attestation may claim an automatic fresh.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import secrets

from . import durable, post, record_owner
from .ship import ShipError

VERSION = 1


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def path(runtime, name):
    if Path(name).name != name or name in ('.', '..'):
        raise ShipError('invalid compaction seat identity')
    return runtime / 'seats' / name / 'compaction.json'


def load(runtime, name):
    try:
        data = json.loads(path(runtime, name).read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise ShipError('compaction journal unreadable; automatic fresh held') from error
    if not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != VERSION or not isinstance(data.get('generation'), str):
        raise ShipError('compaction journal unsupported; automatic fresh held')
    required = {'count', 'source', 'source_launch', 'request', 'protocol_requests', 'reserved', 'handoff_digest', 'used', 'ready', 'attestation', 'transition', 'error', 'intents', 'reason'}
    if (not required <= data.keys() or data['count'] is not None and (type(data['count']) is not int or data['count'] < 0)
            or any(data[key] is not None and not isinstance(data[key], dict)
                   for key in ('source', 'source_launch', 'ready', 'attestation', 'transition'))
            or any(data[key] is not None and not isinstance(data[key], str) for key in ('request', 'reserved', 'handoff_digest', 'error'))
            or any(not isinstance(data[key], list) for key in ('used', 'intents', 'protocol_requests'))
            or any(not isinstance(item, str) for key in ('used', 'intents', 'protocol_requests') for item in data[key])
            or not isinstance(data['reason'], str)):
        raise ShipError('compaction journal invalid; automatic fresh held')
    if (data['source'] is not None and any(not isinstance(data['source'].get(key), str) for key in ('record', 'harness', 'conversation'))
            or data['attestation'] is not None and (not isinstance(data['attestation'].get('revision'), str)
                or not isinstance(data['attestation'].get('report'), str) or not isinstance(data['attestation'].get('binding'), dict))):
        raise ShipError('compaction journal identity invalid; automatic fresh held')
    for intent in [*data['intents'], data['request'], (data['attestation'] or {}).get('report')]:
        if intent is not None:
            decode(intent)
    return data


def save(runtime, name, data):
    target = path(runtime, name)
    text = json.dumps(data, sort_keys=True) + '\n'
    try:
        if target.read_text() == text:
            return
    except FileNotFoundError:
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    durable.replace_file(target, text)


def new():
    return dict(version=VERSION, generation=secrets.token_hex(16), count=None, source=None, source_launch=None,
                request=None, protocol_requests=[], reserved=None, handoff_digest=None, used=[], ready=None,
                attestation=None, transition=None, error=None, intents=[], reason='count unknown')


def launch_key(seat):
    return key_for(seat.launched())


def key_for(launched):
    return dict(pane=launched.pane, pid=launched.pid, at=launched.at,
                harness=record_owner.launch_reader(launched)) if launched else None


def live_identity(seat):
    launch, pane = launch_key(seat), seat.pane()
    return bool(launch and pane and not pane.dead and (pane.id, pane.pid) == (launch['pane'], launch['pid']))


def config_key(seat):
    # Configuration identity, never work-content inspection.
    declaration = seat.ship.folder / 'ship.toml'
    try:
        raw = declaration.read_bytes()
    except OSError:
        raw = b'declaration unavailable'
    return hashlib.sha256(repr((seat.spec, seat.session)).encode() + raw).hexdigest()


def release(data, reason):
    if data.get('reserved') and data['reserved'] not in data['used']:
        data['used'].append(data['reserved'])
    data.update(reserved=None, handoff_digest=None, ready=None, attestation=None, reason=reason)


def disarm(seat, reason='ordinary lifecycle action; prepare a current handoff'):
    """Called inside the ordinary lifecycle claim, or by explicit cancellation/disable."""
    if not path(seat.ship.runtime, seat.name).exists():
        return 'no prepared compaction readiness'
    with post.admission(seat.ship.runtime, seat.name):
        return disarm_locked(seat, reason)


def disarm_locked(seat, reason, *, disabled=None, clear_transition=True):
    """The caller owns recipient admission (including a declaration edit)."""
    try:
        data = load(seat.ship.runtime, seat.name)
    except ShipError:
        # An unreadable journal cannot arm; never make it a veto on lawful recovery.
        return 'compaction journal unknown and retained; automatic fresh held'
    if data is None:
        return 'no prepared compaction readiness'
    try:
        recover(seat.ship.runtime, data)
    except (OSError, ShipError):
        data['error'] = 'immutable intent publication unresolved; automatic fresh held'
    release(data, reason)
    if clear_transition:
        data['transition'] = None
    if disabled is not None:
        data['disabled'] = disabled
    save(seat.ship.runtime, seat.name, data)
    return reason


def admitting(runtime, message):
    """Before publication, under recipient admission: reserve handoffs, invalidate later work."""
    try:
        data = load(runtime, message.to)
    except ShipError:
        return  # unknown holds automation, never guesses a reservation or vetoes ordinary posts
    if data is None:
        return
    if data.get('transition'):
        return
    try:
        recover(runtime, data)
    except (OSError, ShipError):
        data['error'] = 'immutable intent publication unresolved; automatic fresh held'
        release(data, data['error'])
        save(runtime, message.to, data)
        return
    request = post.Message.from_json(data['request']) if data.get('request') else None
    if (request and message.kind == 'order' and message.sender == message.to
            and message.re == request.id and not data.get('disabled')):
        release(data, 'handoff published; waiting for explicit readiness')
        data.update(reserved=message.id, handoff_digest=digest(message.to_json()))
        data['intents'].append(message.to_json())
        save(runtime, message.to, data)  # before its immutable name is reader-visible
    elif message.kind != 'ack' and (data.get('ready') or data.get('attestation')):
        release(data, 'new incoming work; prepare a new handoff and renew readiness')
        save(runtime, message.to, data)


def protect_closure(runtime, message):
    """A report can close only an order to its sender; that admission lock is held."""
    if message.kind != 'report' or not message.re:
        return
    try:
        data = load(runtime, message.sender)
    except ShipError:
        return  # the unreadable journal cannot authorize automation
    if data and message.re in (data.get('reserved'), (data.get('transition') or {}).get('handoff')):
        raise ShipError('the reserved handoff stays open for the successor; cancel readiness first')


def protocol_requests(seat):
    data = load(seat.ship.runtime, seat.name)
    return set(data['protocol_requests']) if data else set()


def withheld(runtime, name):
    try:
        data = load(runtime, name)
    except ShipError:
        return set(), False
    return ({data['reserved']} if data and data.get('reserved') else set(),
            bool(data and data.get('transition')))


def decode(intent):
    try:
        message = post.Message.from_json(intent)
        if not isinstance(message.id, str) or not message.id or Path(message.id).name != message.id:
            raise ValueError('intent identity')
        return message
    except (ValueError, TypeError, AttributeError, KeyError) as error:
        raise ShipError('compaction immutable intent invalid; automatic fresh held') from error


def publish(runtime, intent):
    """Recover a fixed intent under admission, refusing conflicting immutable bytes."""
    message = decode(intent)
    store = post.Store(runtime)
    target = store.dir / (message.id + '.json')
    if target.exists():
        if target.read_text() != intent:
            raise ShipError('compaction immutable post conflict')
    else:
        store._write(message)
    return message


def recover(runtime, data):
    for intent in data['intents']:
        publish(runtime, intent)
    data['intents'] = []  # the immutable post is now durable; keep only pending publication intents here


def incoming(runtime, name, request):
    excluded = {post.Message.from_json(request).id} if request else set()
    data = load(runtime, name)
    if data and data.get('reserved'):
        excluded.add(data['reserved'])
    ids = [m.id for m in post.Store(runtime).for_seat(name) if m.kind != 'ack' and m.id not in excluded]
    return hashlib.sha256(json.dumps(ids).encode()).hexdigest()


def current(seat):
    try:
        launched = seat.launched()
        joined = seat.join()
        if joined.record is None or record_owner.pinned_reader(seat, joined.record) != seat.spec.harness:
            raise ShipError('current owned record identity not proved')
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise ShipError('current launch/record identity unreadable') from error
    reading = seat.harness.compactions(joined.record)
    if reading.reason or reading.count is None or reading.state is None:
        raise ShipError(reading.reason or 'compaction count unknown')
    from dataclasses import replace
    from . import idle
    reading = replace(reading, state=idle.execution(seat, reading.state))
    if launched and launched.resume and launched.resume != reading.conversation:
        raise ShipError('saved resume does not name the joined native conversation')
    if seat.launched() != launched:
        raise ShipError('launch changed during compaction read')
    return joined.record, reading, launched


def observe(seat, data, record, reading, launched):
    if seat.launched() != launched:
        raise ShipError('launch changed before compaction observation')
    source = dict(record=str(record), harness=seat.spec.harness, conversation=reading.conversation)
    prior = data.get('source')
    key = key_for(launched)
    # The already-durable launch and its owned first record are enough even if
    # service died immediately after startup. Never infer a successor from a new PID alone.
    fresh = bool(launched and key != data['source_launch'] and launched.resume is None
                 and str(record) not in launched.records_before and reading.origin >= launched.when)
    if prior and prior != source:
        same = (prior['harness'], prior['conversation']) == (source['harness'], source['conversation'])
        if not fresh or same:
            raise ShipError('conversation changed without a proved fresh successor')
        replacement = new()
        recover(seat.ship.runtime, data)
        replacement['protocol_requests'] = data['protocol_requests']
        replacement['reason'] = 'proved fresh successor'
        data.clear()
        data.update(replacement)
    elif prior:
        if key and data['source_launch'] and key != data['source_launch'] and launched.resume is None:
            raise ShipError('fresh launch has not proved a new native origin')
        if data.get('error'):
            raise ShipError(data['error'])
        extent = data.get('extent', 0)
        if len(reading.raw) < extent or hashlib.sha256(reading.raw[:extent]).hexdigest() != data.get('digest'):
            data['error'] = 'native history rewritten or truncated; count unknown'
            raise ShipError(data['error'])
    if prior == source and data.get('transition'):
        raise ShipError('fresh transition claimed; successor not yet proved')
    data.update(source=source, count=reading.count, extent=reading.extent, digest=reading.digest,
                source_launch=key or data['source_launch'], disabled=False, reading_unknown=False)


def threshold(seat, data):
    runtime = seat.ship.runtime
    if data.get('request') is None:
        message = post.Store(runtime).prepare('order', post.SHIP, seat.name,
            'Compaction threshold reached. Post an open self-addressed handoff replying to this request: '
            'qd post order --to SELF --re REQUEST --body-file handoff.txt. Leave that handoff open. '
            'If status says ready for manual fresh, report that you left the handoff and let the outside '
            'controller choose the idle break. From outside the seat, qd fresh-ready SEAT --cancel '
            'releases the reservation, then explicit qd fresh SEAT carries the open handoff. '
            'For proved automatic readiness at a natural break, run qd fresh-ready --request '
            'REQUEST --handoff HANDOFF --at-break with the actual ids. This explicitly attests '
            'no review, signature or landing is half done and that this turn will finish without '
            'starting another task. Readiness alone does not fresh until that exact turn completes. '
            'Human-owned sessions remain manual-only; qd fresh-ready --cancel releases readiness '
            'and preserves all posts. Use qd fresh at the chosen moment.')
        data['request'] = message.to_json()
        data['protocol_requests'].append(message.id)
        data['intents'].append(message.to_json())
        data['reason'] = 'threshold reached; waiting for open handoff/readiness'
        save(runtime, seat.name, data)
    publish(runtime, data['request'])


def binding(seat, data, reading):
    return dict(generation=data['generation'], launch=launch_key(seat), source=data['source'],
                frontier=reading.frontier, turn=reading.turn, call=reading.last_call,
                incoming=incoming(seat.ship.runtime, seat.name, data['request']), config=config_key(seat),
                handoff=data.get('reserved'), handoff_digest=data.get('handoff_digest'))


def valid_binding(seat, data, reading, saved):
    return saved == binding(seat, data, reading)


def open_handoff(runtime, data):
    if not data.get('reserved'):
        return False
    store = post.Store(runtime)
    handoff = store.get(data['reserved'])
    return digest(handoff.to_json()) == data.get('handoff_digest') and store.is_open(handoff)


def prepare_ready(seat, request_ref, handoff_ref):
    """The explicit seat command publishes its own ordinary attestation; never arms by prose."""
    runtime = seat.ship.runtime
    with post.admission(runtime, seat.name):
        data = load(runtime, seat.name)
        if data is None or not data.get('request') or data.get('disabled') or not seat.spec.fresh_after_compactions:
            raise ShipError('no enabled compaction threshold request')
        if not live_identity(seat):
            raise ShipError('current live launch identity not proved')
        record, reading, launched = current(seat)
        observe(seat, data, record, reading, launched)
        if reading.count < seat.spec.fresh_after_compactions:
            raise ShipError('current conversation is below its compaction threshold')
        store = post.Store(runtime)
        request, handoff = store.get(request_ref), store.get(handoff_ref)
        if (request.to_json() != data['request'] or handoff.id != data.get('reserved')
                or digest(handoff.to_json()) != data.get('handoff_digest') or handoff.id in data['used']
                or not store.is_open(handoff)):
            raise ShipError('readiness needs the current reserved open self-handoff and request')
        state = reading.state
        if not state.turn_open or len(state.pending_tools or ()) != 1 or not reading.last_call:
            data['reason'] = 'execution unknown: readiness needs one proved current attestation tool in its recorded turn'
            if state.pending_tools is None:
                data['manual'] = 'ready for manual fresh: execution unknown; choose an idle break'
            save(runtime, seat.name, data)
            raise ShipError(data['reason'])
        bound = binding(seat, data, reading)
        intent = data.get('attestation')
        if intent is not None and intent['binding'] != bound:
            release(data, 'attestation stale; prepare a new handoff and renew readiness')
            save(runtime, seat.name, data)
            raise ShipError(data['reason'])
        if intent is None:
            report = store.prepare('report', seat.name, request.sender,
                f'Ready at a natural break for request {request.id}, open handoff {handoff.id}. '
                'I attest no review, signature or landing is half done; I will finish this turn '
                'without starting another task.', re=request.id)
            intent = dict(revision=secrets.token_hex(16), binding=bound, report=report.to_json())
            data['attestation'] = intent
            data['intents'].append(report.to_json())
            data['reason'] = 'attestation report prepared; readiness control unconfirmed'
            save(runtime, seat.name, data)
        report = publish(runtime, intent['report'])
        return dict(revision=intent['revision'], report=report.id, binding=bound)


def arm(seat, revision, report, binding):
    report_ref, bound = report, binding
    runtime = seat.ship.runtime
    with post.admission(runtime, seat.name):
        data = load(runtime, seat.name)
        if not data or not seat.spec.fresh_after_compactions:
            raise ShipError('compaction readiness disabled or unknown')
        if not live_identity(seat):
            raise ShipError('current live launch identity not proved')
        record, reading, launched = current(seat)
        observe(seat, data, record, reading, launched)
        intent = data.get('attestation')
        if (not intent or intent['revision'] != revision or intent['binding'] != bound
                or not valid_binding(seat, data, reading, bound)
                or not open_handoff(runtime, data)
                or post.Store(runtime).get(report_ref).to_json() != intent['report']):
            raise ShipError('readiness control is stale or does not name the exact seat attestation')
        from . import recovery
        from .door import Door
        state, screen = reading.state, Door(seat).screen()
        if (state.queue_error or state.queued or state.pending_tools is None or len(state.pending_tools) > 1
                or recovery.held(seat) or screen is None or screen.box != '' or screen.queued
                or screen.unknown or screen.interruption or screen.starting):
            raise ShipError('readiness cannot arm with queue/composer/tool/recovery uncertainty')
        data['ready'] = bound
        data['reason'] = 'manual-only readiness' if seat.session.human else 'ready; waiting for exact turn completion'
        save(runtime, seat.name, data)
        return data['reason']


def obstruction(seat, data, record, reading):
    from . import pending, recovery
    from .door import Door
    if seat.session.human:
        return 'human-owned session; manual fresh only'
    if not seat.spec.up or not live_identity(seat):
        return 'current live launch identity not proved; automatic fresh held'
    if not data.get('ready'):
        return data.get('reason') or 'waiting for explicit readiness'
    if not valid_binding(seat, data, reading, data['ready']):
        return 'readiness stale; prepare a new handoff and renew readiness'
    state = reading.state
    if not reading.complete or state.turn_open is not False or state.pending_tools != ():
        return 'waiting for exact recorded turn/tool completion'
    if not any(turn.timed and turn.started is not None and turn.ended is not None
               and state.last_input_at is not None and turn.started <= state.last_input_at <= turn.ended
               for turn in state.completed_turns[-1:]):
        return 'exact timed completion not proved'
    if state.queue_error or state.queued:
        return 'queue unknown or queued; automatic fresh held'
    if recovery.held(seat) or pending.active(seat):
        return 'recovery or delivery uncertainty; automatic fresh held'
    if lifecycle_pending(seat.ship.runtime, seat.name):
        return 'explicit lifecycle request pending or uncertain; automatic fresh held'
    attempts, store = post.Attempts(seat.ship.runtime), post.Store(seat.ship.runtime)
    latest = attempts.latest(seat.name)
    if any(value in (None, 'pending', 'blind', 'queued') for value in latest.values()):
        return 'unconfirmed or queued post receipt; automatic fresh held'
    request = decode(data['request'])
    if any(message.id != request.id for message in attempts.undelivered(store, seat.name)):
        return 'incoming work has not reached the seat; automatic fresh held'
    screen = Door(seat).screen()
    if (screen is None or screen.box != '' or screen.working is not False or screen.queued
            or screen.unknown or screen.interruption or screen.starting):
        return 'composer/queue not proved empty and idle'
    if not open_handoff(seat.ship.runtime, data):
        return 'handoff not open; automatic fresh held'
    return None


def lifecycle_pending(runtime, name):
    """Read only request routing metadata under the same lock as control publication."""
    from .servicectl import LIFECYCLE_REQUESTS
    folder = runtime / 'requests'
    for request in [*folder.glob('*.json'), *folder.glob('*.claimed')]:
        try:
            data = json.loads(request.read_text())
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            return True  # absence of a conflicting action is not proved
        if isinstance(data, dict) and data.get('seat') == name:
            if not isinstance(data.get('kind'), str) or data['kind'] in LIFECYCLE_REQUESTS:
                return True
    return False


def tick(seat):
    runtime = seat.ship.runtime
    if not seat.spec.fresh_after_compactions:
        if path(runtime, seat.name).exists():
            with post.admission(runtime, seat.name):
                disarm_locked(seat, 'compaction automation disabled', disabled=True)
        return
    try:
        record, reading, launched = current(seat)
        with post.admission(runtime, seat.name):
            data = load(runtime, seat.name) or new()
            if data.get('disabled') and seat.ship.file.is_file():
                from .ship import load as load_ship
                declared = load_ship(seat.ship.folder).seats.get(seat.name)
                if declared is None or not declared.fresh_after_compactions:
                    return  # a declaration edit won admission before this tick's older view
            observe(seat, data, record, reading, launched)
            recover(runtime, data)
            manual = None
            if reading.count >= seat.spec.fresh_after_compactions:
                threshold(seat, data)
                if reading.state.pending_tools is None:
                    manual = 'ready for manual fresh: execution unknown; choose an idle break'
                elif seat.session.human:
                    manual = 'ready for manual fresh: human-owned session; choose an idle break'
            else:
                data['reason'] = 'below compaction threshold'
            prepared = data.get('ready') or (data.get('attestation') or {}).get('binding')
            if prepared and not valid_binding(seat, data, reading, prepared):
                release(data, 'readiness stale; prepare a new handoff and renew readiness')
            data['manual'] = manual
            save(runtime, seat.name, data)
        if reading.count < seat.spec.fresh_after_compactions or manual or not data.get('ready'):
            return
        with seat._lifecycle(automatic=True):
            with post.admission(runtime, seat.name):
                data = load(runtime, seat.name)
                record, reading, launched = current(seat)
                observe(seat, data, record, reading, launched)
                reason = obstruction(seat, data, record, reading)
                if reason:
                    data['reason'] = reason
                    save(runtime, seat.name, data)
                    return
                latest_record, latest_reading, latest_launch = current(seat)
                if (latest_record, latest_reading.digest, latest_launch) != (record, reading.digest, launched):
                    data['reason'] = 'record changed during fresh check; waiting for stable proof'
                    save(runtime, seat.name, data)
                    return
                data['transition'] = dict(source=launch_key(seat), generation=data['generation'], handoff=data['reserved'])
                data['reason'] = 'fresh transition claimed; waiting for proved successor'
                save(runtime, seat.name, data)
            # Admission is released; later arrivals belong to the successor and are withheld.
            seat._down()
            seat._up(resume=False)
    except (OSError, ShipError) as error:
        with post.admission(runtime, seat.name):
            try:
                data = load(runtime, seat.name) or new()
            except ShipError:
                return  # unreadable/unsupported journal remains visibly unknown via status()
            data['reason'] = str(error)
            data['reading_unknown'] = True
            if data.get('source') and 'rewritten' in str(error):
                data['error'] = str(error)
            save(runtime, seat.name, data)


def status(seat, *, alerts_only=False):
    """Read the journal for detailed status or for actionable status-line entries."""
    threshold = seat.spec.fresh_after_compactions
    try:
        data = load(seat.ship.runtime, seat.name)
    except ShipError as error:
        return str(error)
    claimed = data is not None and data['transition'] is not None
    if not threshold:
        if claimed:
            return 'compaction automation disabled; transition already claimed'
        if alerts_only or (threshold is None and data is None):
            return None
        return 'compaction automation disabled'
    if data is None or data.get('error') or data.get('reading_unknown') or data.get('count') is None:
        reason = data['reason'] if data else 'awaiting current record'
        if claimed:
            label = 'fresh transition claimed; compaction count unknown'
            return label if alerts_only else f'{label}: {reason}'
        return f'compaction count unknown: {reason}'
    count = data['count']
    if claimed:
        detail = 'fresh transition claimed; waiting for proved successor'
    elif count >= threshold:
        detail = data.get('manual') or data['reason']
    elif alerts_only:
        return None
    else:
        detail = 'below compaction threshold'
    return f'compactions {count}/{threshold}: {detail}'
