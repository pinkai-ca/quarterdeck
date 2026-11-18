"""Recorded sign-in failures: the next delivery first, then bounded guarded resumes per failed episode.

While the pool's sign-in is usable or can renew on use, a crew seat whose latest turn alone failed at sign-in takes its next
message through the door as usual: the measured program reads its sign-in file again for a new request (its
refused-request handler drops its cached sign-in and reads the store again, and a changed file is adopted by
its modification time). The failed turn's input stays in the conversation it sends. Only a second sign-in
failure in a row, or the renewal window passing with no message, holds the door and brings the guarded
resume, and only a resume that does not cure it needs attention.

Native syntax belongs to the adapter. Credential values never enter this module's
journal: it retains only stable file generations and consumed attempts' times.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import stat

from . import credential, durable, post_dir, turn_failure
from .harness.types import AUTHENTICATION
from .ship import ShipError

MAX_ATTEMPTS = 3
RETRY_SECONDS = 60
WINDOW_SECONDS = 15 * 60
WAITING = 'sign-in: waiting for the renewed pass'
DELIVERING = 'pool sign-in valid; its next message is delivered'
HUMAN_WAKING = 'pool sign-in valid; the waking line goes once'
RENEWABLE = 'pool access can renew on use'
RENEWABLE_DELIVERY = RENEWABLE + '; its next message is delivered'
RENEWABLE_WAKE = RENEWABLE + '; the waking line goes once'
USABLE = ('valid', 'renewable')


def failed_in_a_row(state) -> int:
    """How many ended turns in a row, newest first, failed at sign-in."""
    count = 0
    for turn in reversed(tuple(getattr(state, 'completed_turns', None) or ())):
        failure = getattr(turn, 'failure', None)
        if failure is None or failure.kind != AUTHENTICATION:
            break
        count += 1
    return count


def first_failure(seat, state=None, now=None, *, observation=None) -> bool:
    """A crew seat whose latest turn alone failed at sign-in, within the renewal window, while its pool's sign-in
    is usable or can renew on use: the door delivers its next message as usual instead of holding it. The failed turn's own input
    stays in the conversation the program sends with that request. Past the window, with no message having come,
    the guarded resume decides as before, so nothing waits silently."""
    if seat.session.human or not seat.is_up:
        return False
    now = now or datetime.now(timezone.utc)
    try:
        if state is None:
            record = seat.record()
            state = seat.harness.state(record) if record is not None else None
    except OSError:
        return False
    failure = getattr(state, 'failure', None)
    if failure is None or failure.kind != AUTHENTICATION or failed_in_a_row(state) != 1:
        return False
    at = failure.at
    if at is None or at.tzinfo is None or not 0 <= (now - at).total_seconds() < WINDOW_SECONDS:
        return False
    return (observation or pool_observation(seat, now))[0] in USABLE


def wakeable(seat, state, now=None) -> bool:
    """For the waking line, from the record the wake already reads: the latest turn alone failed at sign-in inside
    the program while the pool's sign-in is usable or can renew on use. Any other failure, or a second in a row, keeps the hold."""
    failure = getattr(state, 'failure', None)
    return (failure is not None and failure.kind == AUTHENTICATION and failed_in_a_row(state) == 1
            and pool_observation(seat, now)[0] in USABLE)


def human_wakeable(seat, now=None, *, observation=None) -> bool:
    """A seat in the person's session whose latest failure, by the service's published metadata and never its
    conversation, is a sign-in failure while its pool's sign-in is usable or can renew on use, and into which no waking line went
    before that failure: the fixed waking line may go to it once, on today's terms (human_wake decides the rest).
    A waking line typed before the failure means the waking turn itself failed: it is named, and nothing more."""
    if not seat.session.human or not seat.is_up:
        return False
    failure = current(seat)
    if failure is None or failure.at is None or failure.at.tzinfo is None:
        return False
    from . import human_wake
    try:
        proof = human_wake.load(seat)
        if proof is not None and proof.data['phase'] != 'cancelled' and datetime.fromisoformat(proof.data['at']) <= failure.at:
            return False
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False  # unknown wake evidence keeps the hold
    return (observation or pool_observation(seat, now))[0] in USABLE


def current(seat):
    failure = turn_failure.current(seat)
    return failure if failure is not None and failure.kind == AUTHENTICATION else None


def pool_observation(seat, now=None):
    """Local metadata only: valid, renewable, expired or unknown, plus stable generation/newer fact."""
    now = now or datetime.now(timezone.utc)
    try:
        launched = seat.launched()
        pool = seat.ship.pools[launched.pool] if launched is not None else None
        source = credential.pool_file(pool) if pool is not None else None
        if source is None:
            return 'unknown', None, False
        before = source.stat()
        status = credential.sign_in_status(pool, source, now)
        after = source.stat()
        def generation(info):
            return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
        stamp = generation(before)
        if stamp != generation(after) or not stat.S_ISREG(after.st_mode) or status == 'unknown':
            return 'unknown', None, False
        if status == 'expired':
            return 'expired', stamp, False
        at = datetime.fromisoformat(launched.at)
        if at.tzinfo is None:
            return 'unknown', None, False
        return status, stamp, after.st_mtime > at.timestamp()
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return 'unknown', None, False


def _decision(seat, failure, attempt, observation, now):
    validity, stamp, newer = observation
    if not seat.is_up or validity not in USABLE:
        return 'attention'
    if attempt is not None and failed(attempt):
        return 'attention'  # its relaunch was held or raised: failed, never the wait for the renewed pass
    if attempt is None:
        if newer:
            return 'retry'
        # Before the first attempt, give a recorded failure one bounded renewal wait.
        at = failure.at if failure is not None else None
        return ('wait' if at is not None and at.tzinfo is not None
                and 0 <= (now - at).total_seconds() < WINDOW_SECONDS else 'attention')
    attempts = attempt['attempts']
    first, last = (datetime.fromisoformat(a['at']) for a in (attempts[0], attempts[-1]))
    age, since = (now - first).total_seconds(), (now - last).total_seconds()
    if (not 0 <= age < WINDOW_SECONDS or since < 0
            or seat.launched() is None or seat.launched().pool != attempt['pool']):
        return 'attention'
    if len(attempts) >= MAX_ATTEMPTS:
        # The old failure can remain visible while the last resume starts its next turn.
        return ('wait' if failure is not None and failure.at is not None
                and failure.at <= last else 'attention')
    if stamp != attempt['credential'] and since >= RETRY_SECONDS:
        return 'retry'
    return 'wait'


def detail(seat, failure=None):
    now = datetime.now(timezone.utc)
    observation = pool_observation(seat, now)
    state, _, newer = observation
    pool_note = ('login needed; pool sign-in expired' if state == 'expired' else
                 'pool sign-in validity unknown; needs attention' if state == 'unknown' else
                 RENEWABLE if state == 'renewable' else 'pool sign-in valid')
    if seat.session.human:
        if state in USABLE and human_wakeable(seat, now, observation=observation):
            return HUMAN_WAKING if state == 'valid' else RENEWABLE_WAKE
        return pool_note + '; human session needs attention; no automatic relaunch'
    if first_failure(seat, now=now, observation=observation):
        return DELIVERING if state == 'valid' else RENEWABLE_DELIVERY
    try:
        attempt = _attempt(seat)
        if _decision(seat, failure, attempt, observation, now) != 'attention':
            return WAITING if state == 'valid' else f'{RENEWABLE}; {WAITING}'
        if attempt is not None and failed(attempt):
            return f'sign-in resume {failed(attempt)}; needs attention' + ('; ' + pool_note if state != 'valid' else '')
        if attempt is not None:
            return 'relaunch did not cure it; needs attention' + ('; ' + pool_note if state != 'valid' else '')
    except (OSError, ValueError, TypeError, AttributeError):
        return 'resume attempt unknown; needs attention'
    if state not in USABLE:
        return pool_note
    return pool_note + (' and newer than launch; needs attention' if newer else
                        '; renewal newer than launch not proved; needs attention')


def waiting(reason):
    """A fixed operational reading, shared by status and alarm routing only."""
    return reason in tuple(f'{AUTHENTICATION}; {reading}' for reading in
                           (WAITING, DELIVERING, HUMAN_WAKING, f'{RENEWABLE}; {WAITING}',
                            RENEWABLE_DELIVERY, RENEWABLE_WAKE))


def status(seat):
    failure = current(seat)
    return f'{AUTHENTICATION}; {detail(seat, failure)}' if failure else None


def _path(seat):
    return seat.ship.runtime / 'post' / 'auth-relaunch' / f'{seat.name}.json'


def failed(attempt):
    """Why the latest attempt's relaunch did not start (held by a lifecycle check, or raised), or None."""
    return attempt['attempts'][-1].get('failed')


def _fail_latest(seat, attempts, why):
    """Record that the latest consumed attempt's relaunch did not start: a failure, read by status and alarm at
    once. The reason is a check's name and its own reason, or an error's kind: never a credential or record."""
    latest = dict(attempts[-1], failed=why)
    attempts = attempts[:-1] + [latest]
    durable.replace_file(_path(seat), json.dumps(dict(format=1, **latest, attempts=attempts)) + '\n')


def _attempt(seat):
    try:
        data = json.loads(turn_failure.metadata(_path(seat)))
    except FileNotFoundError:
        return None
    if not isinstance(data, dict) or data.get('format') != 1:
        raise ValueError('auth resume attempt unknown')
    attempts = data.get('attempts', [dict(at=data.get('at'), pool=data.get('pool'),
                                          credential=data.get('credential'))])
    if not isinstance(attempts, list) or not 1 <= len(attempts) <= MAX_ATTEMPTS:
        raise ValueError('auth resume attempts unknown')
    previous = None
    for item in attempts:
        if (not isinstance(item, dict) or not isinstance(item.get('pool'), str)
                or not isinstance(item.get('failed', ''), str)
                or not isinstance(item.get('credential'), list) or len(item['credential']) != 5
                or any(type(v) is not int for v in item['credential']) or not isinstance(item.get('at'), str)):
            raise ValueError('auth resume attempt unknown')
        at = datetime.fromisoformat(item['at'])
        if at.tzinfo is None or previous is not None and at <= previous:
            raise ValueError('auth resume attempt time unknown')
        previous = at
    if any(data.get(key) != attempts[-1].get(key) for key in ('at', 'pool', 'credential', 'failed')):
        raise ValueError('auth resume latest attempt unknown')
    return dict(data, attempts=attempts)


def run(service):
    """Service only. Human seats are named by readings/alarms and never mutated here."""
    for seat in service.resolved_seats():
        if seat.session.human:
            continue
        step = 'the resume journal'
        try:
            attempt = _attempt(seat)
            step = "the seat's record"
            record = seat.record()
            state = seat.harness.state(record) if record is not None else None
            failure = getattr(state, 'failure', None)
            if failure is None or failure.kind != AUTHENTICATION:
                # Only a genuine later completed success ends an attempted episode.
                # Merely opening a new turn does not prove that authentication recovered.
                completed = state.completed_turns if state is not None else ()
                if attempt and completed:
                    turn = completed[-1]
                    if (getattr(turn, 'successful', None) is True
                            and turn.timed and turn.failure is None and turn.ended is not None
                            and turn.ended > datetime.fromisoformat(attempt['at'])):
                        post_dir.remove(_path(seat))
                continue
            now = datetime.now(timezone.utc)
            step = "the pool's sign-in reading"
            observation = pool_observation(seat, now)
            if first_failure(seat, state, now, observation=observation):
                continue  # the next delivery comes first; a second sign-in failure brings the resume
            decision = _decision(seat, failure, attempt, observation, now)
            if decision == 'attention':
                service.needs_human(seat)
                continue
            if decision == 'wait' or not seat.is_up or state.turn_open is not False:
                continue
            _, stamp, _ = observation
            step = 'the resume journal write'
            path = _path(seat)
            post_dir.ensure_directory(path.parent)
            latest = dict(at=now.isoformat(), pool=seat.launched().pool, credential=stamp)
            attempts = (attempt['attempts'] if attempt else []) + [latest]
            durable.replace_file(path, json.dumps(dict(format=1, **latest, attempts=attempts)) + '\n')
            # Consume before the existing lifecycle path, including across process failure.
            step = 'the relaunch'
            service.request_wait = None
            proof = service.relaunch(seat, True)
            if proof is None:
                # A held relaunch is recorded failed before any log line, so that no later failure leaves it waiting.
                step = 'the record of a held relaunch'
                _fail_latest(seat, attempts, f'held by {held_by(service)}')
                step = 'the held relaunch log'
                service.log('authentication', seat=seat.name, proof=f'{seat.name}: sign-in resume held by {held_by(service)}')
                service.needs_human(seat)
            else:
                step = 'the relaunch log'  # the relaunch started: what fails from here is not the relaunch
                sign_in = RENEWABLE if observation[0] == 'renewable' else 'renewed pool sign-in'
                service.log('authentication', seat=seat.name,
                            proof=f'{seat.name}: {sign_in}; relaunch with resume ({len(attempts)}/{MAX_ATTEMPTS}) · {proof}')
        except (OSError, ValueError, TypeError, AttributeError, ShipError) as error:
            # Do not report raw credential/record/state contents as an alarm or proof: the check and the error's kind only.
            service.log('authentication', seat=seat.name,
                        proof=f'{seat.name}: sign-in recovery held by {step} ({type(error).__name__}); needs attention')
            if step == 'the relaunch':
                try:
                    _fail_latest(seat, attempts, f'raised {type(error).__name__}')
                except (OSError, ValueError, TypeError):
                    pass  # unrecorded, the status still reads waiting and no alarm goes, as before; the attempt stays consumed
            service.needs_human(seat)


def held_by(service) -> str:
    """The check that held a relaunch, as the service recorded it: its name and its reason."""
    wait = getattr(service, 'request_wait', None)
    if not wait:
        return 'an unnamed lifecycle check'
    dimension, reason = wait
    check = 'the recovery hold' if dimension == 'recovery' else f'the idle proof ({dimension})'
    return f'{check}: {reason}'
