"""Claude Code root Bash task evidence, independent of turn accounting.

Only measured structural fields are joined. The own-watch carriers also match
their exact native text against the acknowledged watch. Unknown evidence is
never a record-wide reset.
Each evidence form trusts only the exact versions measured to record it the same way;
an unmeasured version, including any later one, stays unknown.
"""

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
import re

from .claude_record import BACKGROUND_HEADER
from .json_record import when
from .types import ActiveTool
from ..seat_command import qd_command


VERSION = '2.1.317'  # the first version measured for every form below
LINKED_VERSION = '2.1.331'
MEASURED = frozenset({VERSION, '2.1.322'})  # 2.1.322 measured the same, form for form, where it appears below
CALL_VERSIONS = MEASURED | {'2.1.325', LINKED_VERSION}
STATUS_VERSIONS = MEASURED | {'2.1.325'}  # running local_bash restoration measured on 2.1.325 too
ERROR_VERSIONS = frozenset({VERSION})  # native failed background invocation with a string result
EXIT_VERSIONS = frozenset({VERSION, '2.1.325'})  # own root/child process death measured across program exit
STOP_VERSIONS = frozenset({VERSION, '2.1.325', LINKED_VERSION})
CHILD_COMPLETION = ('queue-operation', 'delivered_to_agent')
NOTICE_VERSIONS = {  # a task notification, by its carrier
    ('queue-operation', 'enqueue'): MEASURED | {'2.1.325'},
    ('queue-operation', 'remove'): frozenset({VERSION, '2.1.325'}),  # no 2.1.322 remove carrier measured
    ('user', 'task-notification'): MEASURED | {'2.1.325'},
    ('attachment', 'queued_command'): frozenset({VERSION, '2.1.325'}),  # no 2.1.322 attachment carrier measured
    CHILD_COMPLETION: frozenset({LINKED_VERSION}),
}
TERMINAL = frozenset({'completed', 'failed', 'killed'})
# The Monitor tool's own notices, measured on 2.1.317 only, on every notice carrier: its call, its result
# (toolUseResult {persistent, taskId, timeoutMs}) and an envelope whose summary is followed by exactly one <event>,
# in two forms: task-id alone while it runs, or task-id, tool-use-id, output-file and status when it ends.
MONITOR_VERSIONS = frozenset({VERSION})
# Claude Code's own note on a killed task when it stopped the command itself, under memory pressure while the
# session was idle: measured byte for byte the same on every instance, it says the command ended, so the kill ends
# the task. Measured on Claude Code 2.1.317 only, on two carriers: the queue enqueue and the user task-notification
# row. Any other wording, version or carrier stays unknown.
REAPED = (
    'This is not a failure of the command. Claude Code stopped it because the system was critically low on memory'
    ' while the session was idle, which says nothing about the command or its own memory use, so there is nothing'
    ' in it to debug. Do not start it again on your own, even if the work seems to need it: memory may still be'
    ' short. Report what was stopped and why, and start it again only when asked. The user can turn this behavior'
    ' off by starting Claude Code with CLAUDE_CODE_DISABLE_BG_SHELL_PRESSURE_REAP=1 in its environment; setting it'
    ' from a shell command has no effect.'
)
REAPED_VERSIONS = {  # the killed notice with REAPED, by its carrier
    ('queue-operation', 'enqueue'): frozenset({VERSION}),
    ('user', 'task-notification'): frozenset({VERSION}),
}


# The single-shell resume emitters measured on 2.1.317 and 2.1.325 write these exact notes. They describe
# the prior process, never prove that a command ended. Only relaunch admission uses them.
RESUME_STOPPED = (
    'No completion record was found for it in the previous session. It may have been stopped'
    ' (via the UI, Monitor timeout, or agent teardown — these leave no transcript marker),'
    ' or it may have been running when the previous Claude Code process exited.'
)
RESUME_NOTES = frozenset({RESUME_STOPPED, RESUME_STOPPED +
    ' Check the output file for partial results before assuming it completed.'})
RESUME_VERSIONS = {
    ('queue-operation', 'enqueue'): frozenset({VERSION, '2.1.325'}),
    ('user', 'task-notification'): frozenset({VERSION, '2.1.325'}),
}


HEADER = re.compile(r'\s*<(task-id|tool-use-id|output-file|status|note)>([^<>]*)</\1>', re.S)


def _header(header):
    fields = {}
    while header.strip():
        field = HEADER.match(header)
        if field is None or field[1] in fields:
            return None
        fields[field[1]] = field[2]
        header = header[field.end():]
    return fields


def monitor_event(text):
    """The Monitor tool's measured event notice: (tool-use-id or None, task-id, status or None), else None.
    Only the envelope's own fields are read; the summary and the event's text never are."""
    if not isinstance(text, str):
        return None
    match = re.fullmatch(r'\s*<task-notification>(.*)</task-notification>\s*', text, re.S)
    if match is None:
        return None
    header, separator, rest = match[1].partition('<summary>')
    _, ending, tail = rest.rpartition('</summary>')
    if not separator or not ending or re.fullmatch(r'\s*<event>(?:(?!<event>).)*</event>\s*', tail, re.S) is None:
        return None
    fields = _header(header)
    if fields is None or not fields.get('task-id') or 'note' in fields:
        return None
    if set(fields) == {'task-id'}:
        return None, fields['task-id'], None
    if set(fields) == {'task-id', 'tool-use-id', 'output-file', 'status'} and fields['tool-use-id'] and fields['status']:
        return fields['tool-use-id'], fields['task-id'], fields['status']
    return None


def identities(text):
    """The task and call identities a notice's own envelope names, whatever else it holds; never its text."""
    if not isinstance(text, str):
        return ()
    return tuple(found[1] for found in re.finditer(r'<(?:task-id|tool-use-id)>([^<>]+)</(?:task-id|tool-use-id)>', text))


def notification(text):
    """Read only the tag envelope and header, excluding the free-text summary. The note comes back as its text
    when it follows the summary, as measured, as '' when it comes before it, and None when there is none."""
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
    if 'note' in fields:
        fields['note'] = ''  # a note before the summary is present, and its text is never read
    if tail.strip():
        note = re.fullmatch(r'\s*<note>(.*)</note>\s*', tail, re.S)
        if note is None or 'note' in fields:
            return None
        fields['note'] = note[1]
    if not all(isinstance(fields.get(key), str) and fields[key] for key in ('task-id', 'tool-use-id', 'status')):
        return None
    return fields['tool-use-id'], fields['task-id'], fields['status'], fields.get('note')


INTERIM = ('This agent stopped with background work of its own still running. It may resume on its own when that '
           'work completes or reports, and the same task-id notifies again if it does; the result below may be interim.')
FINISHED = ('A task-notification fires each time this agent stops with no live background children of its own. '
            'The user can send it another message and resume it, so the same task-id may notify more than once.')
AGENT_BODY = re.compile(r'\s*<note>([^<>]*)</note>\s*<result>.*?</result>\s*'
                        r'<usage><subagent_tokens>\d+</subagent_tokens><tool_uses>\d+</tool_uses>'
                        r'<duration_ms>\d+</duration_ms></usage>\s*', re.S)
NOTICE_ENVELOPE = re.compile(r'\s*<task-notification>(.*?)<summary>.*?</summary>(.*)</task-notification>\s*', re.S)


@dataclass(frozen=True)
class TaskNotice:
    kind: str
    task: str
    call: str | None
    output: str


def linked_notice(text):
    """Only the measured completion envelopes. Descriptions and result text supply no evidence."""
    envelope = NOTICE_ENVELOPE.fullmatch(text) if isinstance(text, str) else None
    if envelope is None:
        return None
    fields = _header(envelope[1])
    if not fields or fields.get('status') != 'completed' or not fields.get('task-id') or not fields.get('output-file'):
        return None
    base = {'task-id', 'status', 'output-file'}
    call = fields.get('tool-use-id')
    if not envelope[2].strip() and set(fields) == base | {'tool-use-id'} and call:
        return TaskNotice('bash', fields['task-id'], call, fields['output-file'])
    body = AGENT_BODY.fullmatch(envelope[2])
    if body is not None:
        if body[1] == INTERIM and set(fields) == base | {'tool-use-id'} and call:
            return TaskNotice('interim', fields['task-id'], call, fields['output-file'])
        if body[1] == FINISHED and set(fields) == base:
            return TaskNotice('finished', fields['task-id'], None, fields['output-file'])
    return None


def linked_root(entry, session):
    return entry.get('isSidechain') is False and entry.get('sessionId') == session


def native_task_id(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]+', value) is not None


def linked_notice_origin(entry):
    origin = entry.get('origin')
    return (entry.get('version') == LINKED_VERSION and entry.get('type') == 'user'
            and not entry.get('isSidechain') and isinstance(origin, dict) and origin.get('kind') == 'task-notification')


def linked_user_notice(entry, *, own_shell=False):
    if not linked_notice_origin(entry) or entry.get('isSidechain') is not False:
        return None
    origin, message, prompt = entry['origin'], entry.get('message'), entry.get('promptId')
    if (set(origin) != {'kind', 'producer', 'runId'} or origin['producer'] != 'session-task'
            or not isinstance(origin['runId'], str) or not origin['runId']
            or entry.get('promptSource') != 'system' or entry.get('rowSource') != 'recorded'
            or entry.get('turnOrigin') != 'task_notification' or entry.get('queueSkipAttachments') is not True
            or not isinstance(prompt, str) or not prompt or entry.get('turnPromptId') != prompt
            or not isinstance(message, dict) or message.get('role') != 'user'):
        return None
    notice = linked_notice(message.get('content'))
    return notice if notice is not None and (notice.kind != 'bash' or own_shell) else None


@dataclass
class PendingNotice:
    notice: TaskNotice
    text: str
    at: datetime
    dequeued_at: datetime | None = None


class TaskNoticeQueue:
    """Read the two private task-notification routes without guessing a human queue slot."""

    def __init__(self, session):
        self.session = session
        self.version = None
        self.pending = None
        self.unknown = False

    def take(self, entry, at):
        """Return (private carrier, bound notice). A candidate alone cannot settle anything."""
        kind = entry.get('type')
        if entry.get('isSidechain'):
            return False, None
        if 'version' in entry:
            self.version = entry['version']
        pending = self.pending
        if linked_notice_origin(entry):
            notice = linked_user_notice(entry)
            if (pending is not None and pending.dequeued_at is not None and notice == pending.notice
                    and linked_root(entry, self.session) and at is not None and at > pending.dequeued_at
                    and entry['message']['content'] == pending.text):
                self.pending = None
                return True, (notice, pending.at)
            self.pending, self.unknown = None, True
            return True, None
        if kind in ('user', 'assistant') or entry.get('subtype') == 'compact_boundary':
            # A different actual turn or compaction breaks the measured carrier sequence.
            if pending is not None:
                self.pending, self.unknown = None, True
        if kind != 'queue-operation' or self.version != LINKED_VERSION:
            return False, None
        text = entry.get('content')
        keys = {'type', 'operation', 'timestamp', 'sessionId'}
        operation = entry.get('operation')
        own = entry.get('sessionId') == self.session
        if operation == 'enqueue' and (notice := linked_notice(text)) is not None:
            if own and at is not None and set(entry) == keys | {'content'} and pending is None:
                self.pending = PendingNotice(notice, text, at)
            else:
                self.pending, self.unknown = None, True
            return True, None
        if pending is None:
            return False, None
        timed = own and at is not None and at > pending.at
        if (timed and operation == 'dequeue' and set(entry) == keys
                and pending.notice.kind != 'bash' and pending.dequeued_at is None):
            pending.dequeued_at = at
            return True, None
        if (timed and operation == 'remove' and set(entry) == keys | {'content', 'reason'}
                and entry.get('reason') == 'delivered_to_agent' and pending.notice.kind == 'bash'
                and pending.text == text):
            self.pending = None
            return True, (pending.notice, pending.at)
        self.pending, self.unknown = None, True
        # A different queue operation stays with the ordinary queue reader.
        return False, None


def user_notice(entry):
    """A measured native notice is program input, separate from its task's execution evidence.

    Only the known user-row carrier is set aside. Text alone, an unfamiliar origin,
    version, status or payload remains ordinary/unclassified input, never an idle proof.
    """
    if entry.get('version') == LINKED_VERSION:
        return linked_user_notice(entry) is not None
    origin = entry.get('origin')
    known_origin = origin == {'kind': 'task-notification'}
    if entry.get('version') == '2.1.325' and isinstance(origin, dict):
        known_origin = known_origin or (
            set(origin) == {'kind', 'producer', 'runId'}
            and origin['kind'] == 'task-notification' and origin['producer'] == 'session-task'
            and isinstance(origin['runId'], str) and bool(origin['runId']))
    if (entry.get('type') != 'user' or entry.get('isSidechain')
            or not known_origin
            or entry.get('version') not in NOTICE_VERSIONS[('user', 'task-notification')]):
        return False
    message = entry.get('message')
    if not isinstance(message, dict) or message.get('role') != 'user':
        return False
    fields = notification(message.get('content'))
    return fields is not None and fields[2] in {*TERMINAL, 'stopped'}


@dataclass(frozen=True)
class WatchOrigin:
    command: str
    description: str
    output: str
    prompt: str | None


def watch_output(block, task):
    """The Bash acknowledgement names the task and its output file in one line."""
    text = block.get('content')
    if block.get('is_error') is not False or not isinstance(text, str):
        return None
    match = re.match(rf'{re.escape(BACKGROUND_HEADER)}{re.escape(task)}\. Output is being written to: (\S+)\. ', text)
    return match[1] if match is not None else None


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
    resume_stopped_at: datetime | None = None
    other_evidence_at: datetime | None = None
    version: str | None = None
    called_at: datetime | None = None
    acknowledged_at: datetime | None = None
    running_seen: bool = False
    kind: str = 'Bash'
    evidence_errors: tuple[str, ...] = ()
    undated_evidence: bool = False  # a row about it the reader could not date: it may be from after any launch
    watch: WatchOrigin | None = None


def ship_watch_commands(record: Path | None) -> frozenset[str]:
    """The inbox watch as a seat may run it: the bare 'qd inbox --wait', and the brief's own absolute command for
    the ship the record belongs to.

    A Claude record belongs to home/projects/<slug>/<record>.jsonl. Only the framework's fixed seat-home layout,
    runtime/seats/<seat>/home/..., supplies its ship: the record's place, never its text. Neither the caller's
    environment nor text inside a command chooses which qd may be a watch."""
    commands = {'qd inbox --wait'}
    if record is not None:
        record = Path(record)
        home = record.parent.parent.parent
        runtime = home.parent.parent.parent
        if (record.is_absolute() and record.parent.parent.name == 'projects'
                and home.name == 'home' and home.parent.parent.name == 'seats'
                and runtime.name == 'runtime'):
            commands.add(qd_command(runtime.parent, 'inbox', '--wait'))
    return frozenset(commands)


class BackgroundBash:
    def __init__(self, record: Path | None = None):
        self.watch_commands = ship_watch_commands(record)
        self.session = Path(record).stem if record is not None else None
        self.manual_compact_at = None
        self.calls = {}
        self.tasks = {}  # includes terminal identities so later duplicate carriers can be joined
        self.task_calls = {}
        self.unknown_calls = set()
        self.unbound = False
        self.monitor_calls = set()
        self.stopped_monitors = set()
        self.monitor_tasks = {}  # a Monitor's task id, from its own result, to its call
        # Notifications met before their call's own rows, by root call id, in file order. When the inbox watch
        # returns at once, Claude Code writes the notification's enqueue first. Each binds when its call's result
        # creates the task; one still waiting at the record's end leaves the record's tools unknown.
        self.early = {}
        self.version = None  # queue carriers inherit the nearest preceding explicit record version

    def observe_tools(self, row, at):
        """Retain native call/acknowledgement evidence during the input walk."""
        if row.get('isSidechain') or row.get('type') not in ('assistant', 'user'):
            return
        message = row.get('message')
        if not isinstance(message, dict) or not isinstance(message.get('content'), list):
            return
        linked = linked_root(row, self.session)
        for block in message['content']:
            if not isinstance(block, dict):
                continue
            if message.get('role') == 'assistant' and block.get('type') == 'tool_use':
                self.call(block.get('id'), block.get('name'), block.get('input'), at, row.get('version'), linked=linked)
            if message.get('role') == 'user' and block.get('type') == 'tool_result':
                call = block.get('tool_use_id')
                if isinstance(call, str):
                    self.result(call, row, block, at, linked=linked)

    def call(self, call, name, arguments, at, version, *, linked=True):
        if name == 'Monitor' and isinstance(call, str) and call and version in MONITOR_VERSIONS:
            self.monitor_calls.add(call)
            return
        if name not in ('Bash', 'TaskStop'):
            return
        if version == LINKED_VERSION and not linked:
            self.unbound = True
            return
        if not isinstance(call, str) or not call or not isinstance(arguments, dict):
            self.unbound = True
            return
        if call in self.calls or call in self.tasks:
            self.unknown_calls.add(call)
        wanted = arguments.get('run_in_background', False) if name == 'Bash' else arguments.get('task_id')
        command = arguments.get('command')
        watch = name == 'Bash' and isinstance(command, str) and command in self.watch_commands
        self.calls[call] = name, wanted, at, version, watch, linked, command, arguments.get('description')
        if name == 'Bash' and (not isinstance(wanted, bool) or wanted and version not in CALL_VERSIONS):
            self.unknown_calls.add(call)

    def result(self, call, entry, block, at, *, linked=True):
        if call in self.monitor_calls:
            result = entry.get('toolUseResult')
            task = result.get('taskId') if isinstance(result, dict) else None
            if (isinstance(task, str) and task and not block.get('is_error', False)
                    and entry.get('version') in MONITOR_VERSIONS):
                self.monitor_tasks.setdefault(task, call)
            return
        info = self.calls.pop(call, None)
        if info is None:
            return
        name, wanted, called_at, version, watch, own_call, command, description = info
        result = entry.get('toolUseResult')
        allowed = CALL_VERSIONS if name == 'Bash' else STOP_VERSIONS
        supported = version in allowed and entry.get('version') == version
        if version == LINKED_VERSION:
            supported = supported and linked
        if name == 'Bash':
            if (version in ERROR_VERSIONS and supported and wanted is True
                    and block.get('is_error') is True and isinstance(result, str)
                    and isinstance(block.get('content'), str)
                    and at is not None and called_at is not None and at >= called_at):
                return  # the invocation failed; no background handle/task was created
            task = result.get('backgroundTaskId') if isinstance(result, dict) else None
            advertised = isinstance(result, dict) and 'backgroundTaskId' in result
            if not advertised and not wanted:
                return  # ordinary foreground completion remains the ordinary reader's evidence
            if (not supported or not isinstance(task, str) or not task or block.get('is_error', False)
                    or version == LINKED_VERSION and (wanted is not True or not native_task_id(task))
                    or at is None or called_at is None or at < called_at
                    or call in self.tasks or task in self.task_calls):
                self.unknown_calls.add(call)
                return
            origin = None
            if (watch and own_call and linked and wanted is True and native_task_id(call) and native_task_id(task)
                    and isinstance(description, str) and description
                    and (output := watch_output(block, task)) is not None):
                origin = WatchOrigin(command, description, output, entry.get('promptId'))
            self.tasks[call] = Task(call, task, at, at, other_evidence_at=at, version=version, called_at=called_at,
                                    acknowledged_at=at, inbox_watch_started=called_at if watch else None, watch=origin)
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
        if version == LINKED_VERSION and (not isinstance(wanted, str) or not wanted or re.fullmatch(
                rf'Successfully stopped task: {re.escape(wanted)} \(.+\)', result['message'], re.S) is None):
            self.unbound = True
            return
        target = self.task_calls.get(wanted) if isinstance(wanted, str) else None
        if target is None:
            if wanted in self.monitor_tasks:
                self.stopped_monitors.add(wanted)
                return  # the known Monitor owns this local_bash success, never a Bash task
            self.unbound = True
            return
        task = self.tasks[target]
        if LINKED_VERSION in (version, task.version) and (task.version != version or called_at < task.started):
            self.invalidate(task, 'unmeasured-or-conflicting-task-stop')
            return
        self.end(task, 'task-stop', at)

    def delivered_to_child(self, notice, at, compacted_at):
        # Only the child projection can bind a delivered_to_agent carrier to its own Bash start.
        self.early.setdefault(notice.call, []).append((notice.task, 'completed', None, at,
                                                       LINKED_VERSION, CHILD_COMPLETION, compacted_at))

    def invalidate(self, task, reason):
        task.phase, task.invalid = 'unknown', True
        if reason not in task.evidence_errors:
            task.evidence_errors += (reason,)

    @staticmethod
    def seen(task, at):
        """Evidence about a task: a dated row is read at its time; an undated one may be from after any launch."""
        if at is None:
            task.undated_evidence = True
        else:
            task.other_evidence_at = max(at, task.other_evidence_at)

    def end(self, task, status, at):
        self.seen(task, at)
        before_ack = (at is not None and task.phase == 'pending' and status in TERMINAL
                      and not task.running_seen and task.latest == task.acknowledged_at
                      and task.called_at is not None and at >= task.called_at)
        if at is None or at < task.latest and not before_ack or task.invalid:
            self.invalidate(task, 'terminal-time-or-status-conflict')
        elif task.phase == 'ended-by-relaunch' or task.phase == 'ended' and task.terminal != status:
            self.invalidate(task, 'terminal-time-or-status-conflict')
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
        if kind == 'system' and entry.get('subtype') == 'compact_boundary':
            metadata = entry.get('compactMetadata')
            self.manual_compact_at = (at if version == LINKED_VERSION and linked_root(entry, self.session)
                                      and isinstance(metadata, dict) and metadata.get('trigger') == 'manual' else None)
        elif self.manual_compact_at is not None and kind in ('assistant', 'user'):
            message = entry.get('message')
            content = message.get('content') if isinstance(message, dict) else None
            local_compact = (kind == 'user' and version == LINKED_VERSION and linked_root(entry, self.session)
                             and (entry.get('isCompactSummary') is True or isinstance(content, str)
                                  and content.startswith(('<local-command-caveat>', '<command-name>/compact</command-name>',
                                                          '<local-command-stdout>'))))
            if not local_compact:
                self.manual_compact_at = None
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
                self.running(attachment, at, version, root=linked_root(entry, self.session))
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
            self.unparsed(text, version, at)
            return
        call, identity, status, note = fields
        task = self.tasks.get(call)
        if task is None:
            # Held until this root call's own result arrives later in the file. A subagent file's call never
            # arrives here, so a main-file notification still cannot establish a start it owns.
            self.early.setdefault(call, []).append((identity, status, note, at, version, carrier))
            return
        self.notice(task, identity, status, note, at, version, carrier)

    def unparsed(self, text, version, at=None):
        """A notice the Bash reading cannot parse holds only itself, never the record's tool state.

        The Monitor tool's measured event notice, bound to a Monitor call by its tool-use-id or its task id, is that
        tool's own and no Bash evidence. Any other, an Agent's, a SendMessage's, a Workflow's or one not measured,
        ends nothing: a tracked Bash task it names by either identity becomes unknown, and one it does not name is
        unchanged, so an unread notice can never end a Bash task, only leave it pending or unknown. The row is still
        evidence at its time, and a row with none may be from after a launch: either holds the task past a relaunch.
        """
        event = monitor_event(text) if version in MONITOR_VERSIONS else None
        if event is not None:
            call, task, _ = event
            owner = self.monitor_tasks.get(task)
            if call in self.monitor_calls and owner in (None, call) or call is None and owner is not None:
                return  # the Monitor's own notice
        for identity in identities(text):
            target = identity if identity in self.tasks else self.task_calls.get(identity)
            if target is not None:
                self.seen(self.tasks[target], at)
                self.invalidate(self.tasks[target], 'unparsed-task-notification')

    def notice(self, task, identity, status, note, at, version, carrier, compacted_at=None):
        resumed = (status == 'stopped' and note in RESUME_NOTES
                   and version in RESUME_VERSIONS.get(carrier, ()))
        supported = (task.task == identity and version in NOTICE_VERSIONS.get(carrier, ())
                     and at is not None and task.called_at is not None and at >= task.called_at)
        if version == LINKED_VERSION or task.version == LINKED_VERSION:
            supported = (supported and version == task.version == LINKED_VERSION
                         and at >= task.acknowledged_at
                         and (compacted_at is None or task.called_at > compacted_at))
        terminal = (status in TERMINAL and (note is None or (
                    status == 'killed' and note == REAPED and version in REAPED_VERSIONS.get(carrier, ()))))
        if supported and terminal and task.phase == 'ended' and not task.invalid and task.terminal == status:
            # A later native carrier can retain a timestamp earlier than the enqueue's.
            # Once its measured identity, status and start bound agree, it is only a copy.
            return
        if at is None or not resumed:
            self.seen(task, at)
        if not supported:
            self.invalidate(task, 'unmeasured-or-conflicting-task-notification')
        elif terminal:
            self.end(task, status, at)
        else:
            # In particular, resume's stopped+note never says the old command ended: it found no completion record
            # and says the command may have been running when the previous process exited.
            task.phase = 'unknown'
            task.latest = max(at, task.latest)
            if resumed:
                task.resume_stopped_at = max(at, task.resume_stopped_at or at)

    def running(self, attachment, at, version, *, root=False):
        identity = attachment.get('taskId')
        target = self.task_calls.get(identity) if isinstance(identity, str) else None
        if target is None:
            self.unbound = True
            return
        task = self.tasks[target]
        task.running_seen = True
        self.seen(task, at)
        supported = version in STATUS_VERSIONS or (
            version == LINKED_VERSION and root and self.restored_watch(task, attachment))
        if (not supported or attachment.get('status') != 'running' or at is None
                or at < task.latest or task.phase in ('ended', 'ended-by-relaunch') or task.invalid):
            self.invalidate(task, 'unmeasured-or-conflicting-task-status')
        else:
            task.phase, task.latest = 'pending', at

    def restored_watch(self, task, attachment):
        """The manual-compact attachment preserves one already acknowledged root watch."""
        watch = task.watch
        return (task.version == LINKED_VERSION and task.phase == 'pending' and watch is not None
                and self.manual_compact_at is not None and task.acknowledged_at < self.manual_compact_at
                and attachment.keys() == {'type', 'taskId', 'taskType', 'status', 'description', 'deltaSummary',
                                         'outputFilePath', 'shell'}
                and attachment['deltaSummary'] is None and attachment['outputFilePath'] == watch.output
                and attachment['description'] == watch.description
                and attachment['shell'] == {'kind': 'bash', 'command': watch.command, 'toolUseId': task.call})


    def task_states(self, launched_at=None):
        """Launch-scoped death is its own state, without changing native completion evidence."""
        for task in self.tasks.values():
            if (launched_at is not None and task.kind == 'Bash' and task.version in EXIT_VERSIONS
                    and task.invalid and task.called_at is not None and task.called_at < launched_at
                    and not task.undated_evidence
                    and task.other_evidence_at is not None and task.other_evidence_at < launched_at):
                yield replace(task, phase='ended-by-relaunch-after-unreadable-evidence', terminal=None)
                continue
            measured = task.phase == 'pending' or (task.resume_stopped_at is not None
                                                  and task.latest == task.resume_stopped_at)
            if (launched_at is not None and task.kind == 'Bash' and task.version in EXIT_VERSIONS and measured
                    and not task.invalid and task.phase not in ('ended', 'ended-by-relaunch')
                    and task.started < launched_at
                    and (task.latest < launched_at or task.resume_stopped_at is not None
                         and launched_at <= task.resume_stopped_at)
                    and not task.undated_evidence
                    and task.other_evidence_at is not None and task.other_evidence_at < launched_at):
                yield replace(task, phase='ended-by-relaunch', terminal=None)
            else:
                yield task

    def state(self, state, *, launched_at=None):
        # A separate admission view; the cached native tasks and their completion state stay unchanged.
        active = [task for task in self.task_states(launched_at)
                  if task.phase not in ('ended', 'ended-by-relaunch', 'ended-by-relaunch-after-unreadable-evidence')]
        unknown = (self.unbound or self.early or self.unknown_calls
                   or any(task.phase == 'unknown' for task in active))
        return replace(state,
                       pending_tools=None if state.pending_tools is None or unknown else
                       state.pending_tools + tuple('Bash' for task in active),
                       active_tools=state.active_tools + tuple(
                           ActiveTool('Bash', task.started, task.inbox_watch_started) for task in active))


QUEUE_KEYS = frozenset({'type', 'operation', 'timestamp', 'sessionId'})
SAME_VERSION_RESUME_KEYS = frozenset({
    'type', 'isSidechain', 'version', 'sessionId', 'timestamp', 'uuid', 'parentUuid', 'promptId',
    'message', 'origin', 'promptSource', 'queueSkipAttachments', 'queueTranscriptOnly',
    'entrypoint', 'userType', 'permissionMode', 'cwd', 'gitBranch',
})
RESUME_SUMMARY = "Background shell command didn't finish before the previous session ended"
RESUME_WATCH_NOTE = RESUME_STOPPED + ' Check the output file for partial results before assuming it completed.'


def watch_notice_text(task, status):
    header = (f'<task-notification>\n<task-id>{task.task}</task-id>\n'
              f'<tool-use-id>{task.call}</tool-use-id>\n')
    if status == 'stopped':
        return (header + f'<status>stopped</status>\n<summary>{RESUME_SUMMARY}</summary>\n'
                f'<note>{RESUME_WATCH_NOTE}</note>\n</task-notification>')
    disposition = 'was stopped' if status == 'killed' else 'completed (exit code 0)'
    return (header + f'<output-file>{task.watch.output}</output-file>\n<status>{status}</status>\n'
            f'<summary>Background command "{task.watch.description}" {disposition}</summary>\n'
            '</task-notification>')


@dataclass
class WatchExit:
    task: Task
    phase: str
    at: datetime
    parent: str
    saved_tools: bool = False


class WatchRelaunchQueue:
    """The root watch's adjacent exit pair and its transcript-only resume pair.

    The old record version scopes the kill. The transcript-only user row
    supplies the version of the resumed launch and settles the stopped pair.
    """

    def __init__(self, background):
        self.background = background
        self.session = background.session
        self.version = None
        self.turn_end = None
        self.exit = None
        self.unknown = False

    @property
    def pending(self):
        return self.exit is not None and self.exit.phase not in ('exited', 'between-launches')

    def take(self, row, at, *, queue_empty):
        """Consume only a proved native carrier, leaving every other queue operation to its reader."""
        kind, old_version = row.get('type'), self.version
        if not row.get('isSidechain') and 'version' in row:
            self.version = row['version']
        exit = self.exit
        if exit is not None:
            if (private := self.advance(exit, row, at, queue_empty=queue_empty, old_version=old_version)) is not None:
                return private
            if self.pending:
                self.unknown = True
            self.exit = None
        if kind in ('assistant', 'user') or row.get('subtype') == 'compact_boundary':
            self.turn_end = None
        if (kind == 'system' and row.get('subtype') == 'turn_duration' and self.version == VERSION
                and linked_root(row, self.session) and at is not None and native_task_id(row.get('uuid'))):
            self.turn_end = at, row['uuid']
        if (not queue_empty or self.turn_end is None or self.version != VERSION
                or kind != 'queue-operation' or row.get('operation') != 'enqueue'
                or row.keys() != QUEUE_KEYS | {'content'} or row.get('sessionId') != self.session
                or at is None or at <= self.turn_end[0]):
            return False
        text = row['content']
        fields = notification(text)
        if fields is None or fields[2:] != ('killed', None):
            return False
        task = self.background.tasks.get(fields[0])
        if (task is None or task.task != fields[1] or task.watch is None or task.version != VERSION
                or task.invalid or task.call in self.background.unknown_calls or task.phase != 'pending'
                or task.latest > self.turn_end[0] or text != watch_notice_text(task, 'killed')):
            return False
        self.exit = WatchExit(task, 'killed-enqueued', at, self.turn_end[1])
        return True

    def advance(self, exit, row, at, *, queue_empty, old_version):
        # True takes a private carrier, False advances across ordinary metadata,
        # and None breaks the sequence.
        if not queue_empty or row.get('sessionId') != self.session or old_version != VERSION:
            return None
        kind, phase, task = row.get('type'), exit.phase, exit.task
        if phase == 'exited' and kind == 'tools-used' and row.keys() == {'type', 'tools', 'sessionId'}:
            exit.phase = 'between-launches'
            tools = row['tools']
            exit.saved_tools = isinstance(tools, list) and len(tools) == 1 and isinstance(tools[0], str) and bool(tools[0])
            return False  # this is still normal metadata
        if (phase == 'stopped-dequeued' and type(row.get('v')) is int
                and row == {'type': 'derived-from', 'v': 1, 'sessionId': self.session,
                            'sessions': [{'type': 'claude_code_session', 'id': self.session, 'via': 'resume'}]}):
            exit.phase = 'resume-linked'
            return False  # this is still normal metadata
        if at is None or at <= exit.at or task.invalid or task.call in self.background.unknown_calls:
            return None
        if kind == 'queue-operation':
            operation = row.get('operation')
            if (operation == 'dequeue' and row.keys() == QUEUE_KEYS
                    and phase in ('killed-enqueued', 'stopped-enqueued')):
                if phase == 'killed-enqueued':
                    self.background.end(task, 'killed', exit.at)
                    exit.phase = 'exited'
                else:
                    exit.phase = 'stopped-dequeued'
                exit.at = at
                return True
            if (phase in ('exited', 'between-launches') and operation == 'enqueue'
                    and row.keys() == QUEUE_KEYS | {'content'}
                    and row['content'] == watch_notice_text(task, 'stopped')):
                exit.phase, exit.at = 'stopped-enqueued', at
                return True
        upgraded = (phase == 'resume-linked' and linked_notice_origin(row) and row.get('rowSource') == 'harness'
                    and not row.keys() & {'turnOrigin', 'turnPosition', 'turnPromptId', 'isMeta', 'isCompactSummary'})
        same_version = (phase == 'stopped-dequeued' and exit.saved_tools and kind == 'user'
                        and row.get('version') == VERSION and row.keys() == SAME_VERSION_RESUME_KEYS
                        and row['entrypoint'] == 'cli' and row['userType'] == 'external'
                        and row['permissionMode'] == 'bypassPermissions')
        if ((upgraded or same_version) and linked_root(row, self.session)
                and row.get('origin') == {'kind': 'task-notification'} and row.get('promptSource') == 'system'
                and row.get('queueSkipAttachments') is True and row.get('queueTranscriptOnly') is True
                and row.get('parentUuid') == exit.parent
                and native_task_id(row.get('promptId')) and native_task_id(row.get('uuid'))
                and row.get('message') == {'role': 'user', 'content': watch_notice_text(task, 'stopped')}
                and task.phase == 'ended' and task.terminal == 'killed'):
            self.background.seen(task, at)
            task.phase, task.terminal, task.latest = 'ended-by-relaunch', None, at
            self.exit = None
            return True
        return None


@dataclass(frozen=True)
class WatchTurn:
    prompt: str
    started: datetime


@dataclass(frozen=True)
class ForegroundBash:
    call: str
    row: str
    started: datetime


@dataclass
class WatchCompletion:
    task: Task
    text: str
    enqueued_at: datetime
    turn: WatchTurn
    phase: str
    foreground: ForegroundBash | None = None
    disposed_at: datetime | None = None
    command: str | None = None
    delivery: str | None = None
    parent: str | None = None
    parent_at: datetime | None = None


class WatchCompletionQueue:
    """Only the two measured dispositions for a completed root inbox watch."""

    def __init__(self, background):
        self.background = background
        self.session = background.session
        self.version = None
        self.turn = None
        self.foreground = None
        self.pending = None
        self.unknown = False

    def take(self, row, at, *, queue_empty, task_queue_empty):
        """Return ordinary, private, or the start of a native callback turn."""
        if self.pending is not None:
            disposition = self.advance(self.pending, row, at)
            if disposition is not None:
                if disposition == 'turn':
                    self.turn = self.foreground = None
                return disposition
            self.pending, self.unknown = None, True
        self.track_turn(row, at)
        if not row.get('isSidechain') and 'version' in row:
            self.version = row['version']
        if (self.version != LINKED_VERSION or self.turn is None or not queue_empty or not task_queue_empty
                or row.get('type') != 'queue-operation' or row.get('operation') != 'enqueue'
                or row.keys() != QUEUE_KEYS | {'content'} or row['sessionId'] != self.session or at is None):
            return 'ordinary'
        text = row['content']
        notice = linked_notice(text)
        if notice is None or notice.kind != 'bash':
            return 'ordinary'
        task = self.background.tasks.get(notice.call)
        watch = task.watch if task is not None else None
        if (watch is None or task.task != notice.task or task.version != LINKED_VERSION or task.phase != 'pending'
                or task.invalid or task.call in self.background.unknown_calls or at < task.latest
                or not native_task_id(watch.prompt) or at <= self.turn.started or notice.output != watch.output
                or text != watch_notice_text(task, 'completed')):
            return 'ordinary'
        if self.turn.prompt == watch.prompt and not self.background.calls and self.turn.started <= task.called_at:
            self.pending = WatchCompletion(task, text, at, self.turn, 'dequeue')
        elif self.turn.prompt != watch.prompt and task.acknowledged_at < self.turn.started:
            foreground = self.foreground
            if foreground is not None and self.turn.started <= foreground.started < at:
                self.pending = WatchCompletion(task, text, at, self.turn, 'remove', foreground)
            elif not self.background.calls:
                # Native output can persist the enqueue before the already-running Bash call.
                self.pending = WatchCompletion(task, text, at, self.turn, 'foreground')
        if self.pending is None:
            return 'ordinary'
        return 'private'

    def root(self, row):
        return (row.get('version') == LINKED_VERSION and linked_root(row, self.session)
                and row.get('session_id', self.session) == self.session)

    def track_turn(self, row, at):
        """Retain only the typed root prompt and foreground Bash relevant to a watch enqueue."""
        kind = row.get('type')
        if kind == 'system' and row.get('subtype') in ('turn_duration', 'compact_boundary'):
            self.turn = self.foreground = None
        if kind not in ('user', 'assistant'):
            return
        if not self.root(row) or at is None:
            self.turn = self.foreground = None
            return
        message = row.get('message')
        if not isinstance(message, dict):
            self.turn = self.foreground = None
            return
        content = message.get('content')
        if kind == 'user':
            self.foreground = None
            if root_tool_result(row):
                return
            prompt = row.get('promptId')
            self.turn = (WatchTurn(prompt, at) if native_task_id(prompt) and row.get('turnPromptId') == prompt
                         and row.get('origin') == {'kind': 'human'} and row.get('promptSource') == 'typed'
                         and row.get('turnOrigin') == 'human' and message.get('role') == 'user'
                         and isinstance(content, str) and content else None)
        elif self.turn is not None and isinstance(content, list):
            calls = [block for block in content if isinstance(block, dict) and block.get('type') == 'tool_use']
            if calls:
                self.foreground = foreground_bash(row, at) if self.foreground is None else None

    def advance(self, pending, row, at):
        if pending.task.invalid or pending.task.phase != 'pending' or pending.task.call in self.background.unknown_calls:
            return None
        kind, phase = row.get('type'), pending.phase
        if kind == 'queue-operation':
            return self.queue_disposition(pending, row, at)
        if row.get('sessionId') != self.session:
            return None
        metadata = ('api-request', 'api-request-blob', 'api-request-shape', 'data-flow-records', 'last-prompt',
                    'tools-used', 'custom-title', 'agent-name', 'mode', 'permission-mode', 'session-owner', 'atis-latch')
        if (phase == 'dequeue' and kind == 'data-flow-records' and 'version' not in row
                or phase == 'finish' and kind in metadata and row.get('version') in (None, LINKED_VERSION)):
            return 'ordinary'
        if not self.root(row) or at is None:
            return None
        if phase == 'dequeue' and total_tokens_attachment(row) and at <= pending.enqueued_at:
            return 'ordinary'
        if phase == 'foreground' and pending.turn.started <= at < pending.enqueued_at:
            foreground = foreground_bash(row, at)
            if foreground is not None:
                pending.phase, pending.foreground = 'remove', foreground
                return 'ordinary'
        if phase == 'result' and self.first_result(pending, row, at):
            pending.phase, pending.parent = 'attachment', row['uuid']
            return 'ordinary'
        if phase == 'attachment' and self.queued_attachment(pending, row, at):
            pending.phase = 'finish'
            return 'private'
        if phase in ('last-answer', 'finish') and kind == 'assistant':
            if not native_task_id(row.get('uuid')) or at <= pending.enqueued_at:
                return None
            message = row.get('message')
            if not isinstance(message, dict) or message.get('role') != 'assistant':
                return None
            if (message.get('stop_reason') == 'end_turn' and isinstance(message.get('content'), list)
                    and message['content']
                    and all(isinstance(block, dict) and block.get('type') == 'text' for block in message['content'])):
                pending.parent, pending.parent_at = row['uuid'], at
                if phase == 'last-answer':
                    if at > pending.disposed_at:
                        return None
                    pending.phase = 'duration'
            elif phase == 'last-answer':
                return None
            else:
                pending.parent, pending.parent_at = None, None
            return 'ordinary'
        if phase in ('duration', 'finish') and kind == 'system' and row.get('subtype') == 'turn_duration':
            if (pending.parent is None or row.get('parentUuid') != pending.parent or at < pending.parent_at
                    or not native_task_id(row.get('uuid')) or type(row.get('durationMs')) is not int
                    or row['durationMs'] <= 0):
                return None
            if phase == 'duration' and at <= pending.disposed_at:
                pending.phase, pending.parent = 'callback', row['uuid']
                return 'ordinary'
            if phase == 'finish' and at > pending.disposed_at:
                self.end(pending)
                self.turn = self.foreground = None
                return 'ordinary'
            return None
        if (phase == 'callback' and kind == 'user' and at > pending.disposed_at
                and row.get('parentUuid') == pending.parent and row.get('promptId') != pending.turn.prompt
                and native_task_id(row.get('uuid')) and linked_user_notice(row, own_shell=True) is not None
                and row['message']['content'] == pending.text and 'queueTranscriptOnly' not in row):
            self.end(pending)
            return 'turn'
        if phase == 'finish':
            if kind == 'user' and row.get('promptId') == pending.turn.prompt and root_tool_result(row):
                return 'ordinary'
            if total_tokens_attachment(row):
                return 'ordinary'
        return None

    def queue_disposition(self, pending, row, at):
        if row.get('sessionId') != self.session or at is None or at <= pending.enqueued_at:
            return None
        if (pending.phase == 'dequeue' and row.get('operation') == 'dequeue' and row.keys() == QUEUE_KEYS):
            pending.phase, pending.disposed_at = 'last-answer', at
            return 'private'
        if (pending.phase == 'remove' and row.get('operation') == 'remove'
                and row.keys() == QUEUE_KEYS | {'content', 'reason', 'commandUuid', 'deliveryId'}
                and row['reason'] == 'absorbed_mid_turn' and row['content'] == pending.text
                and native_task_id(row['commandUuid']) and native_task_id(row['deliveryId'])
                and row['commandUuid'] != row['deliveryId']):
            pending.phase, pending.disposed_at = 'result', at
            pending.command, pending.delivery = row['commandUuid'], row['deliveryId']
            return 'private'
        return None

    @staticmethod
    def first_result(pending, row, at):
        # The result is filed after removal, with a slightly earlier timestamp.
        result = root_tool_result(row)
        foreground = pending.foreground
        return (result is not None and result.get('tool_use_id') == foreground.call
                and result.get('is_error') is False and row.get('promptId') == pending.turn.prompt
                and row.get('sourceToolAssistantUUID') == row.get('parentUuid') == foreground.row
                and native_task_id(row.get('uuid')) and pending.enqueued_at < at < pending.disposed_at)

    @staticmethod
    def queued_attachment(pending, row, at):
        # The attachment is filed beside that result and retains the enqueue time.
        value = row.get('attachment')
        return (row.get('type') == 'attachment' and isinstance(value, dict) and row.get('parentUuid') == pending.parent
                and native_task_id(row.get('uuid')) and at == pending.enqueued_at
                and row.get('dataFlowOn') is True and 'rowSource' not in row
                and value.keys() == {'type', 'prompt', 'source_uuid', 'delivery_id', 'commandMode', 'origin',
                                     'rowSource', 'runId', 'timestamp'}
                and value['type'] == 'queued_command' and value['commandMode'] == 'task-notification'
                and value['origin'] == {'kind': 'task-notification', 'producer': 'session-task'}
                and value['rowSource'] == 'recorded' and isinstance(value['runId'], str) and bool(value['runId'])
                and value['prompt'] == pending.text and value['source_uuid'] == pending.command
                and value['delivery_id'] == pending.delivery and when(value['timestamp']) == at)

    def end(self, pending):
        self.background.end(pending.task, 'completed', pending.enqueued_at)
        self.pending = None


def foreground_bash(row, at):
    message = row.get('message')
    if (row.get('type') != 'assistant' or not isinstance(message, dict) or message.get('role') != 'assistant'
            or message.get('stop_reason') != 'tool_use' or not native_task_id(row.get('uuid'))):
        return None
    content = message.get('content')
    if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict):
        return None
    block = content[0]
    args = block.get('input')
    if (block.get('type') == 'tool_use' and block.get('name') == 'Bash' and native_task_id(block.get('id'))
            and isinstance(args, dict) and isinstance(args.get('command'), str)
            and args.get('run_in_background', False) is False):
        return ForegroundBash(block['id'], row['uuid'], at)
    return None


def root_tool_result(row):
    message = row.get('message')
    if row.get('type') == 'user' and isinstance(message, dict) and message.get('role') == 'user':
        content = message.get('content')
        if (isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict)
                and content[0].get('type') == 'tool_result'):
            return content[0]
    return None


def total_tokens_attachment(row):
    value = row.get('attachment')
    return row.get('type') == 'attachment' and isinstance(value, dict) and value.get('type') == 'total_tokens_reminder'
