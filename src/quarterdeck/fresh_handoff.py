"""One crew handoff and fresh start for a launch that needs a new conversation."""

from dataclasses import dataclass
import json
import re

from . import compaction, idle, pending, post, post_dir, recovery, relaunch_intent
from .harness import resume_target
from .ship import ShipError


SUBJECT = 'Prepare a conversation handoff'
LIMIT = 16 * 1024
REPORT = re.compile(r'Handoff: (ord-[A-Za-z0-9-]+)')


@dataclass(frozen=True)
class FreshHandoffResult:
    disposition: str
    proof: str


def folder(runtime):
    return runtime / 'post' / 'fresh-handoff'


def path(runtime, name):
    return folder(runtime) / (name + '.json')


def load(runtime, name):
    try:
        data = json.loads(post_dir.read_bytes(path(runtime, name), limit=LIMIT))
    except FileNotFoundError:
        return None
    except (ValueError, OSError) as error:
        raise ShipError(f'{name}: conversation handoff state unreadable') from error
    if (not isinstance(data, dict) or data.get('version') != 1 or data.get('seat') != name
            or data.get('phase') not in ('waiting', 'starting', 'done', 'superseded', 'failed')
            or not isinstance(data.get('callers'), list) or not all(isinstance(c, str) for c in data['callers'])
            or not isinstance(data.get('reason'), str) or type(data.get('restart')) is not bool
            or data['phase'] == 'waiting' and not isinstance(data.get('order'), str)):
        raise ShipError(f'{name}: conversation handoff state invalid')
    if data.get('order') is not None:
        order = compaction.decode(data['order'])
        if (order.kind, order.sender, order.to, order.subject, order.re) != ('order', post.SHIP, name, SUBJECT, None):
            raise ShipError(f'{name}: conversation handoff order invalid')
    return data


def save(runtime, name, data):
    post_dir.ensure_directory(folder(runtime))
    post_dir.replace_bytes(path(runtime, name), (json.dumps(data) + '\n').encode())


def active(runtime, name):
    data = load(runtime, name)
    return data if data is not None and data['phase'] in ('waiting', 'starting') else None


def blocks_delivery(runtime, message):
    data = active(runtime, message.to)
    if data is None:
        return False
    if data['phase'] == 'starting':
        return True  # later arrivals are carried by the successor
    return (message.kind == 'order' and message.sender == message.to
            and message.re == compaction.decode(data['order']).id)


def _publish_order(store, data):
    order = compaction.decode(data['order'])
    target = store.dir / (order.id + '.json')
    if post_dir.exists(target):
        if post_dir.read_bytes(target, limit=LIMIT) != order.to_json().encode():
            raise ShipError('conversation handoff order differs from its saved publication')
    else:
        compaction.admitting(store.dir.parent.parent, order)
        store._write(order)
    return order


def _current_record(seat):
    record = seat.lookup_record()
    return str(record) if record is not None else None


def _caller(seat, caller):
    operator = seat.ship.operator.lower()
    return caller if caller in seat.ship.seats or caller == operator else operator if caller else None


def _unavailable(seat, launched, pane):
    if launched is None:
        return seat.retiring() is None
    if pane is not None:
        return pane.dead
    from .server_recovery import pid_dead
    return pid_dead(launched.pid) is True


def request(seat, *, reason, caller=None, expected_launch=None, service=None, restart=False,
            pool=None, enclosed=False, immediate=None):
    """Stage once per live launch, or start immediately when this program cannot take a turn.

    ``pool`` is a destination the caller already chose. ``immediate`` defaults to that choice being set, which is
    how a walled program starts at once: it cannot write its handoff. A caller whose current pool is still open
    passes ``immediate=False`` so the same handoff is staged, bound to ``pool``, and started at the idle break
    after the report. A later request for the same launch does not start another. ``enclosed`` means the caller
    already holds this seat's relaunch section.
    """
    runtime, name = seat.ship.runtime, seat.name
    reason = reason.rstrip('.')
    if seat.session.human:
        return FreshHandoffResult('held', str(resume_target.Unproved(name, reason)))
    if pool is not None and pool not in seat.spec.pools:
        return FreshHandoffResult('held', f'{name}: {pool} is not one of its declared choices.')
    if immediate is None:
        immediate = pool is not None
    store = service.store if service is not None else post.Store(runtime)
    try:
        with seat._lifecycle(automatic=True):
            launched = seat.launched()
            launch = compaction.key_for(launched)
            if (restart or expected_launch is not None) and compaction.key_for(expected_launch) != launch:
                return FreshHandoffResult('changed-launch', f'{name}: the launch changed before its handoff was requested.')
            held = recovery.held(seat) or ('the seat is declared down' if not seat.spec.up and not restart else None)
            if held:
                return FreshHandoffResult('held', f'{name}: the handoff is held because {held}.')
            pane = seat._lifecycle_pane(launched)
            stopped = _unavailable(seat, launched, pane)
            if stopped and not restart and not immediate:
                return FreshHandoffResult('held', f'{name}: the observed launch is no longer running.')
            if not stopped and not immediate and (pane is None or pane.dead):
                return FreshHandoffResult('held', f'{name}: its program or pane could not be identified. The restart waits.')
            with post.admission(runtime, name):
                data = load(runtime, name)
                current = data is not None and data.get('launch') == launch
                if current and data['phase'] != 'waiting':
                    return FreshHandoffResult('held', f'{name}: a handoff for this launch was already handled.')
                if not current:
                    data = dict(version=1, seat=name, launch=launch, record=_current_record(seat),
                                phase='waiting', reason=reason, callers=[], order=None, restart=False)
                data['restart'] = data['restart'] or restart
                who = _caller(seat, caller)
                if who and who not in data['callers']:
                    data['callers'].append(who)
                if not stopped and not immediate:
                    if not current:
                        body = (
                            f'{reason}. Update the notes needed to continue your work. '
                            f'Post an open order to yourself replying to this order with your handoff '
                            f'and subject "Conversation handoff". Leave the handoff open for the next conversation. '
                            'When nothing is half done, report on this order to ship with this one line: '
                            'Handoff: HANDOFF_ID. Use the ID of the self-addressed order you just posted. '
                            'End your turn without starting another task.'
                        )
                        data['order'] = store.prepare('order', post.SHIP, name, body, subject=SUBJECT).to_json()
                    if pool is not None:
                        data['pool'] = pool
                    save(runtime, name, data)  # publish the intent before the immutable post
                    order = _publish_order(store, data)
                    disposition = 'already-staged' if current else 'staged'
                    return FreshHandoffResult(disposition, f'{name}: {reason}. The handoff order {order.short} '
                                              'waits for its report and an idle break before starting a new conversation.')
                data['phase'] = 'starting'
                if pool is not None:
                    data['pool'] = pool
                    for spec in seat.ship.seats.values():
                        if spec.role == 'systems' and spec.name != name and spec.name not in data['callers']:
                            data['callers'].append(spec.name)
                save(runtime, name, data)
            return FreshHandoffResult('started', _start(service, seat, store, data, unable=not immediate,
                                                        pool=pool, enclosed=enclosed))
    except (OSError, ShipError) as error:
        return FreshHandoffResult('held', f'{name}: the conversation handoff is held. {error}')


def _reported_handoff(store, order):
    for report in store.replies(order):
        matched = REPORT.fullmatch(report.body.strip())
        if report.kind != 'report' or report.sender != order.to or report.to != post.SHIP or matched is None:
            continue
        try:
            handoff = store.get(matched[1])
        except ShipError:
            continue
        if ((handoff.kind, handoff.sender, handoff.to, handoff.re)
                == ('order', order.to, order.to, order.id)
                and order.at <= handoff.at <= report.at and store.is_open(handoff)):
            return report, handoff
    return None


def _notice(store, seat, data, text):
    for caller in dict.fromkeys([seat.name, *data['callers']]):
        store.new('report', post.SHIP, caller, text)


def _settle_superseded(store, seat, data):
    """Close the handoff order this launch carried, only once its replacement has started.

    A start that raises leaves the order open. Any other open order, including the seat's own handoff, stays.
    """
    raw = data.get('order')
    if not isinstance(raw, str):
        return
    try:
        order = compaction.decode(raw)
        if (order.kind, order.sender, order.to, order.subject) != ('order', post.SHIP, seat.name, SUBJECT):
            return
        if not store.is_open(order):
            return
        store.new('report', seat.name, post.SHIP,
                  f'{seat.name}: the handoff was superseded by its new conversation.', re=order.id)
    except (OSError, ShipError):
        return


def _start(service, seat, store, data, *, unable=False, pool=None, enclosed=False):
    runtime, name = seat.ship.runtime, seat.name
    explanation = data['reason']
    if unable:
        explanation += ' and its old program could no longer run'
    note = f'{name} {resume_target.NEW_CONVERSATION} because {explanation}.'

    def begin():
        try:
            proof = seat._fresh_on_pool(pool) if pool is not None else seat._fresh()
        except (OSError, ShipError) as error:
            data['phase'] = 'failed'
            save(runtime, name, data)
            _notice(store, seat, data, f'{name}: its new conversation could not start. {error}')
            raise
        _settle_superseded(store, seat, data)  # the order was open while this launch carried it
        data['phase'] = 'done'
        save(runtime, name, data)
        _notice(store, seat, data, note)
        if service is not None:
            service.log('fresh-handoff', seat=name, proof=note)
        return proof

    if enclosed:
        proof = begin()
    else:
        with relaunch_intent.section(service, seat, 'fresh-handoff', resume=False):
            proof = begin()
    return f'{proof}\n{note}'


def _interrupted(service, seat, data):
    name, runtime = seat.name, seat.ship.runtime
    explanation = f'{name}: the service stopped during its fresh start and could not confirm the outcome. Inspect qd watch {name}.'
    data.update(phase='failed', problem=explanation, failed_launch=compaction.launch_key(seat))
    save(runtime, name, data)
    _notice(service.store, seat, data, explanation)
    raise ShipError(explanation)


def tick(service, seat):
    runtime, name = seat.ship.runtime, seat.name
    data = None if seat.session.human else load(runtime, name)
    if data is None:
        return
    if data['phase'] == 'failed' and 'problem' in data and data['failed_launch'] == compaction.launch_key(seat):
        raise ShipError(data['problem'])  # the status line keeps the hold until another launch establishes a new outcome
    if data['phase'] not in ('waiting', 'starting'):
        return
    with seat._lifecycle(automatic=True):
        data = active(runtime, name)
        if data is None:
            return
        if data['phase'] == 'starting':
            _interrupted(service, seat, data)
        with post.admission(runtime, name):
            if data['launch'] != compaction.launch_key(seat):
                data['phase'] = 'superseded'
                save(runtime, name, data)
                return
            if not seat.spec.up or recovery.held(seat) or compaction.lifecycle_pending(runtime, name):
                return
            order = _publish_order(service.store, data)
            result = _reported_handoff(service.store, order)
            launched = seat.launched()
            pane = seat._lifecycle_pane(launched)
            unable = _unavailable(seat, launched, pane)
            if unable and not data['restart']:
                return  # a forecast never reopens a stopped seat
            if not unable:
                if result is None or pane is None or pending.active(seat):
                    return
                report, handoff = result
                if data['record'] is not None and _current_record(seat) != data['record']:
                    return
                state, hold = idle.reading(seat)
                if hold is not None or service.handoff_quiet(seat, state, hold) is not None:
                    return
                if state.completed_turns[-1].ended < report.at:
                    return  # the turn in which the seat reported has not yet proved its end
                if any(message.id != handoff.id for message in service.attempts.undelivered(service.store, name)):
                    return
                if data['launch'] != compaction.launch_key(seat):
                    return
            data['phase'] = 'starting'
            save(runtime, name, data)  # claim under the same admission used by input delivery and post
        _start(service, seat, service.store, data, unable=unable, pool=data.get('pool'))
