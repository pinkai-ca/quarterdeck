"""Measured 2.1.317 shapes, recreated with synthetic identities and no native text."""

from datetime import datetime, timedelta, timezone
import json

import pytest

from quarterdeck import idle
from quarterdeck.harness import claude


AT = datetime(2032, 4, 5, 6, 7, tzinfo=timezone.utc)
VERSION = '2.1.317'
CALL = 'call-spruce'
TASK = 'task-willow'


def row(kind, second, **fields):
    entry = dict(type=kind, timestamp=(AT + timedelta(seconds=second)).isoformat(), **fields)
    if kind != 'queue-operation':
        entry['version'] = VERSION
    return entry


def prompt(second=0, text='synthetic input'):
    return row('user', second, message=dict(role='user', content=text))


def call(second=1, identity=CALL, name='Bash', arguments=None):
    return row('assistant', second, message=dict(role='assistant', content=[dict(
        type='tool_use', id=identity, name=name,
        input=arguments if arguments is not None else dict(command='synthetic command', run_in_background=True))]))


def result(second=2, identity=CALL, task=TASK, error=False, value=None):
    return row('user', second, message=dict(role='user', content=[dict(
        type='tool_result', tool_use_id=identity, content='synthetic result', is_error=error)]),
        toolUseResult=value if value is not None else dict(
            stdout='', stderr='', interrupted=False, isImage=False,
            noOutputExpected=False, backgroundTaskId=task))


def ended(second=3):
    return row('system', second, subtype='turn_duration')


def text(status='completed', identity=CALL, task=TASK, note=False):
    return ('<task-notification>\n'
            f'<task-id>{task}</task-id>\n<tool-use-id>{identity}</tool-use-id>\n'
            '<output-file>/synthetic/output</output-file>\n'
            f'<status>{status}</status>\n<summary>synthetic summary</summary>\n'
            + ('<note>synthetic resume uncertainty</note>\n' if note else '')
            + '</task-notification>')


def notice(carrier='attachment', second=4, **fields):
    payload = text(**fields)
    if carrier in ('enqueue', 'remove'):
        return row('queue-operation', second, operation=carrier, content=payload)
    if carrier == 'user':
        return row('user', second, origin=dict(kind='task-notification'),
                   message=dict(role='user', content=payload))
    return row('attachment', second, attachment=dict(type='queued_command',
               commandMode='task-notification', origin=dict(kind='task-notification'), prompt=payload))


def initial():
    return [prompt(), call(), result(), ended()]


def read(tmp_path, rows, name='record-spruce.jsonl'):
    record = tmp_path / name
    record.write_text(''.join(json.dumps(entry) + '\n' for entry in rows))
    return record, claude.state(record)


def test_returned_background_handle_survives_closed_and_later_turns(tmp_path):
    rows = initial() + [prompt(5, 'synthetic later input'), ended(6)]
    _, state = read(tmp_path, rows)
    assert state.turn_open is False
    assert state.pending_tools == ('Bash',)
    assert state.active_tools[0].started == AT + timedelta(seconds=2)
    assert idle.check(state).dimension == 'tools-pending'


@pytest.mark.parametrize('carrier', ['enqueue', 'remove', 'user', 'attachment'])
@pytest.mark.parametrize('status', ['completed', 'failed', 'killed'])
def test_measured_terminal_carriers_settle_only_the_matching_task(tmp_path, carrier, status):
    _, state = read(tmp_path, initial() + [notice(carrier, status=status)])
    assert state.pending_tools == ()
    assert state.active_tools == ()


def test_sentry_sequence_holds_until_tools_queue_and_latest_turn_are_all_proved(tmp_path):
    record, before = read(tmp_path, initial())
    assert idle.check(before).dimension == 'tools-pending'
    queue_end = notice('enqueue')
    record.write_text(record.read_text() + json.dumps(queue_end) + '\n')
    after_end = claude.state(record)
    assert after_end.pending_tools == ()
    assert idle.check(after_end).dimension == 'queue-pending'
    rest = [notice('remove', 5), notice('user', 6), ended(7)]
    record.write_text(record.read_text() + ''.join(json.dumps(entry) + '\n' for entry in rest))
    after = claude.state(record)
    assert after.queued == ()
    assert idle.check(after) is None


def test_foreground_timeout_promotion_is_tracked_without_background_input_flag(tmp_path):
    rows = initial()
    rows[1] = call(arguments=dict(command='synthetic command'))
    rows[2]['toolUseResult']['timedOutAfterMs'] = 1000
    _, pending = read(tmp_path, rows)
    assert pending.pending_tools == ('Bash',)
    _, finished = read(tmp_path, rows + [notice()], name='record-oak.jsonl')
    assert finished.pending_tools == ()


@pytest.mark.parametrize('success', [True, False])
def test_task_stop_result_joins_call_input_and_background_task(tmp_path, success):
    value = dict(command='synthetic command', message='Successfully stopped synthetic task',
                 task_id=TASK, task_type='local_bash') if success else 'synthetic stop error'
    rows = initial() + [prompt(4), call(5, 'call-maple', 'TaskStop', dict(task_id=TASK)),
                       result(6, 'call-maple', error=not success, value=value), ended(7)]
    _, state = read(tmp_path, rows)
    assert state.pending_tools == (() if success else ('Bash',))


def test_resume_stopped_notice_is_local_unknown_and_later_turn_cannot_clear_it(tmp_path):
    rows = initial() + [notice(status='stopped', note=True), prompt(5), ended(6)]
    _, state = read(tmp_path, rows)
    assert state.pending_tools is None
    assert idle.check(state).dimension == 'tools-unknown'


def test_positive_terminal_can_resolve_its_own_resume_uncertainty(tmp_path):
    rows = initial() + [notice(status='stopped', note=True), notice(second=5)]
    _, state = read(tmp_path, rows)
    assert state.pending_tools == ()


def test_one_task_end_never_clears_another_tasks_uncertainty(tmp_path):
    rows = [prompt(), call(), call(2, 'call-oak'), result(3),
            result(4, 'call-oak', 'task-oak'), ended(5),
            notice(second=6, status='stopped', note=True),
            notice(second=7, identity='call-oak', task='task-oak')]
    record, state = read(tmp_path, rows)
    assert state.pending_tools is None
    assert len(state.active_tools) == 1
    with record.open('a') as stream:
        stream.write(json.dumps(notice(second=8)) + '\n')
    assert claude.state(record).pending_tools == ()


def test_task_stop_result_for_another_task_cannot_end_the_known_task(tmp_path):
    value = dict(command='synthetic command', message='Successfully stopped synthetic task',
                 task_id='task-other', task_type='local_bash')
    rows = initial() + [prompt(4), call(5, 'call-maple', 'TaskStop', dict(task_id=TASK)),
                       result(6, 'call-maple', value=value), ended(7)]
    _, state = read(tmp_path, rows)
    assert state.pending_tools is None
    assert len(state.active_tools) == 1


@pytest.mark.parametrize('difference', ['call', 'task', 'version', 'missing-version'])
def test_unjoined_or_unsupported_notification_stays_unknown(tmp_path, difference):
    entry = notice(identity='call-other' if difference == 'call' else CALL,
                   task='task-other' if difference == 'task' else TASK)
    if difference == 'version':
        entry['version'] = '2.1.322'
    elif difference == 'missing-version':
        del entry['version']
    _, state = read(tmp_path, initial() + [entry])
    assert state.pending_tools is None


@pytest.mark.parametrize('position', [1, 2])
def test_start_and_result_each_require_measured_version(tmp_path, position):
    rows = initial()
    del rows[position]['version']
    _, state = read(tmp_path, rows + [notice()])
    assert state.pending_tools is None


def test_queue_carrier_uses_nearest_preceding_version_not_start_version(tmp_path):
    rows = initial() + [row('agent-name', 4)]
    rows[-1]['version'] = '2.1.323'  # a version no form has been measured on
    rows.append(notice('enqueue', 5))
    _, state = read(tmp_path, rows)
    assert state.pending_tools is None


def test_unrelated_unknown_is_not_cleared_by_matching_background_end(tmp_path):
    rows = initial() + [row('synthetic-unclassified', 4), notice(second=5)]
    _, state = read(tmp_path, rows)
    assert state.active_tools == ()
    assert state.pending_tools is None


def test_running_attachment_is_pending_and_keeps_original_start(tmp_path):
    entry = row('attachment', 4, attachment=dict(type='task_status', taskId=TASK,
                taskType='local_bash', status='running', description='synthetic description'))
    _, state = read(tmp_path, initial() + [entry])
    assert state.pending_tools == ('Bash',)
    assert state.active_tools[0].started == AT + timedelta(seconds=2)


def test_duplicate_terminal_carriers_do_not_create_unjoined_results(tmp_path):
    rows = initial() + [notice('enqueue'), notice('remove', 5), notice('attachment', 6)]
    _, state = read(tmp_path, rows)
    assert state.pending_tools == ()


def test_incremental_read_equals_fresh_parse_and_truncation_discards_task_cache(tmp_path):
    record, _ = read(tmp_path, initial())
    with record.open('a') as stream:
        stream.write(json.dumps(notice()) + '\n')
    appended = claude.state(record)
    assert appended == claude._Reader(record).state()
    record.write_text(''.join(json.dumps(entry) + '\n' for entry in [prompt(), ended()]))
    reset = claude.state(record)
    assert reset.pending_tools == ()
    assert reset.active_tools == ()
    assert reset == claude._Reader(record).state()
