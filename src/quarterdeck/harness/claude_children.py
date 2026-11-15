"""Measured Claude Code 2.1.325 child execution, joined to its root conversation.

A child file proves starts, never an end merely because its parent returned.
The root's native notices settle its background Bash tasks. Every state reading
re-reads child evidence, including a child which changed without a root write.
"""
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
import json
from pathlib import Path
import re
import stat

from .claude_background import BackgroundBash
from .json_record import object_lines, when
from .types import ActiveTool

VERSION = '2.1.325'
ENDS = frozenset({'end_turn'})  # the explicit child return measured on this version
FINISHED = frozenset({'ended', 'ended-by-relaunch'})


def child_paths(record):
    record = Path(record)
    parent = record.with_suffix('')
    folder = parent / 'subagents'
    for path in (parent, folder):
        try:
            if not stat.S_ISDIR(path.lstat().st_mode):
                raise OSError('child directory is not a native directory')
        except FileNotFoundError:
            return []
    paths = sorted(folder.glob('agent-*.jsonl'))
    for path in paths:
        if (re.fullmatch(r'agent-[A-Za-z0-9_-]+\.jsonl', path.name) is None
                or not stat.S_ISREG(path.lstat().st_mode)):
            raise OSError('child record is not a native file')
    return paths


def seat_folder(record):
    record = Path(record)
    home = record.parent.parent.parent
    return (home.parent if record.is_absolute() and home.name == 'home'
            and home.parent.parent.name == 'seats' and home.parent.parent.parent.name == 'runtime'
            and record.parent.parent.name == 'projects' else None)


def current_launch(record):
    """Only this joined record's current, measured resumed launch can end old child processes."""
    folder = seat_folder(record)
    if folder is None:
        return None
    try:
        def text(name):
            path = folder / name
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError('launch evidence is not regular')
            return path.read_text()
        data = json.loads(text('launch.json'))
        if (data.get('harness') != 'claude' or data.get('version') != VERSION
                or data.get('resume') != Path(record).stem or type(data.get('pid')) is not int
                or data['pid'] <= 0 or text('record').strip() != str(record)):
            return None
        at = datetime.fromisoformat(data['at'].replace('Z', '+00:00'))
        expected = dict(record=str(record), at=data.get('at'), pool=data.get('pool'), harness='claude')
        joined = [json.loads(line) for line in text('record-launches.jsonl').splitlines() if line.strip()]
        if at is not None and at.tzinfo is not None and any(
                isinstance(row, dict) and all(row.get(key) == value for key, value in expected.items())
                for row in joined):
            return at
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def fingerprint(record):
    from .base import record_fingerprint
    record = Path(record)
    paths = [record, record.with_suffix(''), record.with_suffix('') / 'subagents', *child_paths(record)]
    if folder := seat_folder(record):
        paths.extend(folder / name for name in ('launch.json', 'record', 'record-launches.jsonl'))
    return tuple(part for path in paths for part in record_fingerprint(path))


def blocks(row, kind):
    content = row.get('message', {}).get('content')
    return [block for block in content if isinstance(block, dict) and block.get('type') == kind] if isinstance(content, list) else []


@dataclass
class Agent:
    name: str
    started: object
    child: str | None = None
    invalid: bool = False


class Leaf:
    def __init__(self, path, session, ignored, title_valid):
        self.background = BackgroundBash()
        self.call_times = {}
        self.live = {}
        self.known = True
        self.open = None
        self.ended_at = None
        self.started = None
        self.queued = []
        agent = path.stem.removeprefix('agent-')
        skipped = []
        source = object_lines(path, lambda *item: skipped.append(item))
        if skipped:
            self.known = False
        for _, row in source:
            kind = row.get('type')
            if kind in ('assistant', 'user'):
                if (row.get('sessionId') != session or row.get('agentId') != agent
                        or row.get('isSidechain') is not True or row.get('version') != VERSION
                        or not isinstance(row.get('message'), dict) or row['message'].get('role') != kind):
                    self.known = False
                    continue
            at = when(row.get('timestamp'))
            if kind == 'assistant':
                if not isinstance(row['message'].get('content'), list):
                    self.known = False
                    continue
                self.open = True
                self.started = self.started or at
                for block in blocks(row, 'tool_use'):
                    call, name, args = block.get('id'), block.get('name'), block.get('input')
                    if (not isinstance(call, str) or not call or not isinstance(name, str)
                            or not name or call in self.live or not isinstance(args, dict) or at is None):
                        self.known = False
                        continue
                    if name in ('Agent', 'Task') or name != 'Bash' and args.get('run_in_background'):
                        self.known = False  # nested delegation is not a measured join
                    self.live[call] = ActiveTool(name, at)
                    self.call_times[call] = at
                    self.background.call(call, name, args, at, row.get('version'))
                if row.get('message', {}).get('stop_reason') in ENDS:
                    if at is None:
                        self.known = False
                    else:
                        self.open, self.ended_at = False, at
            elif kind == 'user':
                self.open = True
                self.started = self.started or at
                for block in blocks(row, 'tool_result'):
                    call = block.get('tool_use_id')
                    self.background.result(call, row, block, at)
                    self.live.pop(call, None)
            elif kind == 'queue-operation':
                text = row.get('content')
                if row.get('operation') == 'enqueue' and isinstance(text, str):
                    self.queued.append(text)
                elif row.get('operation') == 'remove' and isinstance(text, str) and text in self.queued:
                    self.queued.remove(text)
                else:
                    self.known = False
            elif kind == 'attachment':
                value = row.get('attachment')
                if isinstance(value, dict) and value.get('type') == 'task_status' and value.get('taskType') == 'local_bash':
                    self.background.running(value, at, row.get('version'))
            elif kind == 'ai-title' and not title_valid(row):
                self.known = False
            elif kind not in ('system', *ignored):
                self.known = False
        if self.background.unknown_calls or self.background.unbound:
            self.known = False


class Children:
    def __init__(self, record):
        self.record = Path(record)
        self.sessions = set()
        self.agents = {}
        self.unknown = False
        self.tasks = {}  # the last projected task phases, including ended-by-relaunch

    def observe(self, row):
        if row.get('type') in ('assistant', 'user') and not row.get('isSidechain'):
            session = row.get('sessionId')
            if isinstance(session, str) and session:
                self.sessions.add(session)

    def call(self, call, name, args, at, version):
        if name not in ('Agent', 'Task'):
            return False
        # Agent's async form and child layout are measured here; older Task forms stay opaque.
        supported = (name == 'Agent' and version == VERSION and isinstance(call, str) and bool(call)
                     and isinstance(args, dict) and at is not None and call not in self.agents)
        if supported:
            self.agents[call] = Agent(name, at)
        else:
            self.unknown = True
        return supported

    def result(self, call, row, block, at):
        agent = self.agents.get(call)
        if agent is None:
            return
        value = row.get('toolUseResult')
        child = value.get('agentId') if isinstance(value, dict) else None
        if (row.get('version') == VERSION and isinstance(value, dict) and value.get('isAsync') is True
                and value.get('status') == 'async_launched' and isinstance(child, str)
                and re.fullmatch(r'[A-Za-z0-9_-]+', child) is not None and not block.get('is_error', False)
                and at is not None and at >= agent.started and agent.child is None):
            agent.child = child
        else:
            agent.invalid = True

    def project(self, state, background, ignored, title_valid, *, launched_at=None):
        """A fresh view: never mutate the root reader's cached tasks or buffered notices."""
        self.tasks = {}
        try:
            paths = child_paths(self.record)
            if not paths and not self.agents and not self.unknown:
                return background.state(state, launched_at=launched_at)
            if self.sessions != {self.record.stem}:
                return replace(background.state(state, launched_at=launched_at), pending_tools=None)
            leaves = {path.stem.removeprefix('agent-'): Leaf(path, self.record.stem, ignored, title_valid)
                      for path in paths}
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            return replace(background.state(state, launched_at=launched_at), pending_tools=None)
        view = deepcopy(background)
        unknown = self.unknown
        ownership = {}
        relaunched = current_launch(self.record)
        for identity, leaf in leaves.items():
            unknown = unknown or not leaf.known
            for call, task in leaf.background.tasks.items():
                if call in view.tasks or task.task in view.task_calls:
                    unknown = True
                    continue
                ownership[call] = identity
                task.inbox_watch_started = None  # a child's watch is never this seat's resting watch
                view.tasks[call] = task
                view.task_calls[task.task] = call
                for early in view.early.pop(call, ()):
                    view.notice(task, *early)
                if (task.phase == 'pending' and not task.invalid and relaunched is not None
                        and leaf.call_times.get(call) is not None and leaf.call_times[call] < relaunched):
                    task.phase, task.terminal, task.latest = 'ended-by-relaunch', 'relaunch', relaunched
        self.tasks = view.tasks
        extra = []
        associated = set()
        for call, agent in self.agents.items():
            if agent.invalid:
                unknown = True
            if agent.child is None:
                continue  # its native root call is still pending until the launch result
            associated.add(agent.child)
            leaf = leaves.get(agent.child)
            settled = (leaf is not None and leaf.known and leaf.open is False and not leaf.live
                       and not leaf.queued and leaf.ended_at is not None and leaf.ended_at >= agent.started)
            if leaf is not None:
                tasks = [task for key, task in view.tasks.items() if ownership.get(key) == agent.child]
                settled = settled and all(task.phase in FINISHED and not task.invalid for task in tasks)
                # A first return can be interim: the callback wakes the child after its command ends.
                terminal_times = [task.latest for task in tasks if task.phase == 'ended']
                settled = settled and (not terminal_times or leaf.ended_at >= max(terminal_times))
            if not settled:
                extra.append(ActiveTool(agent.name, agent.started))
        for identity, leaf in leaves.items():
            extra.extend(leaf.live.values())
            if identity not in associated and (leaf.open is not False or leaf.queued):
                extra.append(ActiveTool('Agent', leaf.started))
        projected = view.state(state, launched_at=launched_at)
        return replace(projected,
                       pending_tools=None if projected.pending_tools is None or unknown else
                       projected.pending_tools + tuple(tool.name for tool in extra),
                       active_tools=projected.active_tools + tuple(extra))
