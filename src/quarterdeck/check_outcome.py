"""Bounded check evidence beside proven marks; publication order, never configuration."""

from __future__ import annotations

from contextlib import contextmanager, suppress
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from . import durable, harness as harnesses

MAX_BYTES = 16384
OBSERVATION_BYTES = 2048
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
    'usage-unavailable': ('usage', 'native usage allowance unavailable'),
    'usage-unreadable': ('usage', 'native usage allowance unreadable'),
    'launch-setting-on': (None, 'reported on at launch'),
    'launch-setting-unreadable': (None, 'could not be read for this launch'),
    'account-login': ('account holder question', 'account-holder question: login'),
    'account-training': ('account holder question', 'account-holder question: training data'),
    'account-permission': ('account holder question', 'account-holder question: permission'),
    'account-trust': ('account holder question', 'account-holder question: workspace trust'),
    'passed': ('complete', 'all check steps passed'),
    'incomplete': (None, 'completion not recorded'),
    'interrupted': (None, 'interrupted'),
    'unclassified': (None, 'completion could not be classified'),
}
ACCOUNT_QUESTIONS = {'account-login', 'account-training', 'account-permission', 'account-trust'}
LAUNCH_SETTINGS = {'launch-setting-on', 'launch-setting-unreadable'}
UNKNOWN = {'incomplete', 'interrupted', 'unclassified'}
OBSERVATION_REASONS = {'exception': 'check raised an exception'}


class MalformedOutcome(ValueError):
    """The saved bytes do not establish a check history."""


def path(runtime: Path, harness: str) -> Path:
    if harness not in harnesses.NAMES:
        raise ValueError('unknown check program')
    return runtime / 'proven' / 'check-outcomes' / f'{harness}.json'


def _step_name(step):
    return isinstance(step, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ._()/:-]{0,95}', step) is not None


def _classification(outcome, reason, step):
    if reason in LAUNCH_SETTINGS:
        return outcome == 'failed' and _step_name(step)
    if outcome == 'failed' and reason == 'unclassified':
        return _step_name(step)
    if reason not in REASONS or outcome not in ('passed', 'failed', 'unknown'):
        return False
    return ((outcome == 'unknown') == (reason in UNKNOWN)
            and (outcome == 'passed') == (reason == 'passed') and step == REASONS[reason][0])


def failure_reason(reason):
    return isinstance(reason, str) and reason in REASONS and reason not in UNKNOWN and reason != 'passed'


def event(outcome, version=None, reason='unclassified', *, step=None):
    if outcome == 'failed' and reason == 'unclassified':
        step = step if _step_name(step) else 'check step'
    elif reason in REASONS and reason not in LAUNCH_SETTINGS:
        step = REASONS[reason][0]
    if not _classification(outcome, reason, step):
        raise ValueError('invalid check classification')
    version = version if isinstance(version, str) and re.fullmatch(r'[A-Za-z0-9._+\-]{1,128}', version) else None
    return dict(outcome=outcome, at=datetime.now(timezone.utc).isoformat() if outcome != 'unknown' else None,
                version=version, step=step, reason=reason)


def _event(data):
    fields = {'sequence', 'outcome', 'at', 'version', 'step', 'reason'}
    if not isinstance(data, dict) or set(data) not in (fields, fields | {'usage'}):
        raise ValueError('invalid check event')
    if 'usage' in data:
        usage = data['usage']
        if (not isinstance(usage, dict) or not {'before', 'after'} & usage.keys()
                or 'share_spent' not in usage or usage.keys() - {'before', 'after', 'share_spent'}
                or any(value != 'unknown' for value in usage.values())):
            raise ValueError('invalid check usage')
    if type(data['sequence']) is not int or not 0 < data['sequence'] < 2**63:
        raise ValueError('invalid check sequence')
    outcome, reason, version = data['outcome'], data['reason'], data['version']
    if (not _classification(outcome, reason, data['step'])
            or (version is not None and (not isinstance(version, str)
                or not re.fullmatch(r'[A-Za-z0-9._+\-]{1,128}', version)))):
        raise ValueError('invalid check classification')
    if outcome == 'unknown':
        if data['at'] is not None:
            raise ValueError('unknown completion has no time')
    else:
        _utc(data['at'])
    return data


def _utc(value):
    when = datetime.fromisoformat(value)
    if when.utcoffset() is None or when.utcoffset().total_seconds() != 0:
        raise ValueError('check time must be UTC')
    return when.isoformat()


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


def _bytes(source, limit):
    try:
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('check evidence is not regular')
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('check evidence exceeds bound')
    return raw, info


def _snapshot(source, harness):
    try:
        if (saved := _bytes(source, MAX_BYTES)) is not None:
            raw, info = saved
            return _data(json.loads(raw), harness), raw, info
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise MalformedOutcome('invalid saved check evidence') from None


def read(runtime, harness):
    saved = _snapshot(path(runtime, harness), harness)
    return saved[0] if saved else None


def _observation_path(source):
    return source.with_suffix('.unknown.json')


def _binding(raw, info):
    # A replacement can restart sequence numbering, or even contain identical bytes.
    return [info.st_dev, info.st_ino, info.st_ctime_ns, hashlib.sha256(raw).hexdigest()]


def _observation(source, latest, raw, info):
    try:
        saved = _bytes(_observation_path(source), OBSERVATION_BYTES)
        if saved is None:
            return None
        data = json.loads(saved[0])
        if (not isinstance(data, dict) or set(data) != {'format', 'binding', 'at', 'classification'}
                or type(data['format']) is not int or data['format'] != 1 or data['binding'] != _binding(raw, info)):
            return None
        classification = data['classification']
        if classification is not None and (latest['reason'] != 'unclassified' or classification not in OBSERVATION_REASONS):
            return None
        return dict(at=_utc(data['at']), classification=classification)
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        return None


@contextmanager
def _lock(runtime, harness):
    """Hidden from every jail, and waited for within a bound."""
    from . import ship_locks
    with ship_locks.held(ship_locks.path(runtime, 'check-outcomes', f'{harness}.lock'), f'{harness}: check outcome'):
        yield


def publish(runtime, harness, observed, *, classification=None):
    """A short publication lock orders completions; no lock covers a paid check."""
    if classification is not None and (observed['outcome'] != 'unknown' or observed['reason'] != 'unclassified'
                                       or classification not in OBSERVATION_REASONS):
        raise ValueError('invalid unknown check classification')
    at = datetime.now(timezone.utc).isoformat() if observed['outcome'] == 'unknown' else None
    source = path(runtime, harness)
    source.parent.mkdir(parents=True, exist_ok=True)
    with _lock(runtime, harness):
        try:
            previous = read(runtime, harness)
        except MalformedOutcome:
            previous = None  # malformed history establishes neither a prior sequence nor a definitive result
        sequence = previous['latest_attempt']['sequence'] + 1 if previous else 1
        latest = _event(dict(observed, sequence=sequence))
        final = (previous['definitive'] if previous else None) if latest['outcome'] == 'unknown' else latest
        document = _data(dict(format=1, harness=harness, latest_attempt=latest, definitive=final), harness)
        text = json.dumps(document, sort_keys=True) + '\n'
        raw = text.encode()
        if len(raw) > MAX_BYTES:
            raise ValueError('check evidence exceeds bound')
        durable.replace_file(source, text)
        # The outcome is valid on its own. A missing or stale observation leaves it untimed.
        with suppress(OSError):
            if at is None:
                _observation_path(source).unlink(missing_ok=True)
            else:
                metadata = dict(format=1, binding=_binding(raw, source.stat(follow_symlinks=False)),
                                at=at, classification=classification)
                durable.replace_file(_observation_path(source), json.dumps(metadata, sort_keys=True) + '\n')


def collect(runtime):
    found, errors = [], []
    for harness in harnesses.NAMES:
        try:
            source = path(runtime, harness)
            if (saved := _snapshot(source, harness)) is not None:
                data, raw, info = saved
                latest = data['latest_attempt']
                if latest['outcome'] == 'unknown' and (observation := _observation(source, latest, raw, info)) is not None:
                    data['unknown_observation'] = observation
                found.append(data)
        except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
            errors.append(f'{harness} check unknown: evidence unavailable')
    return found, errors


def alarms(outcomes):
    lines = []
    for data in outcomes:
        final = data['definitive']
        if final is None or final['outcome'] != 'failed':
            continue
        step = final['step']
        reason = final['reason']
        if reason == 'unclassified':
            detail = f'unclassified failure at {step}'
        elif reason in LAUNCH_SETTINGS:
            detail = f'{step} {REASONS[reason][1]}'
        else:
            detail = REASONS[reason][1]
        lines.append(f"{data['harness']} failed its check at {final['at']}: {detail}")
    return lines


def unknown_alarms(outcomes):
    alarms = []
    for data in outcomes:
        latest = data['latest_attempt']
        if latest['outcome'] == 'unknown':
            observation = data.get('unknown_observation')
            since = f" since {observation['at']}" if observation is not None else ''
            reason = REASONS[latest['reason']][1]
            if observation is not None and observation['classification'] is not None:
                reason = OBSERVATION_REASONS[observation['classification']]
            alarms.append(f"{data['harness']} check unknown{since}: {reason}")
    return alarms
