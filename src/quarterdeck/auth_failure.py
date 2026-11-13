"""Recorded sign-in failures: visible readings and one guarded resume per failed episode.

Native syntax belongs to the adapter. Credential values never enter this module's
journal: it retains only a stable file generation and the consumed attempt's time.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import stat

from . import credential, durable, turn_failure
from .harness.types import AUTHENTICATION
from .ship import ShipError


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


def detail(seat, failure=None):
    state, _, newer = pool_observation(seat)
    pool_note = ('login needed; pool sign-in expired' if state == 'expired' else
                 'pool sign-in validity unknown; needs attention' if state == 'unknown' else
                 'pool sign-in valid')
    if seat.session.human:
        return pool_note + '; human session needs attention; no automatic relaunch'
    try:
        attempt = _attempt(seat)
        if attempt is not None:
            note = ('relaunch did not cure it' if failure is not None and failure.at is not None
                    and failure.at > datetime.fromisoformat(attempt['at']) else
                    'resume already attempted; needs attention')
            return note + ('; ' + pool_note if state != 'valid' else '')
    except (OSError, ValueError, TypeError):
        return 'resume attempt unknown; needs attention'
    if state != 'valid':
        return pool_note
    return pool_note + (' and newer than launch' if newer else '; renewal newer than launch not proved')


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
    if (not isinstance(data, dict) or data.get('format') != 1
            or not isinstance(data.get('pool'), str) or not isinstance(data.get('credential'), list)
            or len(data['credential']) != 5 or any(type(v) is not int for v in data['credential'])
            or not isinstance(data.get('at'), str)):
        raise ValueError('auth resume attempt unknown')
    at = datetime.fromisoformat(data['at'])
    if at.tzinfo is None:
        raise ValueError('auth resume attempt time unknown')
    return data


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
                    if (turn.timed and turn.failure is None and turn.ended is not None
                            and turn.ended > datetime.fromisoformat(attempt['at'])):
                        _path(seat).unlink()
                        durable.sync_folder(_path(seat).parent)
                continue
            if attempt is not None:
                service.needs_human(seat)
                continue
            now = datetime.now(timezone.utc)
            validity, stamp, newer = pool_observation(seat, now)
            if not seat.is_up or state.turn_open is not False or validity != 'valid' or not newer:
                service.needs_human(seat)
                continue
            path = _path(seat)
            path.parent.mkdir(parents=True, exist_ok=True)
            durable.sync_folder(path.parent.parent)  # retain the new attempt-directory entry across a power cut
            durable.replace_file(path, json.dumps(dict(format=1, at=now.isoformat(),
                                 pool=seat.launched().pool, credential=stamp)) + '\n')
            # Consume before the existing lifecycle path, including across process failure.
            proof = service.relaunch(seat, True)
            service.log('authentication', seat=seat.name,
                        proof=(f'{seat.name}: renewed pool sign-in; one relaunch with resume · {proof}'
                               if proof is not None else f'{seat.name}: sign-in resume held by existing lifecycle checks'))
            if proof is None:
                service.needs_human(seat)
        except (OSError, ValueError, TypeError, AttributeError, ShipError):
            # Do not report raw credential/record/state contents as an alarm or proof.
            service.log('authentication', seat=seat.name, proof=f'{seat.name}: sign-in recovery held; needs attention')
            service.needs_human(seat)
