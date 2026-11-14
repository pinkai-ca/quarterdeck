"""Claude Code root Bash task evidence, independent of turn accounting.

Only measured structural fields are joined. Notification summaries and command/output
text supply no execution proof. Unknown evidence is never a record-wide reset.
Each evidence form trusts only the exact versions measured to record it the same way;
an unmeasured version, including any later one, stays unknown.
"""

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
import re

from .types import ActiveTool
from ..seat_command import qd_command


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


# The single-shell resume emitter in 2.1.317 writes these two exact notes. They describe
# the prior process, never prove that a command ended. Only relaunch admission uses them.
RESUME_STOPPED = (
    'No completion record was found for it in the previous session. It may have been stopped'
    ' (via the UI, Monitor timeout, or agent teardown — these leave no transcript marker),'
    ' or it may have been running when the previous Claude Code process exited.'
)
RESUME_NOTES = frozenset({RESUME_STOPPED, RESUME_STOPPED +
    ' Check the output file for partial results before assuming it completed.'})
RESUME_VERSIONS = {
    ('queue-operation', 'enqueue'): frozenset({VERSION}),
    ('user', 'task-notification'): frozenset({VERSION}),
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


def user_notice(entry):
    """A measured native notice is program input, separate from its task's execution evidence.

    Only the known user-row carrier is set aside. Text alone, an unfamiliar origin,
    version, status or payload remains ordinary/unclassified input, never an idle proof.
    """
    if (entry.get('type') != 'user' or entry.get('isSidechain')
            or entry.get('origin') != {'kind': 'task-notification'}
            or entry.get('version') not in NOTICE_VERSIONS[('user', 'task-notification')]):
        return False
    message = entry.get('message')
    if not isinstance(message, dict) or message.get('role') != 'user':
        return False
    fields = notification(message.get('content'))
    return fields is not None and fields[2] in {*TERMINAL, 'stopped'}


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


class BackgroundBash:
    def __init__(self, record: Path | None = None):
        self.watch_commands = {'qd inbox --wait'}
        # A Claude record belongs to home/projects/<slug>/<record>.jsonl. Only the
        # framework's fixed seat-home layout supplies its ship; neither the caller's
        # environment nor text inside the command chooses which qd may be a watch.
        if record is not None:
            record = Path(record)
            home = record.parent.parent.parent
            runtime = home.parent.parent.parent
            if (record.is_absolute() and record.parent.parent.name == 'projects'
                    and home.name == 'home' and home.parent.parent.name == 'seats'
                    and runtime.name == 'runtime'):
                self.watch_commands.add(qd_command(runtime.parent, 'inbox', '--wait'))
        self.calls = {}
        self.tasks = {}  # includes terminal identities so later duplicate carriers can be joined
        self.task_calls = {}
        self.unknown_calls = set()
        self.unbound = False
        self.monitor_calls = set()
        self.monitor_tasks = {}  # a Monitor's task id, from its own result, to its call
        # Notifications met before their call's own rows, by root call id, in file order. When the inbox watch
        # returns at once, Claude Code writes the notification's enqueue first. Each binds when its call's result
        # creates the task; one still waiting at the record's end leaves the record's tools unknown.
        self.early = {}
        self.version = None  # queue carriers inherit the nearest preceding explicit record version

    def call(self, call, name, arguments, at, version):
        if name == 'Monitor' and isinstance(call, str) and call and version in MONITOR_VERSIONS:
            self.monitor_calls.add(call)
            return
        if name not in ('Bash', 'TaskStop'):
            return
        if not isinstance(call, str) or not call or not isinstance(arguments, dict):
            self.unbound = True
            return
        if call in self.calls or call in self.tasks:
            self.unknown_calls.add(call)
        wanted = arguments.get('run_in_background', False) if name == 'Bash' else arguments.get('task_id')
        command = arguments.get('command')
        watch = name == 'Bash' and isinstance(command, str) and command in self.watch_commands
        self.calls[call] = name, wanted, at, version, watch
        if name == 'Bash' and (not isinstance(wanted, bool) or wanted and version not in CALL_VERSIONS):
            self.unknown_calls.add(call)

    def result(self, call, entry, block, at):
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
            self.tasks[call] = Task(call, task, at, at, other_evidence_at=at,
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
        if at is not None:
            task.other_evidence_at = max(at, task.other_evidence_at)
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
            self.unparsed(text, version)
            return
        call, identity, status, note = fields
        task = self.tasks.get(call)
        if task is None:
            # Held until this root call's own result arrives later in the file. A subagent file's call never
            # arrives here, so a main-file notification still cannot establish a start it owns.
            self.early.setdefault(call, []).append((identity, status, note, at, version, carrier))
            return
        self.notice(task, identity, status, note, at, version, carrier)

    def unparsed(self, text, version):
        """A notice the Bash reading cannot parse holds only itself, never the record's tool state.

        The Monitor tool's measured event notice, bound to a Monitor call by its tool-use-id or its task id, is that
        tool's own and no Bash evidence. Any other, an Agent's, a SendMessage's, a Workflow's or one not measured,
        ends nothing: a tracked Bash task it names by either identity becomes unknown, and one it does not name is
        unchanged, so an unread notice can never end a Bash task, only leave it pending or unknown.
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
                self.tasks[target].phase, self.tasks[target].invalid = 'unknown', True

    def notice(self, task, identity, status, note, at, version, carrier):
        resumed = (status == 'stopped' and note in RESUME_NOTES
                   and version in RESUME_VERSIONS.get(carrier, ()))
        if at is not None and not resumed:
            task.other_evidence_at = max(at, task.other_evidence_at)
        if (task.task != identity or version not in NOTICE_VERSIONS.get(carrier, ())
                or at is None or at < task.started):
            task.phase, task.invalid = 'unknown', True
        elif status in TERMINAL and (note is None or (
                status == 'killed' and note == REAPED and version in REAPED_VERSIONS.get(carrier, ()))):
            self.end(task, status, at)
        else:
            # In particular, resume's stopped+note never says the old command ended: it found no completion record
            # and says the command may have been running when the previous process exited.
            task.phase = 'unknown'
            task.latest = max(at, task.latest)
            if resumed:
                task.resume_stopped_at = max(at, task.resume_stopped_at or at)

    def running(self, attachment, at, version):
        identity = attachment.get('taskId')
        target = self.task_calls.get(identity) if isinstance(identity, str) else None
        if target is None:
            self.unbound = True
            return
        task = self.tasks[target]
        if at is not None:
            task.other_evidence_at = max(at, task.other_evidence_at)
        if (version not in STATUS_VERSIONS or attachment.get('status') != 'running' or at is None
                or at < task.latest or task.phase == 'ended' or task.invalid):
            task.phase, task.invalid = 'unknown', True
        else:
            task.phase, task.latest = 'pending', at

    def state(self, state, *, launched_at=None):
        def prior_launch(task):
            return (launched_at is not None and task.phase == 'unknown' and not task.invalid
                    and task.resume_stopped_at is not None and task.other_evidence_at is not None
                    and task.started < launched_at <= task.resume_stopped_at
                    and task.other_evidence_at < launched_at)

        # A separate admission view; task phases and the ordinary record reading stay unchanged.
        active = [task for task in self.tasks.values() if task.phase != 'ended' and not prior_launch(task)]
        unknown = (self.unbound or self.early or self.unknown_calls
                   or any(task.phase == 'unknown' for task in active))
        return replace(state,
                       pending_tools=None if state.pending_tools is None or unknown else
                       state.pending_tools + tuple('Bash' for task in active),
                       active_tools=state.active_tools + tuple(
                           ActiveTool('Bash', task.started, task.inbox_watch_started) for task in active))
