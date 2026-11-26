"""Claude Code root Bash task evidence, independent of turn accounting.

Only measured structural fields are joined. The own-watch carriers also match
their exact native text against the acknowledged watch. Unknown evidence is
never a record-wide reset.
Each evidence form trusts only the exact versions measured to record it the same way;
an unmeasured version, including any later one, stays unknown.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
import re

from .claude_record import BACKGROUND_HEADER, file_history_checkpoint
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


STOPPED_NOTE = ('No completion record was found for it in the previous session. It may have been stopped (via the UI, '
                'Monitor timeout, or agent teardown — these leave no transcript marker), or it may have been running '
                'when the previous Claude Code process exited. Check the output file for partial results before '
                'assuming it completed.')
STOPPED_RESUME = 'the stopped notice of a task the previous process left running'


def stopped_resume_notice(entry, session) -> bool:
    """The measured 2.1.331 resume notice for a task the previous process left running. Whether that task's output
    was delivered is not in the record, so this names the hold; it never settles it."""
    message = entry.get('message')
    if (entry.get('type') != 'user' or entry.get('version') != LINKED_VERSION or not linked_root(entry, session)
            or entry.get('origin') != {'kind': 'task-notification'} or entry.get('rowSource') != 'harness'
            or entry.get('promptSource') != 'system' or entry.get('queueSkipAttachments') is not True
            or entry.get('queueTranscriptOnly') is not True or not native_task_id(entry.get('promptId'))
            or entry.keys() & {'turnPromptId', 'turnOrigin'}
            or not isinstance(message, dict) or message.get('role') != 'user'):
        return False
    envelope = NOTICE_ENVELOPE.fullmatch(message.get('content')) if isinstance(message.get('content'), str) else None
    if envelope is None:
        return False
    fields, rest = _header(envelope[1]), _header(envelope[2])
    return (fields is not None and set(fields) == {'task-id', 'tool-use-id', 'status'} and fields['status'] == 'stopped'
            and native_task_id(fields['task-id']) and bool(fields['tool-use-id'])
            and rest == {'note': STOPPED_NOTE})


@dataclass
class PendingNotice:
    notice: TaskNotice
    text: str
    at: datetime
    dequeued_at: datetime | None = None
    absorption: 'BashAbsorption | None' = None
    watch: 'WatchNoticeAbsorption | None' = None
    early: 'EarlyWatchBinding | None' = None
    read: int = 0  # rows this queue has read since the enqueue
    unmeasured: bool = False  # a row since the enqueue that no measured absorption admits before its opening row


ABSORPTION_CONTEXT = frozenset({
    'environment', 'model', 'deferred_tools_delta', 'agent_listing_delta', 'mcp_instructions_delta', 'skill_listing',
    'total_tokens_reminder', 'session_context', 'date', 'credential_org', 'prompt_snapshot',
})


class TaskNoticeQueue:
    """Read private task notifications without guessing a human queue slot."""

    def __init__(self, background):
        self.background = background
        self.session = background.session
        self.version = None
        self.turn = None
        self.pending = None
        self.unknown = False
        self.causes = set()  # why unknown: 'stopped' names the measured resume notice; anything else is 'other'
        self.last_call = None  # the latest root assistant row that called tools: (its uuid, its calls still open)

    def _unknown(self, cause='other'):
        self.pending, self.unknown = None, True
        self.causes.add(cause)

    def track_turn(self, row, at):
        kind = row.get('type')
        if kind == 'system' and row.get('subtype') in ('turn_duration', 'compact_boundary'):
            self.turn = None
        if self.pending is None and self.turn is not None and self.turn.row is not None:
            if kind == 'attachment':
                value = row.get('attachment')
                if (isinstance(value, dict) and isinstance(value.get('type'), str)
                        and value['type'] in ABSORPTION_CONTEXT
                        and BashAbsorption.root(row, self.session) and at is not None
                        and row.get('parentUuid') == self.turn.row and native_task_id(row.get('uuid'))
                        and row['uuid'] != self.turn.row):
                    self.turn = replace(self.turn, row=row['uuid'], context=value['type'])
                else:
                    self.turn = replace(self.turn, row=None, context=None)
            elif kind == 'assistant' or root_tool_result(row) is not None:
                self.turn = replace(self.turn, row=None, context=None)
        if kind != 'user' or root_tool_result(row) is not None:
            return
        self.turn = None
        prompt, message = row.get('promptId'), row.get('message')
        if (at is None or row.get('version') != LINKED_VERSION or not linked_root(row, self.session)
                or not native_task_id(prompt) or row.get('turnPromptId') != prompt
                or not isinstance(message, dict) or message.get('role') != 'user'
                or not isinstance(message.get('content'), str) or not message['content']):
            return
        typed = (row.get('origin') == {'kind': 'human'} and row.get('promptSource') == 'typed'
                 and row.get('turnOrigin') == 'human')
        if typed:
            uuid = row.get('uuid')
            self.turn = RootTurn(prompt, at, uuid if native_task_id(uuid) else None)

    def take(self, entry, at, *, complete=True):
        """Return (private carrier, bound notice). A candidate alone cannot settle anything."""
        kind = entry.get('type')
        pending = self.pending
        if pending is not None and pending.absorption is not None:
            disposition = pending.absorption.advance(entry, at, pending, self.background, complete=complete)
            if disposition == 'settled':
                self.background.end(pending.absorption.task, 'completed', pending.at)
                self.pending = None
                return True, None
            if disposition is not None:
                return disposition == 'private', None
            self._unknown()
            pending = None
        if pending is not None and pending.watch is not None:
            disposition = pending.watch.advance(entry, at, pending, self.background, complete=complete)
            if disposition == 'settled':
                self.background.end(pending.watch.task, 'completed', pending.at)
                self.pending = None
                return True, None
            if disposition == 'ordinary':
                self._read_through(entry, at, complete)
                return False, None
            if disposition is not None:
                return True, None
            self._unknown()
            pending = None
        if pending is not None:
            pending.read += 1  # the late-bound form may begin only on the first row after the enqueue
            pending.unmeasured = pending.unmeasured or not self._window_row(entry)
        if pending is not None and pending.early is not None:
            disposition = pending.early.advance(entry, at, pending, self.background, complete=complete)
            if disposition == 'bound':
                pending.watch, pending.early = pending.early.watch, None
            if disposition is not None:
                self._read_through(entry, at, complete)
                return False, None
            self._unknown()
            pending = None
        if entry.get('isSidechain'):
            return False, None
        if 'version' in entry:
            self.version = entry['version']
        if complete:
            self.track_turn(entry, at)
            self._note_calls(entry)
        if linked_notice_origin(entry):
            # A Bash notice is the seat's own only where this queue already holds it, dequeued, with the same text.
            notice = linked_user_notice(entry, own_shell=pending is not None and pending.dequeued_at is not None
                                        and pending.notice.kind == 'bash')
            if (pending is not None and pending.dequeued_at is not None and notice == pending.notice
                    and linked_root(entry, self.session) and at is not None and at > pending.dequeued_at
                    and entry['message']['content'] == pending.text):
                self.pending = None
                if complete:
                    self.turn = RootTurn(entry['promptId'], at)
                return True, (notice, pending.at)
            self._unknown('stopped' if pending is None and stopped_resume_notice(entry, self.session) else 'other')
            return True, None
        if kind in ('user', 'assistant') or entry.get('subtype') == 'compact_boundary':
            # Only the measured assistant chain can carry a notice through a turn already in progress.
            if pending is not None:
                if kind == 'assistant' and pending.dequeued_at is None and complete:
                    pending.absorption = BashAbsorption.start(entry, at, pending, self.background, self.turn)
                    if pending.absorption is not None:
                        return False, None
                    if self._absorb_watch(entry, at, pending, 'ordinary'):
                        self._read_through(entry, at, complete)
                        return False, None
                    if pending.read == 1 and (early := EarlyWatchBinding.start(entry, at, pending,
                                                                               self.background)):
                        pending.early = early
                        self._read_through(entry, at, complete)
                        return False, None
                self._unknown()
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
                self._unknown()
            return True, None
        if pending is None:
            return False, None
        timed = own and at is not None and at > pending.at
        if timed and operation == 'dequeue' and set(entry) == keys and pending.dequeued_at is None:
            # 2.1.331 also delivers an own Bash notice this way (measured on person's seats): it settles only on
            # the linked recorded notice row with the same text, after the dequeue, as every dequeued notice does.
            pending.dequeued_at = at
            return True, None
        if (timed and operation == 'remove' and set(entry) == keys | {'content', 'reason'}
                and entry.get('reason') == 'delivered_to_agent' and pending.notice.kind == 'bash'
                and pending.text == text):
            self.pending = None
            return True, (pending.notice, pending.at)
        if operation == 'remove' and complete and self._absorb_watch(entry, at, pending, 'private'):
            return True, None
        self._unknown()
        # A different queue operation stays with the ordinary queue reader.
        return False, None

    def _window_row(self, entry):
        """A row the measured absorptions admit between the enqueue and their opening row: the turn's own rows and
        queue operations, read by take() itself, or the bookkeeping measured there. Any other row is still read as
        before, so a dequeued notice is unchanged, but the seat's own watch absorption no longer opens after it."""
        kind = entry.get('type')
        if kind in ('assistant', 'user'):
            return entry.get('isSidechain') is not True
        return kind == 'queue-operation' or WatchNoticeAbsorption.bookkeeping(entry, self.session)

    def _absorb_watch(self, entry, at, pending, expected):
        """Open the measured absorption of the seat's own watch notice on the row that begins it."""
        if (pending.dequeued_at is not None or pending.absorption is not None or pending.early is not None
                or pending.unmeasured):
            return False
        watch = WatchNoticeAbsorption.start(pending, self.background, self.last_call)
        if watch is None or watch.advance(entry, at, pending, self.background, complete=True) != expected:
            return False
        pending.watch = watch
        return True

    def _read_through(self, entry, at, complete):
        """A turn's own row read while a notice is absorbed keeps the version and turn the queue tracks."""
        if entry.get('isSidechain'):
            return
        if 'version' in entry:
            self.version = entry['version']
        if complete:
            self.track_turn(entry, at)
            self._note_calls(entry)

    def _note_calls(self, entry):
        """Keep the latest tool-calling assistant row while any of its calls is open in its turn."""
        kind = entry.get('type')
        if kind == 'system' and entry.get('subtype') in ('turn_duration', 'compact_boundary'):
            self.last_call = None
        elif kind == 'user':
            result = root_tool_result(entry)
            if result is None:
                self.last_call = None  # a prompt or a notice: whatever comes next is not that call's turn
            elif self.last_call is not None and linked_root(entry, self.session):
                row, calls = self.last_call
                if entry.get('sourceToolAssistantUUID') == row and result.get('tool_use_id') in calls:
                    calls = calls - {result['tool_use_id']}
                    self.last_call = (row, calls) if calls else None
        elif calls := tool_calls(entry, self.session):
            self.last_call = entry['uuid'], calls


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
    exact_acknowledgement: bool = False
    cwd: str | None = None


@dataclass(frozen=True)
class BashAcknowledgement:
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


BASH_ACK_GUIDANCE = (
    'You will be notified when it completes — do not poll that file or sleep while waiting. '
    'Read the file only if the user asks for progress, or if you need a line it prints before you can go on, '
    "such as a server's ready line or port."
)
BASH_CWD_HINT = re.compile(r'Session cwd remains [^;\r\n]*[^/;\r\n]; directory changes made by the '
                          r'backgrounded command do not apply to subsequent commands\.')


def bash_acknowledgement(entry, block, task, call, call_row, command, description, *, with_cwd_hint=False):
    result = entry['toolUseResult']
    if (not native_task_id(call) or not native_task_id(call_row)
            or entry.get('sourceToolAssistantUUID') != call_row or entry.get('parentUuid') != call_row
            or not isinstance(command, str) or not command or not isinstance(description, str) or not description
            or result.get('stdout') != '' or result.get('stderr') != ''
            or any(result.get(key) is not False for key in ('interrupted', 'isImage', 'noOutputExpected'))):
        return None
    output = watch_output(block, task)
    if output is None:
        return None
    expected = f'{BACKGROUND_HEADER}{task}. Output is being written to: {output}. {BASH_ACK_GUIDANCE}'
    if with_cwd_hint:
        hint = result.get('backgroundCwdHint')
        if not isinstance(hint, str) or BASH_CWD_HINT.fullmatch(hint) is None:
            return None
        expected += '\n' + hint
    if block['content'] == expected:
        prompt = entry.get('promptId')
        return BashAcknowledgement(description, output, prompt if native_task_id(prompt) else None)
    return None


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
    bash_ack: BashAcknowledgement | None = None
    absorption_ack: BashAcknowledgement | None = None


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
                self.call(block.get('id'), block.get('name'), block.get('input'), at, row.get('version'),
                          linked=linked, row_uuid=row.get('uuid'))
            if message.get('role') == 'user' and block.get('type') == 'tool_result':
                call = block.get('tool_use_id')
                if isinstance(call, str):
                    self.result(call, row, block, at, linked=linked)

    def call(self, call, name, arguments, at, version, *, linked=True, row_uuid=None):
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
        self.calls[call] = name, wanted, at, version, watch, linked, command, arguments.get('description'), row_uuid
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
        name, wanted, called_at, version, watch, own_call, command, description, call_row = info
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
                exact = (version == LINKED_VERSION and called_at < at
                         and bash_acknowledgement(entry, block, task, call, call_row, command, description) is not None)
                cwd = entry.get('cwd')
                origin = WatchOrigin(command, description, output, entry.get('promptId'), exact,
                                     cwd if isinstance(cwd, str) else None)
            ack = absorption_ack = None
            if version == LINKED_VERSION and not watch and own_call and linked and called_at < at:
                ack = bash_acknowledgement(entry, block, task, call, call_row, command, description)
                if ack is None:
                    absorption_ack = bash_acknowledgement(entry, block, task, call, call_row, command, description,
                                                          with_cwd_hint=True)
            self.tasks[call] = Task(call, task, at, at, other_evidence_at=at, version=version, called_at=called_at,
                                    acknowledged_at=at, inbox_watch_started=called_at if watch else None,
                                    watch=origin, bash_ack=ack, absorption_ack=absorption_ack)
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
REMOTE_RESUME_KEYS = SAME_VERSION_RESUME_KEYS | {'rowSource', 'dataFlowOn'}
BRIDGE_KEYS = frozenset({'type', 'sessionId', 'bridgeSessionId', 'lastSequenceNum',
                         'ownerAccountUuid', 'ownerOrganizationUuid'})
BRIDGE_STATUS_TEXT = '/remote-control is active · Continue here, on your phone, or at '
BRIDGE_EVENT_ID = re.compile(r'event_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}')
COST_STATE_KEYS = frozenset({'type', 'sessionId', 'totalCostUSD', 'totalAPIDuration',
                             'totalAPIDurationWithoutRetries', 'totalToolDuration', 'totalLinesAdded',
                             'totalLinesRemoved', 'totalDuration', 'startTime', 'modelUsage', 'hasUnknownModelCost'})
RESUME_SUMMARY = "Background shell command didn't finish before the previous session ended"
RESUME_WATCH_NOTE = RESUME_STOPPED + ' Check the output file for partial results before assuming it completed.'


def bridge_pointer(row):
    return (row.keys() == BRIDGE_KEYS and row['type'] == 'bridge-session'
            and type(row['lastSequenceNum']) is int and row['lastSequenceNum'] == 0
            and all(isinstance(row[key], str) and row[key]
                    for key in ('bridgeSessionId', 'ownerAccountUuid', 'ownerOrganizationUuid')))


def bridge_status(row, parent):
    return (row.keys() == {'type', 'subtype', 'content', 'url', 'isMeta', 'timestamp', 'uuid', 'userType',
                          'entrypoint', 'cwd', 'sessionId', 'version', 'gitBranch', 'dataFlowOn',
                          'parentUuid', 'isSidechain'}
            and row['type'] == 'system' and row['subtype'] == 'bridge_status' and row['version'] == LINKED_VERSION
            and row['parentUuid'] == parent and row['isSidechain'] is False and row['isMeta'] is False
            and row['dataFlowOn'] is True and row['userType'] == 'external' and row['entrypoint'] == 'cli'
            and all(isinstance(row[key], str) and row[key] for key in ('uuid', 'url', 'cwd', 'gitBranch'))
            and row['content'] == BRIDGE_STATUS_TEXT + row['url'] and when(row['timestamp']) is not None)


def bridge_link_event(row, *, snapshot=False):
    keys = {'type', 'v', 'sessionId', 'records'}
    if (row.keys() != (keys | {'snapshot'} if snapshot else keys) or row['type'] != 'data-flow-records'
            or snapshot and row['snapshot'] is not True or type(row['v']) is not int or row['v'] != 1
            or not isinstance(row['records'], list) or len(row['records']) != 1):
        return None
    event = row['records'][0]
    if (isinstance(event, dict) and event.keys() == {'tool_name', 'event_id'}
            and event['tool_name'] == 'RemoteControlLinked' and isinstance(event['event_id'], str)
            and BRIDGE_EVENT_ID.fullmatch(event['event_id']) is not None):
        return event['event_id']
    return None


def shell_notice_text(task, status, acknowledgement=None):
    header = (f'<task-notification>\n<task-id>{task.task}</task-id>\n'
              f'<tool-use-id>{task.call}</tool-use-id>\n')
    if status == 'stopped':
        return (header + f'<status>stopped</status>\n<summary>{RESUME_SUMMARY}</summary>\n'
                f'<note>{RESUME_WATCH_NOTE}</note>\n</task-notification>')
    disposition = 'was stopped' if status == 'killed' else 'completed (exit code 0)'
    source = task.watch if acknowledgement is None else acknowledgement
    return (header + f'<output-file>{source.output}</output-file>\n<status>{status}</status>\n'
            f'<summary>Background command "{source.description}" {disposition}</summary>\n'
            '</task-notification>')


@dataclass
class WatchExit:
    task: Task
    phase: str
    at: datetime
    parent: str
    saved_tools: bool = False
    upgrade_bridge: str | None = None


@dataclass
class OrdinaryBashExit(WatchExit):
    position: int = 0
    stop_dequeued_at: datetime | None = None
    textless_enqueued_at: datetime | None = None


@dataclass
class RemoteWatchExit(WatchExit):
    bridge: str = ''
    position: int = 0
    notice: str = ''
    status: str = ''

    @property
    def steps(self):
        if self.task.version == VERSION:
            return ('incomplete', 'empty-records', 'bridge-session', 'bridge-status', 'link')
        common = ('kill-dequeue', 'tools-used')
        stopped = ('stop-enqueue', 'stop-dequeue')
        return (*common, 'derived-from', 'incomplete', *stopped, 'notice', 'bridge-session', 'bridge-status')


def cost_checkpoint(row):
    return (row.keys() == COST_STATE_KEYS and row['type'] == 'cost-state'
            and isinstance(row['modelUsage'], dict) and type(row['hasUnknownModelCost']) is bool
            and all(type(row[key]) in (int, float)
                    for key in COST_STATE_KEYS - {'type', 'sessionId', 'modelUsage', 'hasUnknownModelCost'}))


class BashRelaunchQueue:
    """Join an acknowledged root Bash task's exit to its transcript-only resume.

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
        self.bridge_seen = False
        self.bridge = None
        self.bridge_events = set()
        self.checkpoint = None
        self.after_link = None
        self.after_status = None
        self.undated_remote_input = False

    @property
    def pending(self):
        return self.exit is not None and (
            self.exit.phase not in ('exited', 'between-launches') or self.exit.upgrade_bridge is not None)

    def take(self, row, at, *, queue_empty):
        """Consume only a proved native carrier, leaving every other queue operation to its reader."""
        self.undated_remote_input = False
        continued = self.remote_continuation(row)
        checkpoint = self.remote_checkpoint(row)
        private = continued or checkpoint or self._take(row, at, queue_empty=queue_empty)
        self.remember(row)
        return private

    def remote_checkpoint(self, row):
        after, self.after_status = self.after_status, None
        if after is None or row.get('sessionId') != self.session:
            return False
        status, prompt_seen = after
        if prompt_seen:
            return cost_checkpoint(row)
        if self.last_prompt(row, status):
            self.after_status = status, True
            return True
        return False

    def remote_continuation(self, row):
        after, self.after_link = self.after_link, None
        if after is None or row.get('sessionId') != self.session:
            return False
        event, status, checkpoint = after
        if not checkpoint:
            if self.last_prompt(row, status):
                self.after_link = event, status, True
            return False
        if (row.keys() != {'type', 'v', 'sessionId', 'records'} or row['type'] != 'data-flow-records'
                or type(row['v']) is not int or row['v'] != 1
                or not isinstance(row['records'], list) or len(row['records']) != 2):
            return False
        linked, remote = row['records']
        if (linked != {'tool_name': 'RemoteControlLinked', 'event_id': event, 'no_message': True}
                or linked['no_message'] is not True or not isinstance(remote, dict)
                or remote.keys() != {'tool_name', 'event_id', 'no_message'}
                or remote['tool_name'] != 'RemoteControlInput' or remote['no_message'] is not True
                or not isinstance(remote['event_id'], str) or BRIDGE_EVENT_ID.fullmatch(remote['event_id']) is None
                or remote['event_id'] in self.bridge_events):
            return False
        self.undated_remote_input = True
        return True

    @staticmethod
    def last_prompt(row, leaf):
        return (row.keys() == {'type', 'sessionId', 'leafUuid', 'lastPrompt'} and row['type'] == 'last-prompt'
                and row['leafUuid'] == leaf and isinstance(row['lastPrompt'], str) and bool(row['lastPrompt']))

    def remember(self, row):
        previous, self.checkpoint = self.checkpoint, None
        if row.get('sessionId') != self.session:
            return
        kind = row.get('type')
        if kind == 'bridge-session':
            self.bridge_seen = True
            self.bridge = row['bridgeSessionId'] if bridge_pointer(row) else None
        elif kind == 'system' and row.get('subtype') == 'bridge_status':
            self.bridge_seen = True
        elif kind == 'data-flow-records' and (event := bridge_link_event(row, snapshot='snapshot' in row)):
            self.bridge_events.add(event)
        elif self.turn_end is not None and self.last_prompt(row, self.turn_end[1]):
            self.checkpoint = 'last-prompt'
        elif previous == 'last-prompt' and cost_checkpoint(row):
            self.checkpoint = 'cost-state'

    def _take(self, row, at, *, queue_empty):
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
        if (kind == 'system' and row.get('subtype') == 'turn_duration' and self.version in (VERSION, LINKED_VERSION)
                and linked_root(row, self.session) and at is not None and native_task_id(row.get('uuid'))):
            self.turn_end = at, row['uuid']
        if (not queue_empty or self.turn_end is None or self.version not in (VERSION, LINKED_VERSION)
                or kind != 'queue-operation' or row.get('operation') != 'enqueue'
                or row.keys() != QUEUE_KEYS | {'content'} or row.get('sessionId') != self.session
                or at is None or at <= self.turn_end[0]):
            return False
        text = row['content']
        fields = notification(text)
        if fields is None or fields[2:] != ('killed', None):
            return False
        task = self.background.tasks.get(fields[0])
        if (task is None or task.task != fields[1] or task.version != self.version
                or task.invalid or task.call in self.background.unknown_calls or task.phase != 'pending'
                or task.latest > self.turn_end[0]):
            return False
        if (task.bash_ack is not None and self.version == LINKED_VERSION and not self.bridge_seen
                and self.checkpoint == 'cost-state' and task.latest < self.turn_end[0]
                and text == shell_notice_text(task, 'killed', task.bash_ack)):
            self.exit = OrdinaryBashExit(task, 'ordinary-bash', at, self.turn_end[1])
            return True
        if task.watch is None or text != shell_notice_text(task, 'killed'):
            return False
        bridge = self.bridge if self.checkpoint == 'cost-state' else None
        if self.version == VERSION:
            # The older launch's exit is shared with its native same-version resume.
            self.exit = WatchExit(task, 'killed-enqueued', at, self.turn_end[1], upgrade_bridge=bridge)
        elif bridge is not None:
            self.exit = RemoteWatchExit(task, 'remote', at, self.turn_end[1], bridge=bridge)
        else:
            return False
        return True

    def advance(self, exit, row, at, *, queue_empty, old_version):
        # True takes a private carrier, False advances across ordinary metadata,
        # and None breaks the sequence.
        if not queue_empty:
            return None
        if isinstance(exit, OrdinaryBashExit):
            return self.advance_ordinary(exit, row, at) if old_version == LINKED_VERSION else None
        if row.get('sessionId') != self.session:
            return None
        if isinstance(exit, RemoteWatchExit):
            return self.advance_remote(exit, row, at)
        if old_version != VERSION:
            return None
        kind, phase, task = row.get('type'), exit.phase, exit.task
        if phase == 'exited' and kind == 'tools-used' and row.keys() == {'type', 'tools', 'sessionId'}:
            exit.phase = 'between-launches'
            tools = row['tools']
            exit.saved_tools = isinstance(tools, list) and len(tools) == 1 and isinstance(tools[0], str) and bool(tools[0])
            return False  # this is still normal metadata
        if phase == 'stopped-dequeued' and self.resume_lineage(row):
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
                    and row['content'] == shell_notice_text(task, 'stopped')):
                exit.phase, exit.at = 'stopped-enqueued', at
                return True
        upgraded = (phase == 'resume-linked' and linked_notice_origin(row) and row.get('rowSource') == 'harness'
                    and not row.keys() & {'turnOrigin', 'turnPosition', 'turnPromptId', 'isMeta', 'isCompactSummary'})
        same_version = (phase == 'stopped-dequeued' and exit.saved_tools and kind == 'user'
                        and row.get('version') == VERSION and row.keys() == SAME_VERSION_RESUME_KEYS
                        and row['entrypoint'] == 'cli' and row['userType'] == 'external'
                        and row['permissionMode'] == 'bypassPermissions')
        if upgraded and self.bridge_seen:
            if exit.upgrade_bridge is None or not exit.saved_tools or not self.remote_notice(exit, row):
                return None
            self.exit = RemoteWatchExit(task, 'remote', at, exit.parent, bridge=exit.upgrade_bridge, notice=row['uuid'])
            return True
        if (upgraded or same_version) and self.stopped_notice(exit, row):
            self.settle(exit, at)
            return True
        return None

    def resume_lineage(self, row):
        return (type(row.get('v')) is int
                and row == {'type': 'derived-from', 'v': 1, 'sessionId': self.session,
                            'sessions': [{'type': 'claude_code_session', 'id': self.session, 'via': 'resume'}]})

    def stopped_notice(self, exit, row):
        task = exit.task
        return task.phase == 'ended' and task.terminal == 'killed' and self.stopped_carrier(exit, row)

    def stopped_carrier(self, exit, row):
        return (linked_root(row, self.session)
                and row.get('origin') == {'kind': 'task-notification'} and row.get('promptSource') == 'system'
                and row.get('queueSkipAttachments') is True and row.get('queueTranscriptOnly') is True
                and row.get('parentUuid') == exit.parent
                and native_task_id(row.get('promptId')) and native_task_id(row.get('uuid'))
                and row.get('message') == {'role': 'user', 'content': shell_notice_text(exit.task, 'stopped')})

    def settle(self, exit, at):
        task = exit.task
        self.background.seen(task, at)
        task.phase, task.terminal, task.latest = 'ended-by-relaunch', None, at
        self.exit = None

    def harness_notice(self, exit, row):
        return (linked_notice_origin(row) and row.keys() == REMOTE_RESUME_KEYS and row['rowSource'] == 'harness'
                and row['dataFlowOn'] is True and row['entrypoint'] == 'cli' and row['userType'] == 'external'
                and row['permissionMode'] == 'bypassPermissions' and row['uuid'] != exit.parent
                and all(isinstance(row[key], str) and row[key] for key in ('cwd', 'gitBranch')))

    def remote_notice(self, exit, row):
        return self.harness_notice(exit, row) and self.stopped_notice(exit, row)

    def advance_ordinary(self, exit, row, at):
        """A .331 crew resume records a dated stopped carrier after another queue pair."""
        task = exit.task
        if task.phase != 'pending' or task.invalid or task.call in self.background.unknown_calls:
            return None
        steps = ('kill-dequeue', 'tools-used', 'before-stop', 'stop-enqueue', 'stop-dequeue',
                 'textless-enqueue', 'textless-dequeue', 'before-notice', 'notice')
        step = steps[exit.position]
        kind = row.get('type')
        own = row.get('sessionId') == self.session
        if step in ('before-stop', 'before-notice'):
            at = file_history_checkpoint(row)
            valid = native_task_id(row.get('messageId')) and at is not None and at > exit.at
        elif not own:
            return None
        elif step == 'tools-used':
            valid = row == {'type': 'tools-used', 'sessionId': self.session, 'tools': ['Bash']}
            at = None
        elif step == 'notice':
            # The notice is timestamped 1 ms before the textless enqueue, despite its later file position.
            if (at is None or at <= exit.stop_dequeued_at or exit.textless_enqueued_at - at != timedelta(milliseconds=1)
                    or not self.harness_notice(exit, row) or not self.stopped_carrier(exit, row)):
                return None
            self.settle(exit, at)
            return True
        elif at is None or kind != 'queue-operation':
            return None
        elif step == 'stop-enqueue':
            valid = (at == exit.at and row.keys() == QUEUE_KEYS | {'content'} and row['operation'] == 'enqueue'
                     and row['content'] == shell_notice_text(task, 'stopped'))
        else:
            operation = 'enqueue' if step == 'textless-enqueue' else 'dequeue'
            valid = at > exit.at and row.keys() == QUEUE_KEYS and row['operation'] == operation
        if not valid:
            return None
        if step == 'stop-dequeue':
            exit.stop_dequeued_at = at
        elif step == 'textless-enqueue':
            exit.textless_enqueued_at = at
        if at is not None:
            exit.at = at
        exit.position += 1
        return kind == 'queue-operation'

    def advance_remote(self, exit, row, at):
        """Remote resumes settle only after the bridge has joined the old leaf and stopped notice."""
        task, steps = exit.task, exit.steps
        step = steps[exit.position]
        if task.invalid or task.call in self.background.unknown_calls:
            return None
        timed = step in ('kill-dequeue', 'stop-enqueue', 'stop-dequeue', 'notice', 'bridge-status')
        if timed and (at is None or at <= exit.at):
            return None
        kind = row.get('type')
        if step in ('kill-dequeue', 'stop-dequeue'):
            valid = kind == 'queue-operation' and row.get('operation') == 'dequeue' and row.keys() == QUEUE_KEYS
        elif step == 'tools-used':
            tools = row.get('tools')
            valid = (kind == step and row.keys() == {'type', 'tools', 'sessionId'}
                     and isinstance(tools, list) and len(tools) == 1 and isinstance(tools[0], str) and bool(tools[0]))
        elif step == 'stop-enqueue':
            valid = (kind == 'queue-operation' and row.get('operation') == 'enqueue'
                     and row.keys() == QUEUE_KEYS | {'content'} and row['content'] == shell_notice_text(task, 'stopped'))
        elif step == 'derived-from':
            valid = self.resume_lineage(row)
        elif step == 'incomplete':
            codes = row.get('codes')
            valid = (kind == 'data-flow-incomplete' and type(row.get('v')) is int and row['v'] == 1
                     and row.keys() == {'type', 'v', 'sessionId', 'codes'} and isinstance(codes, list)
                     and all(type(code) is int for code in codes)
                     and (codes == [9] or task.version == LINKED_VERSION and codes == [9, 10]))
        elif step == 'notice':
            valid = self.remote_notice(exit, row)
        elif step == 'empty-records':
            valid = (type(row.get('v')) is int
                     and row == {'type': 'data-flow-records', 'v': 1, 'sessionId': self.session, 'records': []})
        elif step == 'bridge-session':
            valid = bridge_pointer(row) and row['bridgeSessionId'] == exit.bridge
        elif step == 'bridge-status':
            valid = (bridge_status(row, exit.notice) and native_task_id(row['uuid'])
                     and row['uuid'] not in (exit.notice, exit.parent))
        else:  # link
            event = bridge_link_event(row)
            valid = event is not None and event not in self.bridge_events
        if not valid:
            return None
        if step == 'kill-dequeue':
            self.background.end(task, 'killed', exit.at)
        elif step == 'notice':
            exit.notice = row['uuid']
        elif step == 'bridge-status':
            exit.status = row['uuid']
        elif step == 'link':
            self.after_link = event, exit.status, False
        if timed:
            exit.at = at
        exit.position += 1
        if exit.position == len(steps):
            self.settle(exit, exit.at)
            if step == 'bridge-status':
                # The undated checkpoint can be written after the seat was eligible to wake.
                self.after_status = exit.status, False
        return kind == 'queue-operation' or step in ('notice', 'link')


@dataclass(frozen=True)
class RootTurn:
    prompt: str
    started: datetime
    row: str | None = None
    context: str | None = None


@dataclass(frozen=True)
class ForegroundBash:
    call: str
    row: str
    started: datetime


@dataclass
class BashAbsorption:
    """A root Bash completion taken into the foreground tool's existing turn."""

    turn: RootTurn
    request_key: str
    request: str
    assistant: str
    task: Task | None = None
    background_call: str | None = None
    background_result: str | None = None
    foreground: ForegroundBash | None = None
    removal: tuple[str, str] | None = None
    result: str | None = None
    snapshot: str | None = None

    @staticmethod
    def root(row, session):
        return (row.get('version') == LINKED_VERSION and linked_root(row, session)
                and row.get('session_id', session) == session)

    @staticmethod
    def assistant_block(row):
        message = row.get('message')
        if (row.get('type') != 'assistant' or not isinstance(message, dict)
                or message.get('role') != 'assistant' or message.get('stop_reason') != 'tool_use'
                or not native_task_id(row.get('uuid'))):
            return None
        content = message.get('content')
        if isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict):
            return content[0]
        return None

    @classmethod
    def start(cls, row, at, pending, background, turn):
        if (pending.notice.kind != 'bash' or turn is None or at is None or at < turn.started
                or not cls.root(row, background.session)):
            return None
        block = cls.assistant_block(row)
        if block is None or block.get('type') != 'thinking' or not isinstance(block.get('thinking'), str):
            return None
        task = background.tasks.get(pending.notice.call)
        if task is None:
            # The enqueue may have been written ahead of the first request's assistant and tool rows.
            if (turn.context != 'prompt_snapshot' or row.get('parentUuid') != turn.row or row['uuid'] == turn.row
                    or at >= pending.at or not native_task_id(row.get('requestId'))
                    or pending.notice.call in background.calls):
                return None
            return cls(turn, 'requestId', row['requestId'], row['uuid'])
        ack = task.bash_ack or task.absorption_ack
        if (not cls.owns(task, pending, background, turn) or ack is None or ack.prompt != turn.prompt
                or ack.output != pending.notice.output or at < task.started or row.get('madeBy') != 'model'
                or not native_task_id(row.get('requestRef'))):
            return None
        return cls(turn, 'requestRef', row['requestRef'], row['uuid'], task=task)

    @staticmethod
    def owns(task, pending, background, turn):
        return (task is not None and task.version == LINKED_VERSION and task.task == pending.notice.task
                and task.inbox_watch_started is None
                and task.phase == 'pending' and not task.invalid and task.call not in background.unknown_calls
                and task.called_at >= turn.started and task.latest <= pending.at)

    def next_assistant(self, row):
        if (row.get(self.request_key) == self.request and row.get('parentUuid') == self.assistant
                and row.get('uuid') != self.assistant
                and (self.request_key == 'requestId' or row.get('madeBy') == 'model')):
            return self.assistant_block(row)
        return None

    def tool_result(self, row, call, parent):
        result = root_tool_result(row)
        if (result is not None and result.get('tool_use_id') == call and result.get('is_error') is False
                and row.get('promptId') == self.turn.prompt and row.get('sourceToolAssistantUUID') == parent
                and row.get('parentUuid') == parent and native_task_id(row.get('uuid')) and row['uuid'] != parent):
            return result
        return None

    def find_background(self, row, at, pending):
        if at >= pending.at:
            return None
        if self.background_call is None:
            block = self.next_assistant(row)
            if block is not None and block.get('type') == 'tool_use' and block.get('name') == 'Bash':
                args = block.get('input')
                if (block.get('id') == pending.notice.call and isinstance(args, dict)
                        and args.get('run_in_background') is True):
                    self.background_call = row['uuid']
                    return 'ordinary'
        else:
            mirror = row.get('toolUseResult')
            result = self.tool_result(row, pending.notice.call, self.background_call)
            if (result is not None and isinstance(mirror, dict)
                    and mirror.get('backgroundTaskId') == pending.notice.task):
                text = result.get('content')
                marker = re.escape(BACKGROUND_HEADER + pending.notice.task)
                if isinstance(text, str) and re.match(rf'{marker}(?![A-Za-z0-9_-])', text) is not None:
                    self.background_result = self.assistant = row['uuid']
                    return 'ordinary'
        return None

    def advance(self, row, at, pending, background, *, complete):
        if not complete or at is None or at < self.turn.started:
            return None
        # The ordinary tool reader sees the acknowledgement after this queue reader sees its row.
        if self.task is None:
            if self.background_result is None:
                if not self.root(row, background.session):
                    return None
                return self.find_background(row, at, pending)
            self.task = background.tasks.get(pending.notice.call)
        if not self.owns(self.task, pending, background, self.turn) or at < self.task.started:
            return None
        if row.get('type') == 'queue-operation':
            removal = absorbed_removal(row, at, pending, background.session)
            if self.foreground is not None and self.removal is None and removal is not None:
                self.removal = removal
                return 'private'
            return None
        if not self.root(row, background.session):
            return None
        if self.foreground is None:
            block = self.next_assistant(row)
            if block is None or self.background_result is not None and at >= pending.at:
                return None
            if self.background_result is None and block.get('type') == 'text' and isinstance(block.get('text'), str):
                self.assistant = row['uuid']
                return 'ordinary'
            if self.background_result is None and block.get('caller') != {'type': 'direct'}:
                return None
            self.foreground = foreground_bash(row, at)
            return 'ordinary' if self.foreground is not None and self.foreground.call != self.task.call else None
        if self.removal is None:
            return None
        if self.result is None:
            mirror = row.get('toolUseResult')
            if (self.tool_result(row, self.foreground.call, self.foreground.row) is not None
                    and isinstance(mirror, dict) and 'backgroundTaskId' not in mirror
                    and at >= self.foreground.started):
                self.result = row['uuid']
                return 'ordinary'
            return None
        if self.background_result is not None and self.snapshot is None:
            if (row.get('type') == 'attachment' and row.get('parentUuid') == self.result
                    and native_task_id(row.get('uuid')) and row['uuid'] != self.result
                    and self.result_snapshot(row.get('attachment'))):
                self.snapshot = row['uuid']
                return 'ordinary'
            return None
        if self.attachment(row, at, pending):
            return 'settled'
        return None

    @staticmethod
    def result_snapshot(value):
        return (isinstance(value, dict) and value.get('type') == 'prompt_snapshot'
                and value.keys() == {'type', 'cliPrefix', 'contextRendering', 'echoWireToolInputs', 'inlineTools',
                                     'keptReminders', 'reminderFold', 'systemPrompt', 'systemTurns',
                                     'toolChangeHeader', 'tools'})

    def attachment(self, row, at, pending):
        if (row.keys() != ABSORBED_ATTACHMENT_KEYS or row['parentUuid'] != (self.snapshot or self.result)
                or row['uuid'] in (self.snapshot, self.result, self.foreground.row)
                or not absorbed_attachment(row, pending.text, self.removal)):
            return False
        return self.snapshot is None or when(row['attachment']['timestamp']) == at == pending.at


ABSORBED_ATTACHMENT_KEYS = frozenset({
    'type', 'attachment', 'parentUuid', 'uuid', 'timestamp', 'isSidechain', 'rendered', 'renderedInHumanTurn',
    'renderedRole', 'session_id', 'sessionId', 'userType', 'entrypoint', 'cwd', 'version', 'gitBranch', 'dataFlowOn',
})


def absorbed_removal(row, at, pending, session):
    """The measured remove of a notice absorbed into a running turn: its (commandUuid, deliveryId), or None."""
    if (row.get('type') == 'queue-operation' and at is not None and at > pending.at
            and row.keys() == QUEUE_KEYS | {'content', 'reason', 'commandUuid', 'deliveryId'}
            and row['sessionId'] == session and row['operation'] == 'remove'
            and row['reason'] == 'absorbed_mid_turn' and row['content'] == pending.text
            and native_task_id(row['commandUuid']) and native_task_id(row['deliveryId'])
            and row['commandUuid'] != row['deliveryId']):
        return row['commandUuid'], row['deliveryId']
    return None


def absorbed_attachment(row, text, removal):
    """The measured queued_command attachment that renders an absorbed notice in its turn, named by its remove.

    The row's own key set and parent are the caller's: they differ by where the attachment was measured."""
    if (row.get('type') != 'attachment' or not native_task_id(row.get('uuid'))
            or row.get('dataFlowOn') is not True or row.get('renderedRole') != 'system'
            or row.get('userType') != 'external' or row.get('entrypoint') != 'cli'
            or not all(isinstance(row.get(key), str) for key in ('cwd', 'gitBranch'))):
        return False
    for rendered in (row.get('rendered'), row.get('renderedInHumanTurn')):
        if (not isinstance(rendered, list) or len(rendered) != 1 or not isinstance(rendered[0], dict)
                or rendered[0].keys() != {'content'} or not isinstance(rendered[0]['content'], str)):
            return False
    value = row.get('attachment')
    return (isinstance(value, dict) and value.keys() == {'type', 'prompt', 'source_uuid', 'delivery_id',
                                                       'commandMode', 'origin', 'rowSource', 'runId', 'timestamp'}
            and value['type'] == 'queued_command' and value['commandMode'] == 'task-notification'
            and value['origin'] == {'kind': 'task-notification', 'producer': 'session-task'}
            and value['rowSource'] == 'recorded' and isinstance(value['runId'], str) and bool(value['runId'])
            and when(value['timestamp']) is not None and value['prompt'] == text
            and removal is not None and (value['source_uuid'], value['delivery_id']) == removal)


# Bookkeeping rows the watch reader already reads as ordinary while an absorbed watch finishes its turn.
WATCH_METADATA = ('api-request', 'api-request-blob', 'api-request-shape', 'data-flow-records', 'last-prompt',
                  'tools-used', 'custom-title', 'agent-name', 'mode', 'permission-mode', 'session-owner', 'atis-latch')


@dataclass
class WatchNoticeAbsorption:
    """The completion of the seat's own registered inbox watch, absorbed into a turn already running.

    Measured on 2.1.331 person seats: the enqueue, the turn's own rows, a remove with reason absorbed_mid_turn and
    the queued_command attachment, with no dequeue. The remove may be written before the turn's assistant rows
    (form R) or after one (form A, whose first block may be thinking or tool_use). Only the remove and the
    attachment settle it, and only on every measured equality: the remove's text is the notice's, the attachment's
    prompt, source and delivery are the enqueue's text and the remove's ids, its own time and its attachment's time
    are one and the enqueue's (in the late-bound form alone, up to 1 ms before it), and its parent is the turn's
    tool result just before it. The notice is the one pending: its text is the exact completion of the watch this
    record registered, by call, task and output file. Anything else is not this reading, and the queue stays
    unknown. So are rows between them of any form not measured there or read as ordinary bookkeeping by the watch
    reader."""

    task: Task
    calls: dict  # assistant row uuid -> its calls still open: the last row before the enqueue, and the turn's since
    removal: tuple[str, str] | None = None
    result: str | None = None  # the turn's latest tool result since the enqueue, the attachment's parent
    late: bool = False  # the late-bound form measured two more bookkeeping rows in its metadata run

    @classmethod
    def start(cls, pending, background, last_call):
        task = background.tasks.get(pending.notice.call) if pending.notice.kind == 'bash' else None
        return cls(task, dict([last_call]) if last_call else {}) if cls.owns(task, pending, background) else None

    @staticmethod
    def owns(task, pending, background):
        return (task is not None and task.watch is not None and task.version == LINKED_VERSION
                and task.task == pending.notice.task and task.watch.output == pending.notice.output
                and task.phase == 'pending' and not task.invalid and not task.undated_evidence
                and task.call not in background.unknown_calls and task.called_at is not None
                and task.latest <= pending.at and pending.text == shell_notice_text(task, 'completed'))

    def advance(self, row, at, pending, background, *, complete):
        """'ordinary' for the turn's own rows, 'private' for the remove, 'settled' for the attachment, else None."""
        if not complete or not self.owns(self.task, pending, background):
            return None
        kind = row.get('type')
        if kind == 'queue-operation':
            removal = absorbed_removal(row, at, pending, background.session)
            if self.removal is None and removal is not None:
                self.removal = removal
                return 'private'
            return None
        if self.bookkeeping(row, background.session):
            return 'ordinary'
        if self.late and self.removal is None and (bookkeeping := late_bookkeeping(row, background.session)):
            # Only between the second call and the remove. Nothing else reads history-suppression: the queue keeps
            # it. The ordinary reader already sets bridge-session aside.
            return 'private' if bookkeeping == 'history-suppression' else 'ordinary'
        if row.get('isSidechain') is not False:
            return None
        if kind == 'assistant':
            # Its time may be earlier than the enqueue's though it is written after: file order is the order.
            if self.turn_assistant(row, at, background.session):
                self.result = None
                if calls := tool_calls(row, background.session):
                    self.calls[row['uuid']] = calls
                return 'ordinary'
            return None
        if kind == 'user':
            # The result closes a call proved by its own row: one read in this turn, or the last before the enqueue.
            if not self.turn_result(row, at, background.session):
                return None
            call, source = root_tool_result(row)['tool_use_id'], row['sourceToolAssistantUUID']
            if call in self.calls.get(source, ()):
                self.calls[source] = self.calls[source] - {call}  # a result closes its call once
                self.result = row['uuid']
                return 'ordinary'
            return None
        value = row.get('attachment')
        if kind == 'attachment' and isinstance(value, dict) and value.get('type') == 'queued_command':
            return 'settled' if self.attachment(row, at, pending, background.session) else None
        return None  # any other row is not a measured form: the turn ended, a prompt came, or it is unread

    @staticmethod
    def bookkeeping(row, session):
        """The bookkeeping measured around an absorbed watch notice, before its opening row as after it: the watch
        reader's own-session metadata, a file-history delta and the deferred_tools_record attachment."""
        kind, value = row.get('type'), row.get('attachment')
        if kind in WATCH_METADATA:
            return (row.get('version') in (None, LINKED_VERSION) and not row.get('isSidechain')
                    and row.get('sessionId') == session)
        if kind == 'file-history-delta':
            return row.get('sessionId', session) == session
        return (kind == 'attachment' and row.get('isSidechain') is False and isinstance(value, dict)
                and value.get('type') == 'deferred_tools_record' and BashAbsorption.root(row, session)
                and native_task_id(row.get('uuid')))

    @staticmethod
    def turn_assistant(row, at, session):
        message = row.get('message')
        content = message.get('content') if isinstance(message, dict) else None
        return (at is not None and BashAbsorption.root(row, session) and native_task_id(row.get('uuid'))
                and row.get('madeBy') == 'model' and isinstance(message, dict) and message.get('role') == 'assistant'
                and message.get('stop_reason') == 'tool_use' and isinstance(content, list) and bool(content)
                and all(isinstance(block, dict) and block.get('type') in ('thinking', 'tool_use') for block in content))

    @staticmethod
    def turn_result(row, at, session):
        result = root_tool_result(row)
        return (result is not None and at is not None and BashAbsorption.root(row, session)
                and native_task_id(row.get('uuid')) and native_task_id(result.get('tool_use_id'))
                and native_task_id(row.get('sourceToolAssistantUUID'))
                and row.get('parentUuid') == row['sourceToolAssistantUUID'] != row['uuid'])

    def attachment(self, row, at, pending, session):
        slug, value = row.get('slug'), row.get('attachment')
        return (self.removal is not None and self.result is not None and at is not None and self.timed(at, pending)
                and isinstance(value, dict) and when(value.get('timestamp')) == at
                and row.keys() in (ABSORBED_ATTACHMENT_KEYS, ABSORBED_ATTACHMENT_KEYS | {'slug'})
                and (slug is None and 'slug' not in row or isinstance(slug, str) and bool(slug))
                and BashAbsorption.root(row, session) and row['parentUuid'] == self.result
                and row['uuid'] != self.result and absorbed_attachment(row, pending.text, self.removal))


    def timed(self, at, pending):
        """The attachment's time, which its own timestamp must repeat: the enqueue's, as measured on every earlier
        form. The late-bound form's one measured attachment was 1 ms before its enqueue, so that form alone may be up
        to 1 ms before it, never after."""
        return pending.at - LATE_ATTACHMENT_LEAD <= at <= pending.at if self.late else at == pending.at


LATE_ATTACHMENT_LEAD = timedelta(milliseconds=1)


def late_bookkeeping(row, session):
    """The kind of one of the two session bookkeeping rows measured only in the late-bound form's metadata run, on
    its exact own-session shape, field types and widths; else None. They carry no conversation, task or launch
    identity, and this form alone reads them."""
    kind = row.get('type')
    if session is None or row.get('sessionId') != session:
        return None
    if kind == 'history-suppression':
        shaped = (row.keys() == {'type', 'sessionId', 'cause', 'ts'} and row['cause'] == 'migration'
                  and when(row['ts']) is not None)
    else:
        shaped = (kind == 'bridge-session' and row.keys() == BRIDGE_KEYS | {'noHistoryBackfill'}
                  and type(row['lastSequenceNum']) is int and row['lastSequenceNum'] == 0
                  and row['noHistoryBackfill'] is True
                  and all(isinstance(row[key], str) and len(row[key]) == width for key, width in (
                      ('bridgeSessionId', 28), ('ownerAccountUuid', 36), ('ownerOrganizationUuid', 36))))
    return kind if shaped else None


@dataclass
class EarlyWatchBinding:
    """The seat's own watch completion enqueued before the rows of the call that started that watch.

    Measured once on 2.1.331 (a person's seat): the watch returned within seconds, so its completion was enqueued
    and then, in file order, the same request's thinking row, a text row, the Bash call that started the watch and
    its result naming the task, each timed before the enqueue. Only that bounded prefix is held, row by row: the
    notice stays pending, so nothing is settled and no idle is proved. The adjacent result must bind the seat's own
    registered watch to exactly this notice; the next row must be the same request's next tool call, and from it the
    measured absorption reads the rest, the remove, the closing result and the attachment, on every equality it
    already proves. Any other row, order, binding or a second notice leaves the queue unknown."""

    thinking: str
    request: tuple[str, str]  # the request's requestId and requestRef, shared by every row of the prefix
    step: str = 'text'  # the row expected next: text, call, result, then bound
    text: str | None = None
    call: str | None = None
    called_at: datetime | None = None
    result: str | None = None
    acknowledged_at: datetime | None = None
    watch: 'WatchNoticeAbsorption | None' = None

    @staticmethod
    def request_of(row):
        request = row.get('requestId'), row.get('requestRef')
        return request if all(native_task_id(part) for part in request) else None

    @staticmethod
    def blocks(row, at, session, kind):
        """The row's one block when it is a root model row of the measured shape with exactly that block."""
        message = row.get('message')
        content = message.get('content') if isinstance(message, dict) else None
        if (row.get('type') != 'assistant' or row.get('isSidechain') is not False or at is None
                or not BashAbsorption.root(row, session) or not native_task_id(row.get('uuid'))
                or row.get('madeBy') != 'model' or not isinstance(message, dict)
                or message.get('role') != 'assistant' or message.get('stop_reason') != 'tool_use'
                or not isinstance(content, list) or len(content) != 1
                or not isinstance(content[0], dict) or content[0].get('type') != kind
                or row.keys() & {'sourceToolAssistantUUID', 'promptId'}):
            return None
        return content[0]

    @classmethod
    def start(cls, row, at, pending, background):
        """Only on the request's thinking row, with nothing yet known of the notice's call."""
        call = pending.notice.call
        if (pending.notice.kind != 'bash' or pending.dequeued_at is not None or pending.absorption is not None
                or pending.watch is not None or not native_task_id(call) or not native_task_id(pending.notice.task)
                or call in background.tasks or call in background.calls or call in background.unknown_calls
                or call in background.early or pending.notice.task in background.task_calls):
            return None
        block = cls.blocks(row, at, background.session, 'thinking')
        request = cls.request_of(row)
        if block is None or not isinstance(block.get('thinking'), str) or request is None or at >= pending.at:
            return None
        return cls(row['uuid'], request)

    def written(self):
        return {self.thinking, self.text, self.call, self.result} - {None}

    def advance(self, row, at, pending, background, *, complete):
        """'ordinary' for each row of the prefix, 'bound' on the row after the binding result, else None.

        Every prefix row is timed before the enqueue though written after it, and names the row before it as its
        parent: each a row of its own."""
        if not complete:
            return None
        session, notice = background.session, pending.notice
        if self.step != 'bound' and (at is None or at >= pending.at or row.get('uuid') in self.written()):
            return None
        if self.step == 'text':
            block = self.blocks(row, at, session, 'text')
            if (block is None or not isinstance(block.get('text'), str) or row.get('parentUuid') != self.thinking
                    or self.request_of(row) != self.request):
                return None
            self.text, self.step = row['uuid'], 'call'
            return 'ordinary'
        if self.step == 'call':
            block = self.blocks(row, at, session, 'tool_use')
            arguments = block.get('input') if block is not None else None
            command = arguments.get('command') if isinstance(arguments, dict) else None
            if (block is None or block.get('name') != 'Bash' or block.get('id') != notice.call
                    or not isinstance(arguments, dict) or arguments.get('run_in_background') is not True
                    or not isinstance(command, str) or command not in background.watch_commands
                    or row.get('parentUuid') != self.text or self.request_of(row) != self.request
                    or notice.call in background.calls or notice.call in background.tasks):
                return None
            self.call, self.called_at, self.step = row['uuid'], at, 'result'
            return 'ordinary'
        if self.step == 'result':
            result = root_tool_result(row)
            outcome = row.get('toolUseResult')
            if (not WatchNoticeAbsorption.turn_result(row, at, session) or row.get('isSidechain') is not False
                    or row['sourceToolAssistantUUID'] != self.call or result.get('tool_use_id') != notice.call
                    or not isinstance(outcome, dict) or outcome.get('backgroundTaskId') != notice.task
                    or not native_task_id(row.get('promptId'))):
                return None
            self.result, self.acknowledged_at, self.step = row['uuid'], at, 'bound'
            return 'ordinary'  # the background reader binds the task from this row, after this queue reads it
        # The binding: exactly this prefix registered the seat's own watch, for exactly the pending notice.
        task = background.tasks.get(notice.call)
        if (row.get('uuid') in self.written() or not WatchNoticeAbsorption.owns(task, pending, background)
                or task.called_at != self.called_at or task.acknowledged_at != self.acknowledged_at or task.started != self.acknowledged_at
                or task.inbox_watch_started != self.called_at or background.task_calls.get(notice.task) != notice.call
                or self.blocks(row, at, session, 'tool_use') is None or row.get('parentUuid') != self.result
                or self.request_of(row) != self.request):
            return None
        watch = WatchNoticeAbsorption(task, {}, late=True)
        if watch.advance(row, at, pending, background, complete=True) != 'ordinary':
            return None
        self.watch = watch
        return 'bound'


@dataclass
class WatchCompletion:
    task: Task
    text: str
    enqueued_at: datetime
    turn: RootTurn
    phase: str
    foreground: ForegroundBash | None = None
    disposed_at: datetime | None = None
    command: str | None = None
    delivery: str | None = None
    parent: str | None = None
    parent_at: datetime | None = None


@dataclass(frozen=True)
class AbsorbedWatch:
    task: str
    output: str
    called_at: datetime
    acknowledged_at: datetime
    enqueued_at: datetime
    cwd: str | None


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
        self.absorbed = None

    def absorbed_watch(self):
        if self.absorbed is None:
            return None
        task, at = self.absorbed
        if (task.invalid or task.undated_evidence or task.call in self.background.unknown_calls
                or not task.watch.exact_acknowledgement):
            return None
        return AbsorbedWatch(task.task, task.watch.output, task.called_at, task.acknowledged_at, at, task.watch.cwd)

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
                or text != shell_notice_text(task, 'completed')):
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
            self.turn = (RootTurn(prompt, at) if native_task_id(prompt) and row.get('turnPromptId') == prompt
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
        if (phase == 'dequeue' and kind == 'data-flow-records' and 'version' not in row
                or phase == 'finish' and kind in WATCH_METADATA and row.get('version') in (None, LINKED_VERSION)):
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
        if pending.phase == 'finish':
            self.absorbed = pending.task, pending.enqueued_at
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


def tool_calls(row, session):
    """The call ids of a root assistant row's tool_use blocks, when it names its own row."""
    message = row.get('message')
    if (row.get('type') != 'assistant' or not linked_root(row, session) or not native_task_id(row.get('uuid'))
            or not isinstance(message, dict) or not isinstance(message.get('content'), list)):
        return frozenset()
    return frozenset(block['id'] for block in message['content'] if isinstance(block, dict)
                     and block.get('type') == 'tool_use' and native_task_id(block.get('id')))


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
