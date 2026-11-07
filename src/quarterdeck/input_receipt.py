"""Timestamped input receipts for exact transport, including mid-turn inputs.

The caller supplies an already owned record and its saved reader binding. No
candidate enumeration, source SQLite connection, or screen evidence is used.
"""

from datetime import datetime
from pathlib import Path

from .harness.json_record import RecordError, entries, when


def confirmed(name: str, record: Path, text: str, since: datetime) -> str | None:
    wanted = text.strip()
    if name == 'fake':
        from .harness.fake import FakeHarness
        name = FakeHarness._reader(record).__name__.rsplit('.', 1)[-1]
    try:
        if name == 'codex':
            return codex(record, wanted, since)
        if name == 'claude':
            return claude(record, wanted, since)
        if name == 'antigravity':
            return antigravity(record, wanted, since)
    except (TypeError, ValueError, AttributeError, KeyError) as error:
        raise RecordError('malformed input receipt fields') from error
    return None


def codex(record, wanted, since):
    for row in entries(record):
        payload = row.get('payload', {})
        if row.get('type') != 'response_item' or payload.get('role') != 'user':
            continue
        content = payload.get('content')
        if not isinstance(content, list):
            raise RecordError('Codex user content is not a list')
        text = '\n'.join(c.get('text', '') for c in content if c.get('type') == 'input_text')
        stamp = when(row.get('timestamp'))
        if stamp and stamp >= since and text.strip() == wanted:
            return 'delivered'
    return None


def claude(record, wanted, since):
    from .harness.claude import prompt_text, unwrap

    queued = []
    for row in entries(record):
        if row.get('isSidechain'):
            continue
        stamp = when(row.get('timestamp'))
        text = prompt_text(row)
        if text is not None and text.strip() == wanted and stamp and stamp >= since:
            return 'delivered'
        if row.get('type') != 'queue-operation' or unwrap(row.get('content') or '').strip() != wanted:
            continue
        operation = row.get('operation')
        if operation == 'enqueue':
            queued.append(stamp)  # retain stale/untimed entries too: removal is FIFO
        elif operation in ('remove', 'dequeue') and queued:
            enqueued = queued.pop(0)
            if enqueued and stamp and since <= enqueued <= stamp:
                return 'delivered'  # consumed into the running turn, no separate user row needed
        else:
            queued.clear()  # an unknown queue operation cannot prove current acceptance
    return 'queued' if any(t and t >= since for t in queued) else None


def antigravity(record, wanted, since):
    from .harness.antigravity_wire import _snapshot_query, _proto, _bytes, _str, _ts

    # Every USER_INPUT is evidence, even one followed by further inputs in the
    # same execution. Metadata field 1 is created_at, never an executor timestamp.
    steps = _snapshot_query(record)[3]
    for _, step_type, _, _, metadata, _, payload in steps:
        if step_type != 14:
            continue
        ui = _proto(_bytes(_proto(payload), 19))
        text = _str(ui, 2) or _str(ui, 1)
        stamp = _ts(_bytes(_proto(metadata), 1))
        if text is not None and stamp and stamp >= since and text.strip() == wanted:
            return 'delivered'
    return None
