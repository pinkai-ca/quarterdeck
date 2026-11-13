"""Service-owned progress for one closed-record receipt pass per framework revision.

The checkpoint carries only opaque journal keys. Receipt evidence and positive
settlement belong to the journal and attempts, never to this progress cache.
"""

import json
import re

from . import durable
from .ship import ShipError


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

    def __init__(self, runtime, commit):
        if not _commit(commit):
            raise ShipError('closed receipt refresh revision unknown; retained')
        self.path = runtime / 'post' / 'receipt-refresh.json'
        try:
            saved = _validate(json.loads(self.path.read_text()))
        except FileNotFoundError:
            saved = None
        except (ValueError, UnicodeError) as error:
            raise ShipError('closed receipt refresh checkpoint unreadable; retained') from error
        self.data = (saved if saved is not None and saved['commit'] == commit else
                     dict(format=1, commit=commit, observed=[], complete=False))

    @property
    def complete(self):
        return self.data['complete']

    def seen(self, key):
        if not _key(key):
            raise ShipError('closed receipt refresh journal key unknown; retained')
        return key in self.data['observed']

    def _save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        durable.replace_file(self.path, json.dumps(data, sort_keys=True) + '\n')
        self.data = data  # a failed publication never advances in-memory progress

    def mark(self, key):
        """Call only when an unchanged negative or both positive owners are settled."""
        if self.seen(key):
            return
        if self.complete:
            raise ShipError('closed receipt refresh already complete; retained')
        self._save({**self.data, 'observed': sorted([*self.data['observed'], key])})

    def finish(self):
        if not self.complete:
            self._save({**self.data, 'complete': True})
