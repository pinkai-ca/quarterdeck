"""Service-owned progress for one closed-record receipt pass per framework revision.

The checkpoint carries only opaque journal keys. Receipt evidence and positive
settlement belong to the journal and attempts, never to this progress cache.
"""

import json
import re
import hashlib
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from . import durable, post_dir
from .ship import ShipError


ELIGIBLE = frozenset({'prepared', 'pasting', 'submitting', 'submitted', 'queued',
                      'not-in-closed-record', 'carried', 'uncertain'})
ATTEMPT_RESULTS = frozenset({None, 'pending', 'blind', 'queued', 'not-in-closed-record',
                             'launch', 'uncertain', 'unsettled'})


def _commit(value):
    return isinstance(value, str) and re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', value)


def _key(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value)


def _validate(data):
    if (not isinstance(data, dict) or set(data) != {'format', 'commit', 'observed', 'complete'}
            or type(data['format']) is not int or data['format'] != 1
            or not _commit(data['commit']) or type(data['complete']) is not bool
            or not isinstance(data['observed'], list)
            or any(not _key(key) for key in data['observed'])
            or len(set(data['observed'])) != len(data['observed'])):
        raise ShipError('closed receipt refresh checkpoint unsupported; retained')
    return data


class Checkpoint:
    """One service writes; marking follows completed journal/attempt settlement."""

    def __init__(self, runtime, commit, *, deferred=False):
        if not _commit(commit):
            raise ShipError('closed receipt refresh revision unknown; retained')
        self.path = runtime / 'post' / 'receipt-refresh.json'
        try:
            saved = _validate(json.loads(post_dir.read_text(self.path)))
        except FileNotFoundError:
            saved = None
        except (ValueError, UnicodeError) as error:
            raise ShipError('closed receipt refresh checkpoint unreadable; retained') from error
        self.data = (saved if saved is not None and saved['commit'] == commit else
                     dict(format=1, commit=commit, observed=[], complete=False))
        self._observed = set(self.data['observed'])
        self._deferred = deferred
        self._dirty = False

    @property
    def complete(self):
        return self.data['complete']

    def seen(self, key):
        if not _key(key):
            raise ShipError('closed receipt refresh journal key unknown; retained')
        return key in self._observed

    def _save(self, data):
        post_dir.ensure_directory(self.path.parent)
        durable.replace_file(self.path, json.dumps(data, sort_keys=True) + '\n')
        self.data = data  # a failed publication never advances in-memory progress
        self._observed = set(data['observed'])
        self._dirty = False

    def mark(self, key):
        """Call only when an unchanged negative or both positive owners are settled."""
        if self.seen(key):
            return
        if self.complete:
            raise ShipError('closed receipt refresh already complete; retained')
        if self._deferred:
            self._observed.add(key)
            self._dirty = True
        else:
            self._save({**self.data, 'observed': sorted(self._observed | {key})})

    def flush(self):
        """Publish partial progress once; a killed pass may safely repeat its reads."""
        if self._dirty:
            self._save({**self.data, 'observed': sorted(self._observed)})

    def finish(self):
        if not self.complete:
            self._save({**self.data, 'observed': sorted(self._observed), 'complete': True})


def _submission(data):
    fields = {key: data[key] for key in ('seat', 'identity', 'at', 'since', 'record', 'payload', 'attempts')}
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def journal_key(proof):
    """Phase/refresh progress may change; the original submission identity may not."""
    return hashlib.sha256((proof.path.name + ':' + _submission(proof.data)).encode()).hexdigest()


def validate_intent(data):
    """A positive intent contains attempt metadata, never an alternative payload."""
    intent = data['receipt_refresh']
    expected = intent.get('attempts') if isinstance(intent, dict) else None
    fields = {'format', 'commit', 'attempts', 'phase', 'journal'}
    if (not isinstance(intent, dict) or set(intent) not in (fields, fields | {'outcome'})
            or type(intent['format']) is not int or intent['format'] != 1
            or not _commit(intent['commit']) or not isinstance(expected, dict)
            or set(expected) != set(data['attempts']) or intent['phase'] not in ELIGIBLE
            or not _key(intent['journal'])
            or _submission(data) != intent['journal']
            or 'outcome' in intent and intent['outcome'] != 'superseded'):
        raise ValueError('invalid closed receipt refresh intent')
    fields = {'message', 'seat', 'at', 'pane', 'pid', 'launched_at', 'result', 'detail'}
    for name, row in expected.items():
        if (Path(name).name != name or not isinstance(row, dict) or set(row) != fields
                or row['result'] not in ATTEMPT_RESULTS or not isinstance(row['message'], str)
                or row['seat'] != data['seat'] or not isinstance(row['detail'], str)
                or (row['pane'], row['pid'], row['launched_at']) !=
                   (data['identity']['pane'], data['identity']['pid'], data['identity']['launch'])
                or datetime.fromisoformat(row['at']).tzinfo is None):
            raise ValueError('closed receipt refresh attempt identity differs')
    return intent


def unfinished(data):
    return ('receipt_refresh' in data and data['phase'] != 'delivered'
            and data['receipt_refresh'].get('outcome') != 'superseded')


def _supersede(proof):
    proof.save(receipt_refresh={**proof.data['receipt_refresh'], 'outcome': 'superseded'})


def _owned_closed(seat, proof):
    """Use retained reader metadata and ordinary launch closure, never a current join."""
    from . import harness, record_owner
    from .harness.closed_record import _closed

    if not proof.data['record'] or not _closed(seat, proof.data['identity']):
        return None
    record, name = Path(proof.data['record']), proof.data['identity']['harness']
    names = {row.get('harness') for filename in (record_owner.REGISTRY, 'record-launches.jsonl')
             for row in record_owner.events(seat.folder / filename)
             if Path(row['record']) == record and row.get('harness')}
    return (record, name) if names == {name} and name in harness.NAMES else None


def _recover(service, proof):
    from . import post

    intent = validate_intent(proof.data)
    if intent.get('outcome') == 'superseded':
        return True
    if proof.data['phase'] not in (intent['phase'], 'delivered'):
        _supersede(proof)
        return True  # a later journal outcome won; the intent cannot hold it forever
    expected = [post.Attempt(**row, path=service.attempts.dir / name)
                for name, row in intent['attempts'].items()]
    outcome = service.attempts.settle_closed_receipt(expected)
    if outcome == 'superseded':
        _supersede(proof)
        return True
    if outcome != 'settled':
        return False
    if proof.data['phase'] != 'delivered':
        proof.save(phase='delivered', **({'screen_queue': False} if 'screen_queue' in proof.data else {}))
    return True


def recover(service, proof):
    """Finish a durable positive decision without another record read or resend."""
    from . import pending, post

    with post.admission(service.runtime, proof.data['seat']):
        current = json.loads(proof.path.read_text())
        pending.validate(current, proof.data['seat'])
        if current != proof.data:
            return False
        return _recover(service, proof)


def _observe(service, seat, proof, attempts, commit):
    from . import input_receipt, pending, post
    from .harness.base import record_fingerprint
    from .tmux import TmuxError

    try:
        owned = _owned_closed(seat, proof)
        if owned is None:
            return True  # not positively a saved closed launch: excluded from this pass
        record, reader = owned
        before = record_fingerprint(record)
        result = input_receipt.confirmed(reader, record, proof.data['payload'],
                                         datetime.fromisoformat(proof.data['since']))
        if record_fingerprint(record) != before:
            return False  # changed source is transient, not a completed negative observation
        if result != 'delivered':
            return True  # even an exact queued result changes no original evidence
    except (OSError, ValueError, TypeError, KeyError, AttributeError, TmuxError):
        return True  # one unreadable observation changes neither evidence owner
    expected = [attempts[name] for name in proof.data['attempts']]
    if any(a.result not in ATTEMPT_RESULTS for a in expected):
        return True  # a prior terminal/lifecycle outcome is outside this refresh
    intent = dict(format=1, commit=commit, phase=proof.data['phase'], journal=_submission(proof.data),
                  attempts={a.path.name: {k: v for k, v in asdict(a).items() if k != 'path'}
                            for a in expected})
    candidate = {**proof.data, 'receipt_refresh': intent}
    validate_intent(candidate)
    with post.admission(service.runtime, seat.name):
        current = json.loads(proof.path.read_text())
        pending.validate(current, seat.name)
        if current != proof.data or _owned_closed(seat, proof) != owned or record_fingerprint(record) != before:
            return False
        outcome = service.attempts.settle_closed_receipt(
                expected, lambda: proof.save(receipt_refresh=intent))
        if outcome == 'superseded':
            return True  # no intent was published; preserve both competing owners
        if outcome != 'settled':
            return False
        proof.save(phase='delivered', **({'screen_queue': False} if 'screen_queue' in proof.data else {}))
    return True


def run(service, commit):
    """Existing rebrief duty retries only unfinished progress; no native program runs."""
    from . import pending
    from .seat import SelectionError
    from .shutdown import never_pasted
    from .tmux import TmuxError

    if getattr(service, 'receipt_refresh_commit', None) == commit:
        return
    checkpoint = Checkpoint(service.runtime, commit, deferred=True)
    if checkpoint.complete:
        service.receipt_refresh_commit = commit
        return
    attempts = {a.path.name: a for a in service.attempts.all()}
    complete = True
    for name in service.ship.seats:
        try:
            seat = service.seat(name)
            proofs, errors = pending.journals(seat)
            if errors:
                complete = False
            for proof in proofs:
                key = journal_key(proof)
                if checkpoint.seen(key):
                    continue
                if unfinished(proof.data):
                    settled = recover(service, proof)
                elif 'receipt_refresh' in proof.data:
                    settled = True  # delivered or superseded: never replay its positive intent
                elif proof.data['phase'] in ELIGIBLE and not never_pasted(proof.data):
                    settled = _observe(service, seat, proof, attempts, commit)
                else:
                    settled = True
                if settled:
                    checkpoint.mark(key)
                else:
                    complete = False
        except (OSError, ValueError, TypeError, KeyError, AttributeError, ShipError, SelectionError, TmuxError) as error:
            complete = False
            service.log('error', step='receipt-refresh', seat=name, error=str(error))
    if complete:
        checkpoint.finish()
        service.receipt_refresh_commit = commit
    else:
        checkpoint.flush()
