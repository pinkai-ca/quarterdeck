"""Bounded private tool evidence for a check's own pinned record, never receipts."""

from __future__ import annotations

from collections import OrderedDict
import json
import os
from pathlib import Path
import stat

MAX_PAIRS = 8
MAX_BYTES = 32768
MAX_READ = 262144


def unknown(reason='owned tool call/results unavailable') -> dict:
    return dict(status='unknown', reason=reason, pairs=[])


def _preview(value) -> tuple[str, bool]:
    text = json.dumps(value, ensure_ascii=True, separators=(',', ':'))
    return text[:1024], len(text) > 1024


def bounded(pairs, *, partial=False) -> dict:
    """Retain rendered fragments with explicit truncation, not invented output."""
    items = []
    for pair in list(pairs)[-MAX_PAIRS:]:
        input_known = pair.get('input_known') is True
        call, clipped = _preview(pair['input']) if input_known else (None, False)
        known = pair.get('result_known') is True
        result, cut = _preview(pair['result']) if known else (None, False)
        name = pair.get('name')
        identity = pair.get('id')
        items.append(dict(id=identity[:128] if isinstance(identity, str) else None,
                          name=name[:128] if isinstance(name, str) else None,
                          call=call, input_known=input_known, call_truncated=clipped,
                          result=result, result_known=known, result_truncated=cut,
                          failed=pair.get('failed') if type(pair.get('failed')) is bool else None))
        partial |= clipped or cut or not known or not input_known
    data = dict(status='partial' if partial else 'known',
                reason='recognized own-record tool call/results only', pairs=items,
                reading_limit=MAX_READ, pair_limit=MAX_PAIRS, byte_limit=MAX_BYTES)
    if not items:
        data.update(status='unknown', reason='no recognized tool call/result pair in bounded reading')
    while len(json.dumps(data, ensure_ascii=True).encode('utf-8')) > MAX_BYTES:
        data['pairs'].pop(0)
        data.update(status='partial', omitted_for_size=True)
    return data


def json_tools(record: Path, decode) -> dict:
    """Read a stable bounded JSONL tail; never follow another record or session."""
    descriptor = os.open(record, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            return unknown('owned record is not a regular JSONL file')
        offset = max(0, before.st_size - MAX_READ)
        os.lseek(descriptor, offset, os.SEEK_SET)
        raw = bytearray()
        while len(raw) <= MAX_READ:
            part = os.read(descriptor, min(65536, MAX_READ + 1 - len(raw)))
            if not part:
                break
            raw.extend(part)
        after = os.fstat(descriptor)
        stamp = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if stamp(before) != stamp(after) or len(raw) > MAX_READ:
            return unknown('owned record changed during bounded tool reading')
    finally:
        os.close(descriptor)
    lines = bytes(raw).splitlines()
    partial = bool(offset)
    if offset:
        lines = lines[1:]  # the first bounded fragment may start inside a row
    calls = OrderedDict()
    for line in lines:
        try:
            entry = json.loads(line)
            if not isinstance(entry, dict):
                partial = True
                continue
            for event in decode(entry):
                kind, identity, value = event
                if not isinstance(identity, str) or not identity:
                    partial = True
                    continue
                if kind == 'call':
                    partial |= identity in calls
                    calls[identity] = dict(value, id=identity, result_known=False)
                    calls.move_to_end(identity)
                    if len(calls) > MAX_PAIRS:
                        calls.popitem(last=False)
                        partial = True
                elif identity in calls:
                    calls[identity].update(value, result_known=True)
                else:
                    partial = True  # an unmatched result supplies no invented call
        except (ValueError, TypeError, KeyError, UnicodeError):
            partial = True
    return bounded(calls.values(), partial=partial)


def claude_tools(entry):
    message = entry.get('message')
    if not isinstance(message, dict) or not isinstance(message.get('content'), list):
        return
    for block in message['content']:
        if not isinstance(block, dict):
            continue
        if entry.get('type') == 'assistant' and block.get('type') == 'tool_use':
            yield 'call', block.get('id'), dict(name=block.get('name'), input=block.get('input'),
                                               input_known=isinstance(block.get('input'), dict))
        elif entry.get('type') == 'user' and block.get('type') == 'tool_result' and 'content' in block:
            if text_shape(block['content']):
                yield 'result', block.get('tool_use_id'), dict(result=block['content'], failed=block.get('is_error'))


def codex_tools(entry):
    item = entry.get('payload')
    if entry.get('type') != 'response_item' or not isinstance(item, dict):
        return
    kind = item.get('type')
    if kind in ('function_call', 'custom_tool_call'):
        yield 'call', item.get('call_id'), dict(name=item.get('name'),
                                              input=item.get('arguments') if kind == 'function_call' else None,
                                              input_known=kind == 'function_call' and isinstance(item.get('arguments'), str))
    elif kind in ('function_call_output', 'custom_tool_call_output') and 'output' in item:
        if text_shape(item['output'], kind='output_text'):
            yield 'result', item.get('call_id'), dict(result=item['output'], failed=None)


def pi_tools(entry):
    message = entry.get('message')
    if entry.get('type') != 'message' or not isinstance(message, dict):
        return
    if message.get('role') == 'assistant' and isinstance(message.get('content'), list):
        for block in message['content']:
            if isinstance(block, dict) and block.get('type') == 'toolCall':
                yield 'call', block.get('id'), dict(name=block.get('name'), input=block.get('arguments'),
                                                   input_known=isinstance(block.get('arguments'), dict))
    elif message.get('role') == 'toolResult' and 'content' in message:
        if text_shape(message['content']):
            yield 'result', message.get('toolCallId'), dict(result=message['content'], failed=message.get('isError'))


def text_shape(value, *, kind='text') -> bool:
    return isinstance(value, str) or (isinstance(value, list) and bool(value) and all(
        isinstance(item, dict) and set(item) <= {'type', 'text'}
        and item.get('type') == kind and isinstance(item.get('text'), str) for item in value))


def opencode_tools(record: Path) -> dict:
    """Known own-root inputs only; this version's output/error shapes are unproved."""
    from .opencode_record import _Rows, _active_session
    sizes = [record.stat().st_size]
    wal = Path(str(record) + '-wal')
    if wal.exists():
        sizes.append(wal.stat().st_size)
    if sum(sizes) > 32 * 1024 * 1024:
        return unknown('owned SQLite tool reading exceeds 32 MiB bound')
    rows = _Rows(record)
    root = _active_session(rows)
    if root is None:
        return unknown('owned SQLite root session unknown')
    pairs = []
    for identity, _, session, _, _, part in rows.parts:
        state = part.get('state')
        if session != root or part.get('type') != 'tool' or not isinstance(state, dict):
            continue
        pairs.append(dict(id='part:' + str(identity), name=part.get('tool'), input=state.get('input'),
                          input_known=isinstance(state.get('input'), dict), result_known=False,
                          failed=True if state.get('status') == 'error' else None))
    result = bounded(pairs, partial=True)
    result['reason'] = 'own-root tool inputs retained; native output/error shape unproved'
    return result
