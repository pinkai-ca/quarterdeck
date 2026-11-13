"""Recorded sign-in failures: visible readings and bounded guarded resumes per failed episode.

Native syntax belongs to the adapter. Credential values never enter this module's
journal: it retains only stable file generations and consumed attempts' times.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import stat

from . import credential, durable, turn_failure
from .harness.types import AUTHENTICATION
from .ship import ShipError

MAX_ATTEMPTS = 3
RETRY_SECONDS = 60
WINDOW_SECONDS = 15 * 60
WAITING = 'sign-in: waiting for the renewed pass'


def current(seat):
    failure = turn_failure.current(seat)
    return failure if failure is not None and failure.kind == AUTHENTICATION else None


def pool_observation(seat, now=None):
    """Local metadata only: valid, expired or unknown, plus stable generation/newer fact."""
    now = now or datetime.now(timezone.utc)
    try:
        launched = seat.launched()
        pool = seat.ship.pools[launched.pool] if launched is not None else None
        source = credential.pool_file(pool) if pool is not None else None
        if source is None:
            return 'unknown', None, False
        before = source.stat()
        fresh = credential.freshness(pool, source)
        grant = credential.grant_expiry(pool)
        after = source.stat()
        def generation(info):
            return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
        stamp = generation(before)
        if stamp != generation(after) or not stat.S_ISREG(after.st_mode) or fresh is None:
            return 'unknown', None, False
        expiry = fresh[0]
        if type(expiry) not in (int, float) or not math.isfinite(expiry):
            return 'unknown', None, False
        if expiry <= now.timestamp() or grant is not None and grant <= now:
            return 'expired', stamp, False
        at = datetime.fromisoformat(launched.at)
        if at.tzinfo is None:
            return 'unknown', None, False
        return 'valid', stamp, after.st_mtime > at.timestamp()
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return 'unknown', None, False


def _decision(seat, failure, attempt, observation, now):
    validity, stamp, newer = observation
    if not seat.is_up or validity != 'valid':
        return 'attention'
    if attempt is None:
        if validity == 'valid' and newer:
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
    if validity == 'valid' and stamp != attempt['credential'] and since >= RETRY_SECONDS:
        return 'retry'
    return 'wait'


def detail(seat, failure=None):
    now = datetime.now(timezone.utc)
    observation = pool_observation(seat, now)
    state, _, newer = observation
    pool_note = ('login needed; pool sign-in expired' if state == 'expired' else
                 'pool sign-in validity unknown; needs attention' if state == 'unknown' else
                 'pool sign-in valid')
    if seat.session.human:
        return pool_note + '; human session needs attention; no automatic relaunch'
    try:
        attempt = _attempt(seat)
        if _decision(seat, failure, attempt, observation, now) != 'attention':
            return WAITING
        if attempt is not None:
            return 'relaunch did not cure it; needs attention' + ('; ' + pool_note if state != 'valid' else '')
    except (OSError, ValueError, TypeError, AttributeError):
        return 'resume attempt unknown; needs attention'
    if state != 'valid':
        return pool_note
    return pool_note + (' and newer than launch; needs attention' if newer else
                        '; renewal newer than launch not proved; needs attention')


def waiting(reason):
    """A fixed operational reading, shared by status and alarm routing only."""
    return reason == f'{AUTHENTICATION}; {WAITING}'


def status(seat):
    failure = current(seat)
    return f'{AUTHENTICATION}; {detail(seat, failure)}' if failure else None


def _path(seat):
    return seat.ship.runtime / 'post' / 'auth-relaunch' / f'{seat.name}.json'


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
                or not isinstance(item.get('credential'), list) or len(item['credential']) != 5
                or any(type(v) is not int for v in item['credential']) or not isinstance(item.get('at'), str)):
            raise ValueError('auth resume attempt unknown')
        at = datetime.fromisoformat(item['at'])
        if at.tzinfo is None or previous is not None and at <= previous:
            raise ValueError('auth resume attempt time unknown')
        previous = at
    if any(data.get(key) != attempts[-1][key] for key in ('at', 'pool', 'credential')):
        raise ValueError('auth resume latest attempt unknown')
    return dict(data, attempts=attempts)


def run(service):
    """Service only. Human seats are named by readings/alarms and never mutated here."""
    for seat in service.resolved_seats():
        if seat.session.human:
            continue
        try:
            attempt = _attempt(seat)
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
                        _path(seat).unlink()
                        durable.sync_folder(_path(seat).parent)
                continue
            now = datetime.now(timezone.utc)
            observation = pool_observation(seat, now)
            decision = _decision(seat, failure, attempt, observation, now)
            if decision == 'attention':
                service.needs_human(seat)
                continue
            if decision == 'wait' or not seat.is_up or state.turn_open is not False:
                continue
            _, stamp, _ = observation
            path = _path(seat)
            path.parent.mkdir(parents=True, exist_ok=True)
            durable.sync_folder(path.parent.parent)  # retain the new attempt-directory entry across a power cut
            latest = dict(at=now.isoformat(), pool=seat.launched().pool, credential=stamp)
            attempts = (attempt['attempts'] if attempt else []) + [latest]
            durable.replace_file(path, json.dumps(dict(format=1, **latest, attempts=attempts)) + '\n')
            # Consume before the existing lifecycle path, including across process failure.
            proof = service.relaunch(seat, True)
            service.log('authentication', seat=seat.name,
                        proof=(f'{seat.name}: renewed pool sign-in; relaunch with resume ({len(attempts)}/{MAX_ATTEMPTS}) · {proof}'
                               if proof is not None else f'{seat.name}: sign-in resume held by existing lifecycle checks'))
            if proof is None:
                service.needs_human(seat)
        except (OSError, ValueError, TypeError, AttributeError, ShipError):
            # Do not report raw credential/record/state contents as an alarm or proof.
            service.log('authentication', seat=seat.name, proof=f'{seat.name}: sign-in recovery held; needs attention')
            service.needs_human(seat)
