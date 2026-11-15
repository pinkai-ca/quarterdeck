"""A seat's successful full-body thread readings, kept in its own writable folder.

The post is read-only in a jail. This file records reading, without changing an
attempt's transport verdict. Writers merge under a seat-local lock and replace
the file whole; readers need no lock. Missing or unreadable evidence settles no work.
"""

from __future__ import annotations

from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import time

from . import durable, post_dir
from .ship import ShipError

NAME = 'thread-reads.json'
LIMIT = 16 << 20
WAIT = 2.0  # only this seat's CLI writers share the lock; service readers never take it


def path(runtime: Path, seat: str) -> Path:
    if not seat or Path(seat).name != seat or seat in ('.', '..'):
        raise ShipError('invalid thread reader seat')
    return runtime / 'seats' / seat / NAME


def _load(target: Path, seat: str) -> dict[str, str]:
    try:
        parent = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            raw = post_dir._read_nofollow(parent, target.name, target, LIMIT)
        finally:
            os.close(parent)
        data = json.loads(raw)
    except FileNotFoundError:
        return {}
    if (not isinstance(data, dict) or data.get('format') != 1 or data.get('seat') != seat
            or not isinstance(data.get('read'), dict)
            or not all(isinstance(key, str) and isinstance(value, str) for key, value in data['read'].items())):
        raise ValueError('invalid thread read receipts')
    return data['read']


def read(runtime: Path, seat: str) -> frozenset[str]:
    try:
        return frozenset(_load(path(runtime, seat), seat))
    except (OSError, ValueError, TypeError, ShipError):
        return frozenset()  # unknown reading evidence cannot suppress work


def record(runtime: Path, seat: str, identifiers) -> None:
    """Receipt only after complete output and flush; repeated reads write nothing."""
    identifiers = frozenset(identifiers)
    if not identifiers or identifiers <= read(runtime, seat):
        return
    target = path(runtime, seat)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(target.with_suffix('.lock'),
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        deadline = time.monotonic() + WAIT
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ShipError('thread read receipt writer busy; body printed, reading not receipted') from None
                time.sleep(0.02)
        try:
            received = _load(target, seat)
        except (ValueError, TypeError) as error:
            raise ShipError('thread read receipts unreadable; retained for inspection') from error
        missing = identifiers - received.keys()
        if not missing:
            return
        moment = datetime.now().astimezone().isoformat()
        received.update((identifier, moment) for identifier in sorted(missing))
        text = json.dumps(dict(format=1, seat=seat, read=received), indent=2) + '\n'
        if len(text.encode('utf-8')) > LIMIT:
            raise ShipError('thread read receipts exceed their size bound; retained for inspection')
        durable.replace_file(target, text)
    finally:
        os.close(descriptor)
