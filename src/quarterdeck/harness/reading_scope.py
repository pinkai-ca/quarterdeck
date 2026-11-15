"""Native results reused only inside one read-only presentation."""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import copy
from pathlib import Path
import time

_READING = ContextVar('native_presentation_reading', default=None)
_TIMESTAMP_TICK_NS = 2_000_000_000


@contextmanager
def reading_pass():
    if _READING.get() is not None:
        yield
        return
    token = _READING.set({})
    try:
        yield
    finally:
        _READING.reset(token)


def _copy(result):
    # Readers already share their immutable turn objects; keep public containers private.
    return copy(result[0]), copy(result[1])


def read(owner, record, load):
    cache = _READING.get()
    if cache is None:
        return load()
    from .base import record_fingerprint
    fingerprint = getattr(owner, 'reading_fingerprint', record_fingerprint)
    record = Path(record)
    key = owner, record
    previous = cache.pop(key, None)
    started = time.time_ns()
    try:
        before = fingerprint(record)
        journal = Path(f'{record}-journal').exists()
    except OSError:
        return load()  # retain the native reader's own failure and diagnostics
    checked = time.time_ns()
    if (not journal and previous is not None and previous[0] == before
            and previous[1] <= started <= checked):
        cache[key] = previous
        return _copy(previous[2])
    result = load()
    try:
        stable = before == fingerprint(record) and not Path(f'{record}-journal').exists()
    except OSError:
        stable = False
    finished = time.time_ns()
    latest = max((part[index] for part in before if part[0] for index in (3, 5)), default=finished)
    if (before[0][0] and not journal and stable
            and latest + _TIMESTAMP_TICK_NS < started <= checked <= finished):
        cache[key] = before, started, _copy(result)
    return result
