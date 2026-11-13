"""Memory policy shared by direct pytest and the test tool; never used by production code.

Both limits are lowered before tests run. The hard limit survives fork, exec, multiprocessing
spawn and an emptied environment; an ordinary child cannot lift it again. This is a per-process
address-space bound, not an aggregate RSS budget for all descendants.
"""
from __future__ import annotations

import os
import resource

DEFAULT_MEMORY_MIB = 8192
MEMORY_ENV = 'TESTRUNS_MEMORY_MIB'
MIB = 1024 * 1024


def memory_mib(value) -> int:
    text = str(value)
    if not text.isascii() or not text.isdecimal() or not 1 <= int(text) <= DEFAULT_MEMORY_MIB:
        raise ValueError(f'test memory limit must be an integer from 1 to {DEFAULT_MEMORY_MIB} MiB')
    return int(text)


def requested_memory(environ=None) -> int:
    return memory_mib((os.environ if environ is None else environ).get(MEMORY_ENV, DEFAULT_MEMORY_MIB))


def apply_memory(mib: int) -> int:
    """Lower soft AND hard RLIMIT_AS, preserving a caller's tighter soft or hard limit."""
    requested = memory_mib(mib) * MIB
    previous = resource.getrlimit(resource.RLIMIT_AS)
    limit = min([requested, *(value for value in previous if value != resource.RLIM_INFINITY)])
    try:
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (OSError, ValueError) as error:
        raise RuntimeError(f'cannot establish test RLIMIT_AS memory limit of {limit} bytes: {error}') from error
    return limit


def apply_from_environment() -> int:
    return apply_memory(requested_memory())
