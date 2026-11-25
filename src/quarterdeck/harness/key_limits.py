"""A provider refusing a key's work for spend or rate, in the forms measured on the real programs and no others.

Measured on 21 November 2026, each program against a local server answering with its provider's documented refusal:

- Claude Code 2.1.317: an assistant row with ``isApiErrorMessage`` and ``error`` ``billing_error`` (spend) or
  ``rate_limit`` (rate).
- Codex 0.166.0: a turn failure with the code ``usage_limit_exceeded`` (spend), or, for rate, with no code and the
  words ``exceeded retry limit, last status: 429``.

- OpenCode 1.19.5 on Z.ai: the owning assistant's completed APIError retains HTTP 429 and business code 1113.
  Claude Code and pi omit the Z.ai business code in the recorded refusal.

A refusal holds the pool
for a bounded time and is then tried again, since a credit can be topped up and a rate window passes. No record reader
needs this module, and it is in no reader's stamp set.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

from .limits import Wall

SPEND, RATE = 'spend', 'rate'
ZAI_EVENT_PREFIX = 'key-refusal:opencode:zai-balance:'
HOLD = {SPEND: timedelta(hours=1), RATE: timedelta(minutes=5)}
TAIL_BYTES = 256 * 1024
CLAUDE_ERRORS = {'billing_error': SPEND, 'rate_limit': RATE}
CODEX_SPEND = 'usage_limit_exceeded'
CODEX_RATE = 'exceeded retry limit, last status: 429'


def _when(value) -> datetime | None:
    try:
        at = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (AttributeError, ValueError):
        return None
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def claude_form(record: Path) -> tuple[str, datetime] | None:
    """The refusal in the last assistant row of the record, if that row is one: a later answer ends it."""
    try:
        with open(record, 'rb') as stream:
            stream.seek(max(os.fstat(stream.fileno()).st_size - TAIL_BYTES, 0))
            lines = stream.read().split(b'\n')
    except OSError:
        return None
    for line in reversed(lines):
        if b'"assistant"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get('type') != 'assistant':
            continue
        form = CLAUDE_ERRORS.get(entry.get('error')) if entry.get('isApiErrorMessage') is True else None
        at = _when(entry.get('timestamp'))
        return (form, at) if form and at else None
    return None


def codex_form(failure) -> tuple[str, datetime] | None:
    if failure is None or failure.at is None:
        return None
    if failure.code == CODEX_SPEND:
        return SPEND, failure.at
    if failure.code is None and isinstance(failure.message, str) and failure.message.startswith(CODEX_RATE):
        return RATE, failure.at
    return None


def refusal(seat) -> Wall | None:
    """The wall this seat's own record proves, or None."""
    name = seat.harness.name
    if name == 'claude':
        record = seat.record()
        found = claude_form(record) if record is not None else None
    elif name == 'codex':
        from ..turn_failure import current
        found = codex_form(current(seat))
    else:
        return None
    if found is None:
        return None
    form, at = found
    key = f'key-refusal:{name}:{form}:{at.isoformat()}'
    return Wall(key, at, at + HOLD[form], f'its provider refused the key for {form}', signature=key)


def zai_outcome(seat, launched):
    """The joined OpenCode record can prove a balance refusal or a later accepted request."""
    from .opencode_record import last_request
    from ..key_proof import ZAI_EXHAUSTED, zai_balance_response

    if seat.harness.name != 'opencode' or (record := seat.record()) is None or seat.launched() != launched:
        return None, None
    found = last_request(record, launched.pool, datetime.fromisoformat(launched.at))
    if found is None or seat.launched() != launched:
        return None, None
    at, status, raw = found
    if status is None:
        return None, at
    if status == 429 and zai_balance_response(raw):
        key = f'{ZAI_EVENT_PREFIX}{at.isoformat()}'
        return Wall(key, at, at + HOLD[SPEND], ZAI_EXHAUSTED, signature=key), None
    return None, None
