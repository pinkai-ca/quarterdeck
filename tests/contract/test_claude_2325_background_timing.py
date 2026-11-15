"""Synthetic timestamp/order variations of the measured 2.1.325 task carriers."""
from datetime import datetime, timedelta, timezone
import json

import pytest

from quarterdeck import idle
from quarterdeck.harness import claude
from quarterdeck.harness.json_record import release

AT = datetime(2032, 4, 5, 6, 7, tzinfo=timezone.utc)


def row(kind, second, **fields):
    value = dict(type=kind, timestamp=(AT + timedelta(seconds=second)).isoformat(), **fields)
    if kind != 'queue-operation':
        value['version'] = '2.1.325'
    return value


def notice(second, carrier='enqueue', status='completed', task='synthetic-task'):
    text = ('<task-notification>\n'
            f'<task-id>{task}</task-id>\n<tool-use-id>synthetic-call</tool-use-id>\n'
            f'<output-file>/synthetic/output</output-file>\n<status>{status}</status>\n'
            '<summary>synthetic summary</summary>\n</task-notification>')
    if carrier == 'attachment':
        return row('attachment', second, attachment=dict(type='queued_command', prompt=text,
                   commandMode='task-notification', origin=dict(kind='task-notification', producer='session-task')))
    return row('queue-operation', second, operation=carrier, content=text,
               **(dict(reason='absorbed_mid_turn') if carrier == 'remove' else {}))


def initial():
    return [row('user', 0, message=dict(role='user', content='synthetic prompt')),
            row('assistant', 1, message=dict(role='assistant', content=[dict(
                type='tool_use', id='synthetic-call', name='Bash',
                input=dict(command='synthetic command', run_in_background=True))])),
            row('user', 3, message=dict(role='user', content=[dict(type='tool_result',
                tool_use_id='synthetic-call', content='synthetic result', is_error=False)]),
                toolUseResult=dict(backgroundTaskId='synthetic-task'))]


def write(record, rows, mode='w'):
    with record.open(mode) as stream:
        stream.writelines(json.dumps(entry)+'\n' for entry in rows)


@pytest.mark.parametrize('difference', ['before-call-time', 'status', 'task', 'older-copy-time'])
def test_a_nonmatching_early_notice_stays_unknown(tmp_path, difference):
    record = tmp_path/'record.jsonl'; rows = initial()
    rows.insert(1, notice(0.5 if difference == 'before-call-time' else 2))
    copy = notice(1.5 if difference == 'older-copy-time' else 2, 'attachment',
                  'failed' if difference == 'status' else 'completed',
                  'synthetic-other' if difference == 'task' else 'synthetic-task')
    write(record, [*rows, notice(4, 'remove'), copy, row('system', 5, subtype='turn_duration')])
    state = claude.state(record)
    assert state.pending_tools is None and len(state.active_tools) == 1
    assert idle.check(state).dimension == 'tools-unknown'
    task = claude._Reader(record).background.tasks['synthetic-call']
    assert task.invalid


@pytest.mark.parametrize('carrier', ['remove', 'attachment'])
def test_a_buffered_copy_cannot_supply_the_early_enqueue_exception(tmp_path, carrier):
    record = tmp_path/'record.jsonl'; rows = initial()
    rows.insert(1, notice(2, carrier))
    write(record, [*rows, row('system', 5, subtype='turn_duration')])
    state = claude.state(record)
    assert state.pending_tools is None
    assert claude._Reader(record).background.tasks['synthetic-call'].invalid


def test_an_unbuffered_terminal_still_requires_the_result_time(tmp_path):
    record = tmp_path/'record.jsonl'
    write(record, [*initial(), notice(2), row('system', 5, subtype='turn_duration')])
    assert claude.state(record).pending_tools is None


def test_the_normal_mid_turn_chain_is_unchanged(tmp_path):
    record = tmp_path/'record.jsonl'
    write(record, [*initial(), notice(4), notice(5, 'remove'), notice(4, 'attachment'),
                   row('system', 6, subtype='turn_duration')])
    state = claude.state(record)
    assert idle.check(state) is None
    assert state.pending_tools == () and state.active_tools == ()
    task = claude._Reader(record).background.tasks['synthetic-call']
    assert task.latest == AT+timedelta(seconds=4) and not task.invalid


def test_a_different_older_timestamp_cannot_undo_a_normal_end(tmp_path):
    record = tmp_path/'record.jsonl'
    write(record, [*initial(), notice(4.5), notice(5, 'remove'), notice(4, 'attachment'),
                   row('system', 6, subtype='turn_duration')])
    assert claude.state(record).pending_tools is None
    assert claude._Reader(record).background.tasks['synthetic-call'].invalid


def test_measured_running_attachment_does_not_poison_a_later_end(tmp_path):
    record = tmp_path/'record.jsonl'
    running = row('attachment', 4, attachment=dict(type='task_status', taskId='synthetic-task',
                  taskType='local_bash', status='running', description='synthetic description'))
    write(record, [*initial(), running, notice(5), notice(6, 'remove'), notice(5, 'attachment'),
                   row('system', 7, subtype='turn_duration')])
    state = claude.state(record)
    assert state.pending_tools == () and state.active_tools == ()
    assert idle.check(state) is None
    task = claude._Reader(record).background.tasks['synthetic-call']
    assert task.phase == 'ended' and not task.invalid


def test_a_running_attachment_still_keeps_its_command_pending(tmp_path):
    record = tmp_path/'record.jsonl'
    running = row('attachment', 4, attachment=dict(type='task_status', taskId='synthetic-task',
                  taskType='local_bash', status='running', description='synthetic description'))
    write(record, [*initial(), running, row('system', 7, subtype='turn_duration')])
    state = claude.state(record)
    assert state.pending_tools == ('Bash',)
    assert idle.check(state).dimension == 'tools-pending'
