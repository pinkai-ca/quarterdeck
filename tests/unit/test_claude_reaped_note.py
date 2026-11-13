"""A killed task whose note is Claude Code's own memory-pressure note has ended; every other noted notice stays unknown.

Measured from records by structure: Claude Code 2.1.317 kills an idle session's background command under memory
pressure and says so in a fixed note, byte for byte the same on every instance, after the summary, on the queue
enqueue and user task-notification carriers, with nothing about the task after it. That kill ends the task. The
note was not measured on 2.1.322, on the queue remove or queued_command attachment carriers, before the summary, or
with any other status, so each of those stays unknown, as does resume's stopped note and any other text.
Rows are synthetic and recreate only measured structure.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from quarterdeck import idle
from quarterdeck.harness import claude, claude_background

AT = datetime(2032, 11, 12, 13, 14, tzinfo=timezone.utc)
CALL, TASK = 'call-juniper', 'task-juniper'
REAPED = (  # written out here, apart from the source, so that a change to either one is seen
    'This is not a failure of the command. Claude Code stopped it because the system was critically low on memory'
    ' while the session was idle, which says nothing about the command or its own memory use, so there is nothing'
    ' in it to debug. Do not start it again on your own, even if the work seems to need it: memory may still be'
    ' short. Report what was stopped and why, and start it again only when asked. The user can turn this behavior'
    ' off by starting Claude Code with CLAUDE_CODE_DISABLE_BG_SHELL_PRESSURE_REAP=1 in its environment; setting it'
    ' from a shell command has no effect.'
)
RESUMED = 'synthetic resume uncertainty: the command may have been running when the previous process exited'


def row(kind, second, version, **fields):
    entry = dict(type=kind, timestamp=(AT + timedelta(seconds=second)).isoformat(), **fields)
    if kind != 'queue-operation':
        entry.update(version=version, isSidechain=False)
    return entry


def initial(version):
    return [
        row('user', 0, version, message=dict(role='user', content='synthetic order juniper')),
        row('assistant', 1, version, message=dict(role='assistant', content=[dict(
            type='tool_use', id=CALL, name='Bash',
            input=dict(command='synthetic command', description='synthetic', run_in_background=True))])),
        row('user', 2, version, message=dict(role='user', content=[dict(
            type='tool_result', tool_use_id=CALL, content='synthetic handle', is_error=False)]),
            toolUseResult=dict(stdout='', stderr='', interrupted=False, isImage=False, noOutputExpected=False,
                               backgroundTaskId=TASK)),
        row('system', 3, version, subtype='turn_duration'),
    ]


def text(status='killed', note=REAPED, before=False):
    noted = '' if note is None else f'<note>{note}</note>\n'
    return ('<task-notification>\n'
            f'<task-id>{TASK}</task-id>\n<tool-use-id>{CALL}</tool-use-id>\n'
            '<output-file>/synthetic/output</output-file>\n'
            f'<status>{status}</status>\n' + (noted if before else '')
            + '<summary>synthetic summary</summary>\n' + ('' if before else noted) + '</task-notification>')


def carrier(kind, version, second=4, **fields):
    payload = text(**fields)
    if kind in ('enqueue', 'remove'):
        return row('queue-operation', second, version, operation=kind, content=payload)
    if kind == 'user':
        return row('user', second, version, origin=dict(kind='task-notification'),
                   message=dict(role='user', content=payload))
    return row('attachment', second, version, attachment=dict(
        type='queued_command', commandMode='task-notification', origin=dict(kind='task-notification'), prompt=payload))


def read(tmp_path, rows):
    record = tmp_path / 'record-juniper.jsonl'
    record.write_text(''.join(json.dumps(entry) + '\n' for entry in rows))
    return claude.state(record)


def test_the_source_holds_the_measured_note_and_its_exact_carriers():
    assert claude_background.REAPED == REAPED
    assert claude_background.REAPED_VERSIONS == {
        ('queue-operation', 'enqueue'): {'2.1.317'},
        ('user', 'task-notification'): {'2.1.317'},
    }


def test_the_parser_returns_the_note_after_the_summary_and_never_reads_one_before_it():
    assert claude_background.notification(text()) == (CALL, TASK, 'killed', REAPED)
    assert claude_background.notification(text(before=True)) == (CALL, TASK, 'killed', '')
    assert claude_background.notification(text(note=None)) == (CALL, TASK, 'killed', None)
    assert claude_background.notification(text(note='')) == (CALL, TASK, 'killed', '')


@pytest.mark.parametrize('kind', ['enqueue', 'user'])
def test_the_measured_kill_with_the_memory_note_ends_the_task(tmp_path, kind):
    state = read(tmp_path, initial('2.1.317') + [carrier(kind, '2.1.317')])
    assert state.pending_tools == ()
    assert state.active_tools == ()


def test_the_measured_sequence_of_both_carriers_ends_the_task_once(tmp_path):
    rows = initial('2.1.317') + [carrier('enqueue', '2.1.317', 4), carrier('user', '2.1.317', 5),
                                 row('system', 6, '2.1.317', subtype='turn_duration')]
    state = read(tmp_path, rows)
    assert state.pending_tools == ()
    assert idle.check(state) is None


def test_a_running_status_after_the_memory_kill_leaves_the_task_unknown(tmp_path):
    rows = initial('2.1.317') + [carrier('enqueue', '2.1.317', 4), row(
        'attachment', 5, '2.1.317', attachment=dict(type='task_status', taskType='local_bash', taskId=TASK,
                                                    status='running'))]
    assert read(tmp_path, rows).pending_tools is None


@pytest.mark.parametrize('kind', ['remove', 'attachment'])
def test_the_memory_note_on_an_unmeasured_carrier_stays_unknown(tmp_path, kind):
    assert read(tmp_path, initial('2.1.317') + [carrier(kind, '2.1.317')]).pending_tools is None


@pytest.mark.parametrize('kind', ['enqueue', 'user', 'remove', 'attachment'])
@pytest.mark.parametrize('version', ['2.1.322', '2.1.318', '2.1.316'])
def test_the_memory_note_on_an_unmeasured_version_stays_unknown(tmp_path, kind, version):
    assert read(tmp_path, initial(version) + [carrier(kind, version)]).pending_tools is None


@pytest.mark.parametrize('kind', ['enqueue', 'user'])
@pytest.mark.parametrize('status', ['stopped', 'completed', 'failed', 'running'])
def test_the_memory_note_with_any_status_but_killed_stays_unknown(tmp_path, kind, status):
    assert read(tmp_path, initial('2.1.317') + [carrier(kind, '2.1.317', status=status)]).pending_tools is None


@pytest.mark.parametrize('note', [
    RESUMED,
    '',
    ' ' + REAPED,
    REAPED + ' ',
    REAPED + '\n',
    REAPED[:-1],
    REAPED.replace('critically low', 'critically  low'),
    REAPED.replace('Claude Code', 'claude code'),
    REAPED.replace('=1', '=0'),
    REAPED + ' Synthetic addition.',
], ids=['resumed', 'empty', 'leading-space', 'trailing-space', 'trailing-newline', 'truncated', 'double-space',
        'case', 'setting', 'extended'])
@pytest.mark.parametrize('kind', ['enqueue', 'user'])
def test_any_other_note_on_a_kill_stays_unknown(tmp_path, kind, note):
    assert read(tmp_path, initial('2.1.317') + [carrier(kind, '2.1.317', note=note)]).pending_tools is None


@pytest.mark.parametrize('kind', ['enqueue', 'user'])
def test_the_memory_note_before_the_summary_stays_unknown(tmp_path, kind):
    assert read(tmp_path, initial('2.1.317') + [carrier(kind, '2.1.317', before=True)]).pending_tools is None


@pytest.mark.parametrize('kind', ['enqueue', 'user'])
def test_a_resume_stopped_note_after_the_memory_kill_still_leaves_the_task_unknown(tmp_path, kind):
    rows = initial('2.1.317') + [carrier('enqueue', '2.1.317', 4),
                                 carrier(kind, '2.1.317', 5, status='stopped', note=RESUMED)]
    assert read(tmp_path, rows).pending_tools is None


@pytest.mark.parametrize('kind', ['enqueue', 'user', 'remove', 'attachment'])
@pytest.mark.parametrize('status', ['completed', 'failed', 'killed'])
def test_a_terminal_notice_without_a_note_still_ends_the_task(tmp_path, kind, status):
    assert read(tmp_path, initial('2.1.317') + [carrier(kind, '2.1.317', status=status, note=None)]).pending_tools == ()
