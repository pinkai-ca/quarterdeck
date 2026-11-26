"""Claude's watch output evidence for attribution and passive status.

A background inbox command names its output file. Only an exact read of that file
with one recorded origin supplies a candidate envelope; the caller verifies the
whole envelope against immutable posts. Passive status reads only the task's
owned output artifact, after the adapter proves its measured absorption.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import shlex
from collections import defaultdict
from datetime import datetime

from . import record_file
from .claude_record import BACKGROUND_HEADER, WATCH_HEADER, inspect_entries
from .json_record import when
from .types import Input
from ..seat_command import watch_instruction

BACKGROUND = re.compile(r'^' + re.escape(BACKGROUND_HEADER) + r'\S+\. Output is being written to: (\S+)\. ')


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
    commands = ship_watch_commands(record)
    watches = {tuple(words(command)) for command in commands}
    footers = tuple('\n' + watch_instruction(command) for command in commands if command != 'qd inbox --wait')
    calls, called_at, ambiguous, results = {}, {}, set(), []
    for row in inspect_entries(record):
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
        for footer in footers:
            raw = raw.removesuffix(footer)
        if raw.startswith(WATCH_HEADER):
            found.append(Input(raw, at, 'watch'))
    return found


TASKS_TMP = Path('/tmp')
NATIVE_TAIL = b'[exited with code 0]\n'
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def artifact_matches(cwd, record, absorbed, proof):
    """Read just the path the launch, owned record, Bash result and notice agree on."""
    from .claude_background import native_task_id

    slug = str(cwd).replace('/', '-')
    if (str(cwd) != absorbed.cwd or not cwd.is_absolute() or record.parent.name != slug
            or not native_task_id(record.stem) or not native_task_id(absorbed.task)):
        return False
    uid = os.getuid()
    root = TASKS_TMP / f'claude-{uid}'
    parts = (slug, record.stem, 'tasks')
    path = root.joinpath(*parts, f'{absorbed.task}.output')
    if str(path) != absorbed.output:
        return False
    size = proof['size']
    raw = _read_artifact(root, parts, path, uid, size + len(NATIVE_TAIL))
    return (raw is not None and raw[size:] == NATIVE_TAIL
            and hashlib.sha256(raw[:size]).hexdigest() == proof['sha256'])


def _read_artifact(root, parts, path, uid, size):
    folder = os.open(root, DIRECTORY)
    try:
        info = os.fstat(folder)
        if info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o700:
            return None
        for part in parts:
            child = os.open(part, DIRECTORY, dir_fd=folder)
            os.close(folder)
            folder = child
            if os.fstat(folder).st_uid != uid:
                return None
        named = os.stat(path.name, dir_fd=folder, follow_symlinks=False)
        if (named.st_uid != uid or not stat.S_ISREG(named.st_mode) or named.st_nlink != 1
                or named.st_size != size):
            return None
        flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
        descriptor = os.open(path.name, flags, dir_fd=folder)
        try:
            opened = os.fstat(descriptor)
            if _stamp(opened) != _stamp(named):
                return None
            raw = record_file.read_from(descriptor, opened, path, size)
            current = os.stat(path.name, dir_fd=folder, follow_symlinks=False)
            if len(raw) == size and _stamp(opened) == _stamp(os.fstat(descriptor)) == _stamp(current):
                return raw
        finally:
            os.close(descriptor)
    finally:
        os.close(folder)
    return None


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mode, info.st_uid, info.st_nlink,
            info.st_mtime_ns, info.st_ctime_ns)



REGISTRATION_FIELDS = ('seat', 'generation', 'launch', 'watch', 'process_start',
                       'launch_at', 'launch_pid', 'launch_pane', 'launch_pool', 'launch_harness', 'previous')


def bindings_path(record):
    from .claude_background import native_task_id
    from .claude_children import launch_folder

    trusted = launch_folder(record)
    if trusted is not None and len(record.stem) <= 128 and native_task_id(record.stem):
        return trusted / 'watch-bindings' / record.stem
    return None


def _metadata(path, bound=4096):
    return record_file.read_bytes(path, bound)[0].decode('utf-8')


def _timestamp(value):
    moment = datetime.fromisoformat(value)
    if moment.utcoffset() is None:
        raise ValueError('untimed watch or launch evidence')
    return moment


def joined_launches(record):
    """Read the exact saved launch and its retained joins to this record, newest last."""
    from .claude_background import CALL_VERSIONS
    from .claude_children import launch_folder
    from .. import trusted_files

    folder = launch_folder(record)
    if folder is None:
        return None
    try:
        raw = _metadata(folder / trusted_files.LAUNCH)
        launch = json.loads(raw)
        if (launch['harness'] != 'claude' or launch['version'] not in CALL_VERSIONS
                or launch['resume'] not in (None, record.stem)
                or type(launch['pid']) is not int or launch['pid'] <= 0
                or re.fullmatch(r'%[0-9]+', launch['pane']) is None
                or not isinstance(launch['pool'], str) or not launch['pool']
                or _metadata(folder / trusted_files.RECORD).strip() != str(record)):
            return None
        at = _timestamp(launch['at'])
        history = {}
        journal = _metadata(folder / trusted_files.LAUNCHES, 1 << 20)
        for line in journal.splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get('record') != str(record):
                continue
            stamp = _timestamp(entry['at'])
            if (stamp in history or entry['harness'] != 'claude'
                    or not isinstance(entry['pool'], str) or not entry['pool']):
                return None
            history[stamp] = entry['pool']
        if (history and max(history) == at and history[at] == launch['pool']
                and _metadata(folder / trusted_files.LAUNCHES, 1 << 20) == journal
                and _metadata(folder / trusted_files.LAUNCH) == raw
                and _metadata(folder / trusted_files.RECORD).strip() == str(record)):
            return launch, sorted(history.items())
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        pass
    return None


def _watch_ended(pid, start):
    """Disappearance, exit or PID reuse proves this saved process cannot print again."""
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return fields[19] != start or fields[0] in ('Z', 'X', 'x')
    except FileNotFoundError:
        return True
    except (OSError, ValueError, IndexError):
        return False


def _no_offer(folder, data):
    generation = data['generation']
    if data['seat'] != folder.parent.parent.name or re.fullmatch(r'[0-9a-f]{32}', generation) is None:
        return False
    pid, _ = data['watch'].split()
    if not pid.isascii() or not pid.isdigit() or not 0 < int(pid) <= 2**31 - 1:
        return False
    start = data['process_start']
    if not isinstance(start, str) or re.fullmatch(r'[0-9]+', start) is None:
        return False
    marker = folder.parents[3] / 'post' / 'attempts' / f'.watch-{generation}-unoffered'
    if not _watch_ended(int(pid), start):
        return False   # a surviving original wait may still open one
    if marker.with_name(f'.watch-{generation}-offered').exists():
        return False
    return json.loads(_metadata(marker)) == {key: data[key] for key in REGISTRATION_FIELDS}


def scoped_stops(record, background, notices, history):
    """Return the individual stopped notices and native calls proved to have had no delivery."""
    from .claude_background import watch_registration_order

    folder = bindings_path(record)
    if not notices or folder is None:
        return set(), set()
    try:
        if not stat.S_ISDIR(folder.lstat().st_mode):
            return set(), set()
        by_call = defaultdict(list)
        for path in folder.glob('*.json'):
            data = json.loads(_metadata(path))
            if (type(data.get('format')) is not int or data['format'] != 1
                    or path.name != f"{data['generation']}.json"):
                return set(), set()
            by_call[data['call']].append(data)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return set(), set()

    launches = dict(history)
    successors = {old[0]: new[0] for old, new in zip(history, history[1:])}
    matches = defaultdict(list)
    for index, (at, call, identity) in enumerate(notices):
        task = background.tasks.get(call)
        if (at is None or task is None or identity != task.task or task.watch is None
                or not task.watch.exact_acknowledgement or task.invalid or task.undated_evidence
                or call in background.unknown_calls or task.phase not in ('pending', 'ended', 'ended-by-relaunch')):
            continue
        if len(by_call[call]) != 1:
            continue
        data = by_call[call][0]
        try:
            origin = _timestamp(data['launch_at'])
            registered = _timestamp(data['watch'].split()[1])
            successor = successors.get(origin)
            end = successors.get(successor)
            if (successor is None or at < successor or end is not None and at >= end
                    or data['launch_pool'] != launches[origin] or data['launch_harness'] != 'claude'
                    or data['task'] != identity
                    or type(data['launch_pid']) is not int or data['launch_pid'] <= 0
                    or re.fullmatch(r'%[0-9]+', data['launch_pane']) is None
                    or re.fullmatch(r'[0-9a-f]{64}', data['launch']) is None
                    or _timestamp(data['called_at']) != task.called_at
                    or _timestamp(data['acknowledged_at']) != task.acknowledged_at
                    or not origin <= task.called_at <= registered < successor or task.acknowledged_at >= successor
                    or not watch_registration_order(origin, task.called_at, registered, task.acknowledged_at,
                                                   data['previous'])
                    or task.latest >= successor and task.phase == 'pending'
                    or not _no_offer(folder, data)):
                continue
        except (OSError, ValueError, TypeError, KeyError, IndexError, RecursionError):
            continue
        matches[call].append(index)
    settled = {call: indexes[0] for call, indexes in matches.items() if len(indexes) == 1}
    return set(settled.values()), set(settled)
