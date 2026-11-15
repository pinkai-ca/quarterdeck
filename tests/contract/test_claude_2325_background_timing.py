"""Synthetic timestamp/order variations of the measured 2.1.325 task carriers."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from quarterdeck import idle
from quarterdeck.harness import claude
from quarterdeck.harness.json_record import release

AT = datetime(2032, 4, 5, 6, 7, tzinfo=timezone.utc)
RUNNING = Path(__file__).parents[1]/'fixtures/claude/2.1.325/background-running.json'


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
    if carrier == 'user':
        return row('user', second, origin=dict(kind='task-notification', producer='session-task', runId='synthetic-run'),
                   message=dict(role='user', content=text))
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
def test_early_notices_are_judged_from_call_time_and_matching_identity(tmp_path, difference):
    record = tmp_path/'record.jsonl'; rows = initial()
    rows.insert(1, notice(0.5 if difference == 'before-call-time' else 2))
    copy = notice(1.5 if difference == 'older-copy-time' else 2, 'attachment',
                  'failed' if difference == 'status' else 'completed',
                  'synthetic-other' if difference == 'task' else 'synthetic-task')
    write(record, [*rows, notice(4, 'remove'), copy, row('system', 5, subtype='turn_duration')])
    state = claude.state(record)
    task = claude._Reader(record).background.tasks['synthetic-call']
    if difference == 'older-copy-time':
        assert state.pending_tools == () and state.active_tools == ()
        assert idle.check(state) is None
        assert not task.invalid and task.latest == AT + timedelta(seconds=2)
    else:
        assert state.pending_tools is None and len(state.active_tools) == 1
        assert idle.check(state).dimension == 'tools-unknown'
        assert task.invalid


@pytest.mark.parametrize('carrier', ['remove', 'attachment'])
def test_a_buffered_terminal_after_the_call_time_is_supported(tmp_path, carrier):
    record = tmp_path/'record.jsonl'; rows = initial()
    rows.insert(1, notice(2, carrier))
    write(record, [*rows, row('system', 5, subtype='turn_duration')])
    state = claude.state(record)
    assert state.pending_tools == ()
    task = claude._Reader(record).background.tasks['synthetic-call']
    assert not task.invalid and task.latest == AT + timedelta(seconds=2)


def test_an_unbuffered_terminal_between_call_and_result_is_supported(tmp_path):
    record = tmp_path/'record.jsonl'
    write(record, [*initial(), notice(2), row('system', 5, subtype='turn_duration')])
    assert claude.state(record).pending_tools == ()
    task = claude._Reader(record).background.tasks['synthetic-call']
    assert task.started == AT + timedelta(seconds=3)
    assert task.called_at == AT + timedelta(seconds=1)
    assert task.latest == AT + timedelta(seconds=2)


def test_the_normal_mid_turn_chain_is_unchanged(tmp_path):
    record = tmp_path/'record.jsonl'
    write(record, [*initial(), notice(4), notice(5, 'remove'), notice(4, 'attachment'),
                   row('system', 6, subtype='turn_duration')])
    state = claude.state(record)
    assert idle.check(state) is None
    assert state.pending_tools == () and state.active_tools == ()
    task = claude._Reader(record).background.tasks['synthetic-call']
    assert task.latest == AT+timedelta(seconds=4) and not task.invalid


def test_an_older_copy_after_the_start_keeps_the_original_end(tmp_path):
    record = tmp_path/'record.jsonl'
    write(record, [*initial(), notice(4.5), notice(5, 'remove'), notice(4, 'attachment'),
                   row('system', 6, subtype='turn_duration')])
    assert claude.state(record).pending_tools == ()
    task = claude._Reader(record).background.tasks['synthetic-call']
    assert not task.invalid and task.phase == 'ended'
    assert task.latest == AT+timedelta(seconds=4.5)


def test_measured_running_attachment_does_not_poison_a_later_end(tmp_path):
    record = tmp_path/'record.jsonl'
    running = json.loads(RUNNING.read_text())
    write(record, [*initial(), running, notice(5), notice(6, 'remove'), notice(5, 'attachment'),
                   row('system', 7, subtype='turn_duration')])
    state = claude.state(record)
    assert state.pending_tools == () and state.active_tools == ()
    assert idle.check(state) is None
    task = claude._Reader(record).background.tasks['synthetic-call']
    assert task.phase == 'ended' and not task.invalid


def test_a_running_attachment_still_keeps_its_command_pending(tmp_path):
    record = tmp_path/'record.jsonl'
    running = json.loads(RUNNING.read_text())
    write(record, [*initial(), running, row('system', 7, subtype='turn_duration')])
    state = claude.state(record)
    assert state.pending_tools == ('Bash',)
    assert idle.check(state).dimension == 'tools-pending'


@pytest.mark.parametrize('status', ['completed', 'failed'])
@pytest.mark.parametrize('carrier', ['enqueue', 'remove', 'attachment', 'user'])
@pytest.mark.parametrize('second', [3, 4, 6])
def test_a_matching_duplicate_changes_nothing_after_its_start(tmp_path, status, carrier, second):
    record = tmp_path/'record.jsonl'
    write(record, [*initial(), notice(4.5, status=status)])
    reader = claude._Reader(record)
    task = reader.background.tasks['synthetic-call']
    before = vars(task).copy()
    reader.take(notice(second, carrier, status))
    assert vars(task) == before
    assert reader.state().pending_tools == ()


@pytest.mark.parametrize('difference', ['status', 'task', 'between-call-and-result', 'carrier-version'])
def test_a_late_copy_obeys_call_time_and_carrier_validation(tmp_path, difference):
    record = tmp_path/'record.jsonl'
    copy = notice(2.5 if difference == 'between-call-and-result' else 4, 'attachment',
                  'failed' if difference == 'status' else 'completed',
                  'synthetic-other' if difference == 'task' else 'synthetic-task')
    if difference == 'carrier-version':
        copy['version'] = '2.1.322'
    write(record, [*initial(), notice(4.5), notice(5, 'remove'), copy,
                   row('system', 6, subtype='turn_duration')])
    task = claude._Reader(record).background.tasks['synthetic-call']
    if difference == 'between-call-and-result':
        assert claude.state(record).pending_tools == ()
        assert not task.invalid and task.latest == AT + timedelta(seconds=4.5)
    else:
        assert claude.state(record).pending_tools is None
        assert task.invalid and task.phase == 'unknown'


def test_incremental_older_copy_and_fresh_parse_agree(tmp_path):
    record = tmp_path/'record.jsonl'
    write(record, [*initial(), notice(4.5)])
    assert claude.state(record).pending_tools == ()
    write(record, [notice(5, 'remove'), notice(4, 'attachment'), row('system', 6, subtype='turn_duration')], 'a')
    state = claude.state(record)
    assert idle.check(state) is None
    fresh = claude._Reader(record)
    assert fresh.state() == state
    assert fresh.background.tasks['synthetic-call'].latest == AT+timedelta(seconds=4.5)
    release(record)
    assert claude.state(record) == state


@pytest.mark.parametrize('difference', ['version', 'before-start', 'running-after-end'])
def test_unmeasured_or_conflicting_running_evidence_stays_unknown(tmp_path, difference):
    record = tmp_path/'record.jsonl'
    running = json.loads(RUNNING.read_text())
    if difference == 'version':
        running['version'] = '2.1.326'
    elif difference == 'before-start':
        running['timestamp'] = (AT+timedelta(seconds=2)).isoformat()
    rows = [*initial()]
    if difference == 'running-after-end':
        rows.append(notice(3.5))
    write(record, [*rows, running, notice(5), notice(6, 'remove'), row('system', 7, subtype='turn_duration')])
    assert claude.state(record).pending_tools is None
    assert claude._Reader(record).background.tasks['synthetic-call'].invalid


@pytest.mark.parametrize('carrier', ['enqueue', 'remove', 'attachment', 'user'])
@pytest.mark.parametrize('order', ['before-call-row', 'after-result', 'after-terminal'])
def test_each_terminal_carrier_before_call_time_stays_invalid_in_each_order(tmp_path, carrier, order):
    record = tmp_path / 'record.jsonl'
    rows = initial()
    early = notice(.5, carrier)
    if order == 'before-call-row': rows.insert(1, early)
    elif order == 'after-result': rows.append(early)
    else: rows.extend([notice(4.5), early])
    write(record, [*rows, row('system', 6, subtype='turn_duration')])
    assert claude.state(record).pending_tools is None
    assert claude._Reader(record).background.tasks['synthetic-call'].invalid


def test_a_pre_result_notice_never_hides_a_later_running_event(tmp_path):
    record = tmp_path / 'record.jsonl'
    running = json.loads(RUNNING.read_text())
    running['timestamp'] = (AT + timedelta(seconds=4)).isoformat()
    write(record, [*initial(), running, notice(2), row('system', 5, subtype='turn_duration')])
    assert claude.state(record).pending_tools is None
    assert claude._Reader(record).background.tasks['synthetic-call'].invalid
