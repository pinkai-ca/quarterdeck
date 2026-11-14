"""Bounded check evidence beside proven marks; publication order, never configuration."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat

from . import durable, harness as harnesses

MAX_BYTES = 16384
REASONS = {
    'pool': ('pool', 'no compatible declared pool'),
    'installed': ('installed', 'installed program version was not established'),
    'launch': ('launch', 'program launch was not established'),
    'door': ('door', 'input door readiness was not established'),
    'paste': ('paste', 'input delivery was not confirmed'),
    'report': ('report', 'task report did not close the order'),
    'file': ('file', 'task file did not match the required line'),
    'record': ('record', 'ended task turn was not established in the record'),
    'program': ('installed', 'the seat did not run the program doctor shows'),
    'answer': ('account holder question', 'an answer for the account holder did not clear its question'),
    'wake': ('human-session waking line', 'the human-session waking line was not proven'),
    'model': ('model', 'the requested model was not confirmed in the program or its record'),
    'account-login': ('account holder question', 'account-holder question: login'),
    'account-training': ('account holder question', 'account-holder question: training data'),
    'account-permission': ('account holder question', 'account-holder question: permission'),
    'account-trust': ('account holder question', 'account-holder question: workspace trust'),
    'passed': ('complete', 'all check steps passed'),
    'incomplete': (None, 'check outcome unknown: completion not recorded'),
    'interrupted': (None, 'check outcome unknown: interrupted'),
    'unclassified': (None, 'check outcome unknown: unclassified'),
}
QUESTIONS = {'login': 'account-login', 'training data question': 'account-training',
             'permission prompt': 'account-permission', 'approval prompt': 'account-permission',
             'trust dialog': 'account-trust'}
UNKNOWN = {'incomplete', 'interrupted', 'unclassified'}


def path(runtime: Path, harness: str) -> Path:
    if harness not in harnesses.NAMES:
        raise ValueError('unknown check program')
    return runtime / 'proven' / 'check-outcomes' / f'{harness}.json'


def event(outcome, version=None, reason='unclassified'):
    if reason not in REASONS or outcome not in ('passed', 'failed', 'unknown'):
        raise ValueError('unknown check outcome')
    step, _ = REASONS[reason]
    if (outcome == 'unknown') != (reason in UNKNOWN) or (outcome == 'passed') != (reason == 'passed'):
        raise ValueError('check outcome and reason disagree')
    version = version if isinstance(version, str) and re.fullmatch(r'[A-Za-z0-9._+\-]{1,128}', version) else None
    return dict(outcome=outcome, at=datetime.now(timezone.utc).isoformat() if outcome != 'unknown' else None,
                version=version, step=step, reason=reason)


def _event(data):
    if not isinstance(data, dict) or set(data) != {'sequence', 'outcome', 'at', 'version', 'step', 'reason'}:
        raise ValueError('invalid check event')
    if type(data['sequence']) is not int or not 0 < data['sequence'] < 2**63:
        raise ValueError('invalid check sequence')
    outcome, reason, version = data['outcome'], data['reason'], data['version']
    if (outcome not in ('passed', 'failed', 'unknown') or reason not in REASONS
            or (outcome == 'unknown') != (reason in UNKNOWN)
            or (outcome == 'passed') != (reason == 'passed')):
        raise ValueError('invalid check outcome')
    if (data['step'] != REASONS[reason][0]
            or (version is not None and (not isinstance(version, str)
                or not re.fullmatch(r'[A-Za-z0-9._+\-]{1,128}', version)))):
        raise ValueError('invalid check classification')
    if data['outcome'] == 'unknown':
        if data['at'] is not None:
            raise ValueError('unknown completion has no time')
    else:
        when = datetime.fromisoformat(data['at'])
        if when.utcoffset() is None or when.utcoffset().total_seconds() != 0:
            raise ValueError('check time must be UTC')
    return data


def _data(data, harness):
    if (not isinstance(data, dict) or set(data) != {'format', 'harness', 'latest_attempt', 'definitive'}
            or type(data['format']) is not int or data['format'] != 1 or data['harness'] != harness):
        raise ValueError('invalid check evidence')
    latest = _event(data['latest_attempt'])
    final = data['definitive']
    if final is not None:
        _event(final)
        if (final['outcome'] == 'unknown' or final['sequence'] > latest['sequence']
                or (latest['outcome'] == 'unknown' and final['sequence'] == latest['sequence'])):
            raise ValueError('invalid definitive check')
    if latest['outcome'] != 'unknown' and latest != final:
        raise ValueError('latest completed check differs')
    return data


def read(runtime, harness):
    source = path(runtime, harness)
    try:
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('check evidence is not regular')
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('check evidence exceeds bound')
    return _data(json.loads(raw), harness)


@contextmanager
def _lock(runtime, harness):
    """Hidden from every jail, and waited for within a bound."""
    from . import ship_locks
    with ship_locks.held(ship_locks.path(runtime, 'check-outcomes', f'{harness}.lock'), f'{harness}: check outcome'):
        yield


def publish(runtime, harness, observed):
    """A short publication lock orders completions; no lock covers a paid check."""
    source = path(runtime, harness)
    source.parent.mkdir(parents=True, exist_ok=True)
    with _lock(runtime, harness):
        previous = read(runtime, harness)
        sequence = previous['latest_attempt']['sequence'] + 1 if previous else 1
        latest = _event(dict(observed, sequence=sequence))
        final = (previous['definitive'] if previous else None) if latest['outcome'] == 'unknown' else latest
        document = _data(dict(format=1, harness=harness, latest_attempt=latest, definitive=final), harness)
        text = json.dumps(document, sort_keys=True) + '\n'
        if len(text.encode()) > MAX_BYTES:
            raise ValueError('check evidence exceeds bound')
        durable.replace_file(source, text)


def collect(runtime):
    found, errors = [], []
    for harness in harnesses.NAMES:
        try:
            if (data := read(runtime, harness)) is not None:
                found.append(data)
        except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
            errors.append(f'{harness} check outcome unknown: evidence unavailable')
    return found, errors


def alarms(outcomes):
    return [f"{data['harness']} failed its check at {final['at']}: {REASONS[final['reason']][1]}"
            for data in outcomes if (final := data['definitive']) is not None and final['outcome'] == 'failed']
