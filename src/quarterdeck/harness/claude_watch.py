"""Recorded watch output consumed by a tool, for attribution only, never a receipt.

A background inbox command names its output file. Only an exact read of that file
with one recorded origin supplies a candidate envelope; the caller verifies the
whole envelope against immutable posts. No output file is opened here.
"""

import re
import shlex
from collections import defaultdict

from .claude_record import entries
from .json_record import when
from .types import Input

BACKGROUND = re.compile(r'^Command running in background with ID: \S+\. Output is being written to: (\S+)\. ')


def words(value):
    try:
        return shlex.split(value) if isinstance(value, str) else []
    except ValueError:
        return []


def text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(b, dict) and b.get('type') == 'text' for b in value):
        return '\n'.join(b.get('text', '') for b in value)
    return ''


def inputs(record):
    from .claude_background import ship_watch_commands
    # The watch by the bare 'qd' or by this ship's own absolute qd (the brief's), word for word; no other path.
    watches = {tuple(words(command)) for command in ship_watch_commands(record)}
    calls, called_at, ambiguous, results = {}, {}, set(), []
    for row in entries(record):
        if row.get('isSidechain'):
            continue
        message = row.get('message')
        if not isinstance(message, dict) or not isinstance(message.get('content'), list):
            continue
        for block in message['content']:
            if not isinstance(block, dict):
                continue
            key = block.get('id')
            if block.get('type') == 'tool_use' and message.get('role') == 'assistant' and isinstance(key, str):
                if key in calls and calls[key] != block:
                    ambiguous.add(key)
                calls[key] = block
                at = when(row.get('timestamp'))
                if at is not None:
                    called_at[key] = min(called_at[key], at) if key in called_at else at
            elif block.get('type') == 'tool_result' and message.get('role') == 'user' and not block.get('is_error'):
                results.append((block, when(row.get('timestamp'))))
    origins = defaultdict(dict)
    for block, at in results:
        key = block.get('tool_use_id')
        call = calls.get(key, {})
        data = call.get('input', {})
        if (key not in ambiguous and at is not None and key in called_at and called_at[key] <= at and call.get('name') == 'Bash' and isinstance(data, dict)
                and tuple(words(data.get('command'))) in watches):
            match = BACKGROUND.match(text(block.get('content')))
            if match:
                if at is not None:
                    prior = origins[match[1]].get(key)
                    origins[match[1]][key] = min(prior, at) if prior else at
    found = []
    for block, at in results:
        key = block.get('tool_use_id')
        call = calls.get(key, {})
        data = call.get('input', {})
        if key in ambiguous or at is None or key not in called_at or called_at[key] > at or not isinstance(data, dict):
            continue
        raw = text(block.get('content'))
        command = words(data.get('command'))
        direct = call.get('name') == 'Bash' and tuple(command) in watches
        path = command[1] if call.get('name') == 'Bash' and len(command) == 2 and command[0] == 'cat' else None
        numbered = call.get('name') == 'Read' and not (data.get('offset') or data.get('limit'))
        if numbered:
            path = data.get('file_path')
        sources = origins.get(path, {})
        if not direct and (len(sources) != 1 or next(iter(sources.values())) > at):
            continue
        if numbered:
            lines = raw.splitlines()
            rows = [re.fullmatch(r'\s*(\d+)[\t→](.*)', line) for line in lines]
            if not all(rows) or [int(row[1]) for row in rows] != list(range(1, len(rows)+1)):
                continue
            raw = '\n'.join(row[2] for row in rows)
        raw = raw.strip().removesuffix('\n[exited with code 0]').strip()
        if raw.startswith('qd >'):
            found.append(Input(raw, at, 'watch'))
    return found
