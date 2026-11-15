"""One fixed human-seat wake through Door, with durable once-per-watch evidence.

The waiting post is never transported or receipted here. Only the fixed wake has
an input receipt; the ordinary inbox watch still delivers the actual messages.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from . import trusted_files
from . import durable, input_receipt, pending, post, recovery
from .harness.base import record_fingerprint
from .turn_failure import metadata
from .ship import ShipError
from .tmux import TmuxError

LINE = 'qd > ship: messages are waiting. Run qd inbox --wait now to read them.'
WAITING = 'human wake confirmed; waiting for watch registration or ended turn'
BOX = 'human wake box not empty; untouched'
GENERATION_UNKNOWN = 'human wake held: watch rearm generation unknown'
FAILURE_REASONS = frozenset(
    f'human wake service failure ({phase}: {kind})'
    for phase in ('ownership', 'pass', 'alarm', 'observation')
    for kind in ('I/O', 'metadata', 'ownership', 'operation'))
NO_SCREEN = object()
PHASES = {'prepared', 'pasting', 'submitting', 'submitted', 'queued', 'delivered', 'cancelled'}


def load(seat) -> pending.Proof | None:
    path = seat.trusted / trusted_files.HUMAN_WAKE
    try:
        data = json.loads(metadata(path))
    except FileNotFoundError:
        return None
    validate(data, seat.name)
    return pending.Proof(path, data)


def validate(data, seat: str) -> None:
    """Validate the existing fixed-wake reservation without changing it."""
    identity = data['identity']
    if (type(data.get('format')) is not int or data['format'] != 1 or data['seat'] != seat or data['payload'] != LINE
            or data['phase'] not in PHASES or data['attempts'] != []
            or type(data.get('ended', False)) is not bool
            or not isinstance(identity['pane'], str) or type(identity['pid']) is not int
            or identity['pid'] <= 0 or not isinstance(identity['harness'], str)
            or not isinstance(data['generation'], (str, type(None)))
            or data['generation'] is not None and (len(data['generation']) != 32
                or any(c not in '0123456789abcdef' for c in data['generation']))
            or not isinstance(data['record'], (str, type(None)))
            or not isinstance(data['shown'], (str, type(None)))):
        raise ValueError('invalid human wake reservation')
    for stamp in (data['at'], data['since'], identity['launch']):
        if datetime.fromisoformat(stamp).tzinfo is None:
            raise ValueError('untimed human wake reservation')


def rearmed(proof, watch) -> bool:
    """Only a known new marker proves registration; absence never does."""
    return watch.generation is not None and watch.generation != proof.data['generation']


def episode(proof, watch) -> str | None:
    """Read-only episode state; only a receipt plus a later arm releases a wake."""
    if proof is None or proof.data['phase'] in ('prepared', 'cancelled'):
        return None
    if watch.generation is None and proof.data['generation'] is not None:
        return GENERATION_UNKNOWN
    if proof.data['phase'] == 'queued' or proof.data.get('screen_queue'):
        return 'human wake queued; waiting for record consumption'
    if proof.data['phase'] != 'delivered':
        return 'human wake pending; waiting for owned-record receipt'
    if not rearmed(proof, watch):
        if watch.armed:
            return 'human wake confirmed; watch rearm generation unknown'
        return ('human wake confirmed but watch not armed; person must intervene'
                if proof.data.get('ended') else WAITING)
    return None


def changed_launch(seat, proof) -> bool:
    """A new saved launch releases an episode; a missing/replaced pane alone does not."""
    launch = seat.launched()
    if launch is None:
        return False
    old = proof.data['identity']
    return (launch.pane, launch.pid, launch.at) != (old['pane'], old['pid'], old['launch'])


def release_launch(seat, proof) -> None:
    raw = metadata(proof.path)
    archive = seat.trusted / trusted_files.HUMAN_WAKE_HISTORY / (hashlib.sha256(raw.encode()).hexdigest() + '.json')
    archive.parent.mkdir(parents=True, exist_ok=True)
    try:
        retained = metadata(archive)
    except FileNotFoundError:
        pass
    else:
        if retained != raw:
            raise ValueError('wake history identity conflict')
    durable.replace_file(archive, raw, encoding='utf-8')
    proof.save(phase='cancelled', resolution='new launch released prior wake episode; original retained in human-wake-history')


def record_stamp(seat) -> dict:
    """Operational pointer and file metadata only, never conversation bytes."""
    try:
        name = metadata(seat.trusted / trusted_files.RECORD).strip()
    except FileNotFoundError:
        return {'record': None, 'fingerprint': None}
    if not name:
        raise ValueError('empty record pointer')
    from pathlib import Path
    return {'record': name, 'fingerprint': json.loads(json.dumps(record_fingerprint(Path(name))))}


def service_failure_reason(phase, error) -> str:
    """Fixed local categories only: never exception text, values or traceback."""
    if phase not in ('ownership', 'pass', 'alarm', 'observation'):
        phase = 'pass'
    kind = ('I/O' if isinstance(error, OSError) else
            'ownership' if isinstance(error, (ShipError, TmuxError)) else
            'metadata' if isinstance(error, (ValueError, TypeError, KeyError)) else 'operation')
    return f'human wake service failure ({phase}: {kind})'


def failure_status(seat) -> str | None:
    try:
        data = json.loads(metadata(seat.trusted / trusted_files.HUMAN_WAKE_STATUS))
        if data.get('failure') is not True:
            return None
        reason = data.get('failure_reason', data.get('reason'))
        if data.get('format') != 1 or reason not in FAILURE_REASONS:
            return 'human wake evidence unreadable; service failure observation invalid'
        saved = data.get('failure_identity', data['identity'])
        if saved is None:
            return reason + '; launch identity unknown'
        launch = seat.launched()
        if launch is None:
            return None
        if (saved['pane'], saved['pid'], saved['launch'], saved['harness']) != (
                launch.pane, launch.pid, launch.at, seat.harness.name):
            return None  # historical local failure is not current launch evidence
        return reason
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ShipError, TmuxError):
        return 'human wake evidence unreadable; service failure observation unknown'


def observe_failure(seat, reason) -> None:
    if reason not in FAILURE_REASONS:
        raise ValueError('unknown wake failure category')
    try:
        launch = seat.launched()
        if launch is None:
            return  # proved down seats get no wake observation, including failure notes
        pane = seat.pane()
        if (pane is not None and pane.id == launch.pane and pane.pid == launch.pid
                and pane.dead and seat.launched() == launch):
            return
        identity = dict(pane=launch.pane, pid=launch.pid, launch=launch.at, harness=seat.harness.name)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ShipError, TmuxError):
        identity = None
    try:
        source = record_stamp(seat)
    except (OSError, ValueError, KeyError, TypeError):
        source = None
    observe(seat, reason, identity, source, failure=True)


def observed_action(seat) -> str | None:
    """Current actionable observation, including a missing reservation receipt."""
    try:
        data = json.loads(metadata(seat.trusted / trusted_files.HUMAN_WAKE_STATUS))
        reason = data['reason']
        if (data['format'] == 1 and isinstance(reason, str) and len(reason) <= 1024
                and data['identity'] == pending.identity(seat) and data['source'] == record_stamp(seat)
                and action_required(seat, reason)):
            return reason
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        return 'human wake evidence unreadable; no waking input'
    return None


@post.reading_pass()
def status(seat) -> str | None:
    try:
        launch = seat.launched()
        pane = seat.pane()
        if seat.launched() != launch:
            return failure_status(seat) or 'human wake up state unknown; launch changed during reading'
        if launch is None:
            return None  # no saved launch is a proved down seat
        if pane is None or pane.id != launch.pane or pane.pid != launch.pid:
            return failure_status(seat) or 'human wake up state unknown; owned pane unavailable'
        if pane.dead:
            return None  # positively owned dead pane, never a missing/foreign one
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ShipError, TmuxError):
        return failure_status(seat) or 'human wake up state unknown; ownership reading failed'
    failure = failure_status(seat)
    watch = post.watch_reading(seat.ship.runtime, seat.name)
    if watch.armed is None:
        return watch.reason if action_required(seat, watch.reason) else failure or watch.reason
    try:
        proof = load(seat)
        if proof is not None and proof.data['phase'] != 'cancelled' and changed_launch(seat, proof):
            try:
                observed = json.loads(metadata(seat.trusted / trusted_files.HUMAN_WAKE_STATUS))
            except FileNotFoundError:
                observed = None
            if (observed and observed['identity'] == pending.identity(seat)
                    and observed['source'] == record_stamp(seat)
                    and isinstance(observed['reason'], str)
                    and observed['reason'].startswith('human wake evidence unreadable')):
                return observed['reason']
            return failure or 'human wake launch changed; awaiting service observation'
        current_episode = episode(proof, watch)
        if action_required(seat, current_episode):
            return current_episode
        if reason := observed_action(seat):
            return reason
        if current_episode:
            return failure or current_episode
        waiting = post.Attempts(seat.ship.runtime).undelivered(post.Store(seat.ship.runtime), seat.name)
        if watch.armed or not waiting:
            return failure
        try:
            data = json.loads(metadata(seat.trusted / trusted_files.HUMAN_WAKE_STATUS))
        except FileNotFoundError:
            return failure or 'human wake awaiting service observation'
        if (data['format'] != 1 or not isinstance(data['reason'], (str, type(None)))
                or data['reason'] is not None and len(data['reason']) > 1024):
            raise ValueError('invalid wake observation')
        if data['identity'] != pending.identity(seat) or data['source'] != record_stamp(seat):
            return failure or 'human wake source changed; awaiting service observation'
        reason = data['reason']
        return reason if action_required(seat, reason) else failure or reason
    except (OSError, ValueError, KeyError, TypeError):
        return 'human wake evidence unreadable; no waking input'


def observe(seat, reason, identity, source, *, failure=False, successful=True) -> None:
    path = seat.trusted / trusted_files.HUMAN_WAKE_STATUS
    try:
        previous = metadata(path)
    except FileNotFoundError:
        previous = None
    data = dict(format=1, identity=identity, source=source, reason=reason)
    prior = json.loads(previous) if previous is not None else {}
    if failure:
        if prior.get('format') == 1:
            # Preserve only existing operational fields, never arbitrary extra data.
            data = dict(format=1, identity=prior.get('identity'), source=prior.get('source'),
                        reason=prior.get('reason'))
        data.update(failure=True, failure_identity=identity, failure_reason=reason)
    elif prior.get('failure') is True:
        failed_identity = prior.get('failure_identity', prior.get('identity'))
        if not successful or failed_identity != identity:
            data.update(failure=True, failure_identity=failed_identity,
                        failure_reason=prior.get('failure_reason', prior.get('reason')))
    text = json.dumps(data, sort_keys=True) + '\n'
    if text != previous:
        path.parent.mkdir(parents=True, exist_ok=True)
        durable.replace_file(path, text)


def action_required(seat, reason) -> bool:
    """Only terminal intervention states generate immutable reports, never waits."""
    if not reason:
        return False
    if reason.startswith(('human wake confirmed but watch not armed',
                          'human wake receipt uncertain', 'human wake reservation unreadable',
                          'human wake evidence unreadable', 'inbox watch generation unreadable',
                          'inbox watch evidence unreadable', 'inbox watch retirement unconfirmed')):
        return True
    return False


def prior_receipt(seat, record, since, proof=None) -> str | None:
    """An old identical input must not qualify as this fixed line's fresh receipt."""
    if reason := candidate_hold(seat, proof):
        return reason
    if record is None:
        return None
    try:
        stamp = record_fingerprint(record)
        if seat.record() != record:
            return 'human wake prior receipt ownership unknown; no waking input'
        inputs = seat.harness.inputs(record)
        diagnostics = getattr(inputs, 'diagnostics', None)
        if diagnostics is not None and diagnostics()['total']:
            return 'human wake prior receipt parse incomplete; no waking input'
        result = input_receipt.confirmed(seat.harness, record, LINE, since)
        if record_fingerprint(record) != stamp or seat.record() != record:
            return 'human wake prior receipt source changed; no waking input'
        if result is not None:
            return 'human wake receipt-time boundary ambiguous; no waking input'
    except (OSError, ValueError, KeyError, TypeError):
        return 'human wake prior receipt unknown; no waking input'
    return None


@post.reading_pass()
def metadata_context(seat):
    """No native state/input/capture is allowed before every shared hold passes."""
    watch = post.watch_reading(seat.ship.runtime, seat.name)
    waiting = bool(post.Attempts(seat.ship.runtime).undelivered(post.Store(seat.ship.runtime), seat.name))
    if watch.armed is None:
        return watch, waiting, None, watch.reason
    if recovery.held(seat):
        return watch, waiting, None, 'human wake recovery held; person must intervene'
    if pending.detached(seat):
        return watch, waiting, None, 'prior launch has an unconfirmed delivery; human wake held'
    try:
        saved = load(seat)
    except (OSError, ValueError, KeyError, TypeError):
        return watch, waiting, None, 'human wake reservation unreadable; never resent'
    if (saved is not None and saved.data['phase'] not in ('prepared', 'cancelled')
            and not changed_launch(seat, saved) and saved.data['generation'] is not None
            and watch.generation is None):
        return watch, waiting, saved, GENERATION_UNKNOWN
    if pending.held(seat, None):
        return watch, waiting, saved, 'human wake pending metadata hold; no native reads'
    return watch, waiting, saved, None


def candidate_hold(seat, proof=None) -> str | None:
    watch, waiting, saved, reason = metadata_context(seat)
    if reason:
        return reason
    if watch.armed is not False:
        return 'inbox watch is armed; its watch delivers'
    if not waiting:
        return 'no waiting message; no waking input'
    if proof is not None and watch.generation != proof.data['generation']:
        if watch.generation is None:
            return GENERATION_UNKNOWN
        return 'inbox watch rearmed during wake; no new input authorized'
    if saved is not None and saved.data['phase'] != 'cancelled':
        own_prepared = (proof is not None and saved.path == proof.path and saved.data == proof.data
                        and saved.data['phase'] == 'prepared')
        if not own_prepared:
            return episode(saved, watch) or 'human wake reservation standing; no new eligibility'
    return None


def screen_hold(screen) -> str | None:
    if (screen is None or screen.box is None or screen.unknown or screen.interruption
            or screen.starting):
        return 'human wake box unknown or obstructed; untouched'
    if screen.box != '':
        return BOX
    if screen.working is not False or screen.queued:
        return 'human wake waiting for screen work to end'
    return None


def width_hold(harness, pane) -> str | None:
    """Use only the adapter's declared margin and this captured pane's width."""
    width = getattr(pane, 'width', None)
    if type(width) is not int or width <= 0:
        return 'human wake held: pane width unknown for the waking line'
    declaration = getattr(harness, 'human_wake_box_margin', None)
    margin = declaration(width) if callable(declaration) else None
    if type(margin) is not int or margin < 0:
        return 'human wake held: adapter box margin unknown for the waking line'
    needed = len(LINE) + margin
    if width < needed:
        return f'human wake held: pane too narrow for the waking line ({needed} columns needed)'
    return None


def eligible(seat, screen=NO_SCREEN, proof=None) -> str | None:
    """Metadata holds, owned identity, record proof, then the supplied screen last."""
    if reason := candidate_hold(seat, proof):
        return reason
    launch, pane = seat.launched(), seat.pane()
    if launch is None or pane is None or pane.dead or pane.in_mode:
        return 'human wake launch/pane identity unavailable or pane in copy mode'
    joined = seat.join()
    if joined.record is None:
        if joined.reason != 'no record yet':
            return 'human wake owned record unknown; no waking input'
    else:
        try:
            stamp = record_fingerprint(joined.record)
            state = seat.harness.state(joined.record)
            if record_fingerprint(joined.record) != stamp:
                return 'human wake held: record activity unknown (turn, tools, queue); record changed'
        except (OSError, ValueError, TypeError):
            return 'human wake held: record activity unknown (turn, tools, queue); record unreadable'
        unknown = []
        if type(state.turn_open) is not bool:
            unknown.append('turn')
        if not isinstance(state.pending_tools, tuple):
            unknown.append('tools')
        if not isinstance(state.queued, tuple) or state.queue_error is not None:
            unknown.append('queue')
        if unknown:
            return 'human wake held: record activity unknown (' + ', '.join(unknown) + ')'
        if state.turn_open is not False or state.pending_tools != () or state.queued != ():
            return 'human wake waiting for recorded work to end'
        if state.failure:
            from .auth_failure import wakeable
            if not wakeable(seat, state):  # a single sign-in failure while the pool's sign-in reads valid alone wakes
                return 'human wake recorded turn error; person must intervene'
        if proof is not None:
            if reason := prior_receipt(seat, joined.record, datetime.fromisoformat(proof.data['since']), proof):
                return reason
    return None if screen is NO_SCREEN else screen_hold(screen)


def authorized(seat, proof, text) -> bool:
    """Only the persisted fixed reservation can enter the human exception."""
    if not seat.session.human or proof is None or text != LINE:
        return False
    current = load(seat)
    return (current is not None and current.path == proof.path and current.data == proof.data
            and proof.data['phase'] == 'prepared' and proof.same_launch(seat))


@post.reader_scope()
def run(seat, *, observe_delivery=None) -> str | None:
    """Called by the serialized service (or its throwaway check), never by a sender."""
    if not seat.session.human:
        raise ShipError('human wake requires a human-owned session')
    if not seat.is_up:
        return None  # no pass or observation; failed ownership reads still raise
    with post.admission(seat.ship.runtime, seat.name):
        if not seat.is_up:
            return None
        before, identity = record_stamp(seat), pending.identity(seat)
        reason = (_run(seat) if observe_delivery is None
                  else _run(seat, observe_delivery=observe_delivery))
        after, current = record_stamp(seat), pending.identity(seat)
        fresh = before == after and identity == current
        if not fresh:
            reason = 'human wake source changed; awaiting service observation'
        if seat.is_up:
            observe(seat, reason, current, after, successful=fresh)
        return reason


def _run(seat, *, observe_delivery=None) -> str | None:
    from pathlib import Path
    from .door import Door

    watch, waiting, proof, reason = metadata_context(seat)
    if reason:
        return reason
    if proof is not None and proof.data['phase'] != 'cancelled' and changed_launch(seat, proof):
        try:
            release_launch(seat, proof)
        except (OSError, ValueError, KeyError, TypeError):
            return 'human wake evidence unreadable; old launch reservation retained'
    if proof is not None and proof.data['phase'] == 'prepared':
        try:
            proof.save(phase='cancelled', resolution='prepared wake was provably unsent')
        except OSError:
            return 'human wake evidence unreadable; unsent cancellation not persisted'
    if proof is not None and proof.data['phase'] == 'delivered' and rearmed(proof, watch):
        proof.save(phase='cancelled', resolution='confirmed wake rearmed; watch episode complete')
    if proof is not None and proof.data['phase'] in ('pasting', 'submitting', 'submitted', 'queued'):
        # Bound settlement exception: original Proof only, no screen
        # or new candidate. Shared metadata holds have all passed above.
        receipt = proof.confirmed(seat)
        if receipt not in ('delivered', 'queued'):
            return 'human wake receipt uncertain; never resent; inspect human-wake.json'
        proof.finish(receipt)
        return episode(proof, watch)
    if proof is not None and proof.data['phase'] == 'delivered':
        if not proof.data.get('ended'):
            record = proof.data['record']
            if (not record or not proof.same_launch(seat)
                    or proof.data['identity']['harness'] != seat.harness.name
                    or record_stamp(seat)['record'] != record):
                return 'human wake held: record activity unknown (turn); pinned wake identity unknown'
            try:
                before = record_fingerprint(Path(record))
                state = seat.harness.state(Path(record))
                if before != record_fingerprint(Path(record)) or type(state.turn_open) is not bool:
                    return 'human wake held: record activity unknown (turn)'
            except (OSError, ValueError, TypeError):
                return 'human wake held: record activity unknown (turn); pinned wake record unreadable'
            if state.turn_open is False:
                proof.save(ended=True)
        return episode(proof, watch)
    if watch.armed or not waiting:
        return None
    if reason := eligible(seat):
        return reason
    identity = pending.identity(seat)
    record = seat.record()
    since = post.now()
    if reason := prior_receipt(seat, record, since):
        return reason
    door = Door(seat)
    screen = door.screen()
    if reason := screen_hold(screen):
        return reason
    reading = door._reading(screen)
    if not reading.open:
        return 'human wake ordinary door obstructed; no waking input'
    if reason := width_hold(seat.harness, door.captured_pane):
        return reason
    at = since.isoformat()
    proof = pending.Proof(seat.trusted / trusted_files.HUMAN_WAKE, {})
    proof.save(format=1, seat=seat.name, identity=identity, at=at, since=at,
               payload=LINE, phase='prepared', record=str(record) if record else None,
               shown=None, attempts=[], generation=watch.generation, ended=False)
    # Keep admission through submission: watch registration and ordinary post
    # transitions cannot race this reservation. Door still verifies each input.
    delivered = door.deliver(LINE, proof=proof, human_wake=True)
    proof.finish(delivered.result)
    if observe_delivery is not None:
        observe_delivery(delivered, door.after_paste.box if door.after_paste is not None else None)
    if proof.data['phase'] == 'prepared':
        proof.save(phase='cancelled', resolution='pre-paste wake outcome was provably unsent')
    if proof.data['phase'] == 'cancelled':
        if delivered.detail.startswith(('human wake held: pane ', 'human wake held: adapter box margin ')):
            return delivered.detail
        return 'human wake unsent; waiting for fresh eligibility'
    return None if delivered.result == 'delivered' else f'human wake {delivered.result}; inspect retained reservation'
