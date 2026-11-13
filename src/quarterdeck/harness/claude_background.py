"""Claude Code root Bash task evidence, independent of turn accounting.

Only measured structural fields are joined. Notification summaries and command/output
text supply no execution proof. Unknown evidence is never a record-wide reset.
Each evidence form trusts only the exact versions measured to record it the same way;
an unmeasured version, including any later one, stays unknown.
"""

from dataclasses import dataclass, replace
from datetime import datetime
import re

from .types import ActiveTool


VERSION = '2.1.317'  # the first version measured for every form below
MEASURED = frozenset({VERSION, '2.1.322'})  # 2.1.322 measured the same, form for form, where it appears below
CALL_VERSIONS = MEASURED  # a root background Bash call and its joined result row
STATUS_VERSIONS = MEASURED  # a running local_bash task_status attachment
STOP_VERSIONS = frozenset({VERSION})  # TaskStop call and result: no 2.1.322 instance measured
NOTICE_VERSIONS = {  # a task notification, by its carrier
    ('queue-operation', 'enqueue'): MEASURED,
    ('queue-operation', 'remove'): frozenset({VERSION}),  # no 2.1.322 remove carrier measured
    ('user', 'task-notification'): MEASURED,
    ('attachment', 'queued_command'): frozenset({VERSION}),  # no 2.1.322 attachment carrier measured
}
TERMINAL = frozenset({'completed', 'failed', 'killed'})


def notification(text):
    """Read only the tag envelope and header, excluding the free-text summary."""
    if not isinstance(text, str):
        return None
    match = re.fullmatch(r'\s*<task-notification>(.*)</task-notification>\s*', text, re.S)
    if match is None:
        return None
    header, separator, rest = match[1].partition('<summary>')
    _, ending, tail = rest.rpartition('</summary>')
    if not separator or not ending:
        return None
    fields = {}
    pattern = re.compile(r'\s*<(task-id|tool-use-id|output-file|status|note)>([^<>]*)</\1>', re.S)
    while header.strip():
        field = pattern.match(header)
        if field is None or field[1] in fields:
            return None
        fields[field[1]] = field[2]
        header = header[field.end():]
    if tail.strip():
        note = re.fullmatch(r'\s*<note>.*</note>\s*', tail, re.S)
        if note is None or 'note' in fields:
            return None
        fields['note'] = True
    if not all(isinstance(fields.get(key), str) and fields[key] for key in ('task-id', 'tool-use-id', 'status')):
        return None
    return fields['tool-use-id'], fields['task-id'], fields['status'], 'note' in fields


@dataclass
class Task:
    call: str
    task: str
    started: datetime
    latest: datetime
    phase: str = 'pending'
    terminal: str | None = None
    invalid: bool = False
    inbox_watch_started: datetime | None = None


class BackgroundBash:
    def __init__(self):
        self.calls = {}
        self.tasks = {}  # includes terminal identities so later duplicate carriers can be joined
        self.task_calls = {}
        self.unknown_calls = set()
        self.unbound = False
        # Notifications met before their call's own rows, by root call id, in file order. When the inbox watch
        # returns at once, Claude Code writes the notification's enqueue first. Each binds when its call's result
        # creates the task; one still waiting at the record's end leaves the record's tools unknown.
        self.early = {}
        self.version = None  # queue carriers inherit the nearest preceding explicit record version

    def call(self, call, name, arguments, at, version):
        if name not in ('Bash', 'TaskStop'):
            return
        if not isinstance(call, str) or not call or not isinstance(arguments, dict):
            self.unbound = True
            return
        if call in self.calls or call in self.tasks:
            self.unknown_calls.add(call)
        wanted = arguments.get('run_in_background', False) if name == 'Bash' else arguments.get('task_id')
        watch = name == 'Bash' and arguments.get('command') == 'qd inbox --wait'
        self.calls[call] = name, wanted, at, version, watch
        if name == 'Bash' and (not isinstance(wanted, bool) or wanted and version not in CALL_VERSIONS):
            self.unknown_calls.add(call)

    def result(self, call, entry, block, at):
        info = self.calls.pop(call, None)
        if info is None:
            return
        name, wanted, called_at, version, watch = info
        result = entry.get('toolUseResult')
        allowed = CALL_VERSIONS if name == 'Bash' else STOP_VERSIONS
        supported = version in allowed and entry.get('version') == version
        if name == 'Bash':
            task = result.get('backgroundTaskId') if isinstance(result, dict) else None
            advertised = isinstance(result, dict) and 'backgroundTaskId' in result
            if not advertised and not wanted:
                return  # ordinary foreground completion remains the ordinary reader's evidence
            if (not supported or not isinstance(task, str) or not task or block.get('is_error', False)
                    or at is None or called_at is None or at < called_at
                    or call in self.tasks or task in self.task_calls):
                self.unknown_calls.add(call)
                return
            self.tasks[call] = Task(call, task, at, at,
                                    inbox_watch_started=called_at if watch else None)
            self.task_calls[task] = call
            for early in self.early.pop(call, ()):
                self.notice(self.tasks[call], *early)
            return
        # A failed stop ends only the TaskStop invocation, never the Bash task.
        if block.get('is_error', False):
            return
        if (not supported or not isinstance(result, dict) or result.get('task_type') != 'local_bash'
                or not isinstance(result.get('message'), str)
                or not result['message'].startswith('Successfully stopped')
                or result.get('task_id') != wanted or at is None or called_at is None or at < called_at):
            self.unbound = True
            return
        target = self.task_calls.get(wanted) if isinstance(wanted, str) else None
        if target is None:
            self.unbound = True
            return
        self.end(self.tasks[target], 'task-stop', at)

    def end(self, task, status, at):
        if at is None or at < task.latest or task.invalid:
            task.phase, task.invalid = 'unknown', True
        elif task.phase == 'ended' and task.terminal != status:
            task.phase, task.invalid = 'unknown', True
        elif task.phase != 'ended':
            task.phase, task.terminal, task.latest = 'ended', status, at
        # Matching duplicate carriers keep the first terminal time.

    def observe(self, entry, at):
        """Native carriers only. Wire request copies and sidechain rows never enter this path."""
        if 'isSidechain' in entry and entry['isSidechain'] is not False:
            self.unbound = True
            return
        kind = entry.get('type')
        if 'version' in entry:
            self.version = entry['version']
        version = self.version if kind == 'queue-operation' else entry.get('version')
        text, notice, carrier = None, False, None
        if kind == 'queue-operation' and entry.get('operation') in ('enqueue', 'remove'):
            carrier = ('queue-operation', entry['operation'])
            text = entry.get('content')
            notice = isinstance(text, str) and text.lstrip().startswith('<task-notification>')
        elif kind == 'user' and isinstance(entry.get('origin'), dict) and entry['origin'].get('kind') == 'task-notification':
            carrier = ('user', 'task-notification')
            text = entry.get('message', {}).get('content')
            notice = True
        elif kind == 'attachment' and isinstance(entry.get('attachment'), dict):
            attachment = entry['attachment']
            if attachment.get('type') == 'task_status' and attachment.get('taskType') == 'local_bash':
                self.running(attachment, at, version)
                return
            if attachment.get('type') == 'queued_command' and attachment.get('commandMode') == 'task-notification':
                carrier = ('attachment', 'queued_command')
                origin = attachment.get('origin')
                notice = isinstance(origin, dict) and origin.get('kind') == 'task-notification'
                text = attachment.get('prompt')
                if not notice:
                    self.unbound = True
        if not notice:
            return
        fields = notification(text)
        if fields is None:
            self.unbound = True
            return
        call, identity, status, note = fields
        task = self.tasks.get(call)
        if task is None:
            # Held until this root call's own result arrives later in the file. A subagent file's call never
            # arrives here, so a main-file notification still cannot establish a start it owns.
            self.early.setdefault(call, []).append((identity, status, note, at, version, carrier))
            return
        self.notice(task, identity, status, note, at, version, carrier)

    def notice(self, task, identity, status, note, at, version, carrier):
        if (task.task != identity or version not in NOTICE_VERSIONS.get(carrier, ())
                or at is None or at < task.started):
            task.phase, task.invalid = 'unknown', True
        elif status in TERMINAL and not note:
            self.end(task, status, at)
        else:
            # In particular, resume's stopped+note never says the old command ended.
            task.phase = 'unknown'
            task.latest = max(at, task.latest)

    def running(self, attachment, at, version):
        identity = attachment.get('taskId')
        target = self.task_calls.get(identity) if isinstance(identity, str) else None
        if target is None:
            self.unbound = True
            return
        task = self.tasks[target]
        if (version not in STATUS_VERSIONS or attachment.get('status') != 'running' or at is None
                or at < task.latest or task.phase == 'ended' or task.invalid):
            task.phase, task.invalid = 'unknown', True
        else:
            task.phase, task.latest = 'pending', at

    def state(self, state):
        active = [task for task in self.tasks.values() if task.phase != 'ended']
        unknown = (self.unbound or self.early or self.unknown_calls
                   or any(task.phase == 'unknown' for task in active))
        return replace(state,
                       pending_tools=None if state.pending_tools is None or unknown else
                       state.pending_tools + tuple('Bash' for task in active),
                       active_tools=state.active_tools + tuple(
                           ActiveTool('Bash', task.started, task.inbox_watch_started) for task in active))
