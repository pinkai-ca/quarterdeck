"""Claude Code 2.1.322's background-task and paste forms, measured against 2.1.317 from records by structure only.

2.1.322 recorded these the same way, form for form: a root background Bash call and its joined result, the queue
enqueue and user-row task-notification carriers, and running local_bash task status. Nothing of 2.1.322 was measured
for TaskStop, the queue remove carrier, the queued_command attachment carrier or the two paste transforms, so those
stay 2.1.317's alone, and any other version stays unknown. Rows are synthetic and recreate only measured structure.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from quarterdeck.harness import claude, claude_background

AT = datetime(2032, 10, 11, 12, 13, tzinfo=timezone.utc)
CALL, TASK = 'call-sorrel', 'task-sorrel'


def row(kind, second, version, **fields):
    entry = dict(type=kind, timestamp=(AT + timedelta(seconds=second)).isoformat(), **fields)
    if kind != 'queue-operation':
        entry.update(version=version, isSidechain=False)
    return entry


def prompt(version, second=0):
    return row('user', second, version, message=dict(role='user', content='synthetic order sorrel'))


def call(version, second=1, name='Bash', arguments=None, identity=CALL):
    return row('assistant', second, version, message=dict(role='assistant', content=[dict(
        type='tool_use', id=identity, name=name,
        input=arguments or dict(command='synthetic command', description='synthetic', run_in_background=True))]))


def result(version, second=2, identity=CALL, value=None):
    return row('user', second, version, message=dict(role='user', content=[dict(
        type='tool_result', tool_use_id=identity, content='synthetic handle', is_error=False)]),
        toolUseResult=value or dict(stdout='', stderr='', interrupted=False, isImage=False, noOutputExpected=False,
                                    backgroundTaskId=TASK))


def text(status='completed'):
    return ('<task-notification>\n'
            f'<task-id>{TASK}</task-id>\n<tool-use-id>{CALL}</tool-use-id>\n'
            '<output-file>/synthetic/output</output-file>\n'
            f'<status>{status}</status>\n<summary>synthetic summary</summary>\n</task-notification>')


def carrier(kind, version, second=3):
    if kind in ('enqueue', 'remove'):
        return row('queue-operation', second, version, operation=kind, content=text())
    if kind == 'user':
        return row('user', second, version, origin=dict(kind='task-notification'),
                   message=dict(role='user', content=text()))
    return row('attachment', second, version, attachment=dict(
        type='queued_command', commandMode='task-notification', origin=dict(kind='task-notification'), prompt=text()))


def ended(version, second=5):
    return row('system', second, version, subtype='turn_duration')


def read(tmp_path, rows):
    record = tmp_path / 'record-sorrel.jsonl'
    record.write_text(''.join(json.dumps(entry) + '\n' for entry in rows))
    return claude.state(record)


def test_the_measured_versions_are_an_exact_set_per_form():
    assert claude_background.CALL_VERSIONS == {'2.1.317', '2.1.322', '2.1.325'}
    assert claude_background.STATUS_VERSIONS == {'2.1.317', '2.1.322'}
    assert claude_background.STOP_VERSIONS == {'2.1.317'}
    assert claude_background.NOTICE_VERSIONS == {
        ('queue-operation', 'enqueue'): {'2.1.317', '2.1.322', '2.1.325'}, ('queue-operation', 'remove'): {'2.1.317'},
        ('user', 'task-notification'): {'2.1.317', '2.1.322', '2.1.325'}, ('attachment', 'queued_command'): {'2.1.317'}}
    assert claude.PASTE_FORM_VERSIONS == {'2.1.317'}, 'no 2.1.322 instance of either paste transform was measured'


@pytest.mark.parametrize('version', ['2.1.317', '2.1.322'])
@pytest.mark.parametrize('kind', ['enqueue', 'user'])
def test_a_measured_call_and_carrier_end_the_task_on_either_measured_version(tmp_path, version, kind):
    state = read(tmp_path, [prompt(version), call(version), result(version), carrier(kind, version), ended(version)])
    assert state.pending_tools == () and state.active_tools == ()


@pytest.mark.parametrize('version', ['2.1.317', '2.1.322'])
def test_a_measured_call_without_its_end_is_pending_and_running_status_keeps_it_known(tmp_path, version):
    status = row('attachment', 3, version, attachment=dict(
        type='task_status', taskType='local_bash', taskId=TASK, status='running', description='synthetic',
        deltaSummary='synthetic', outputFilePath='/synthetic/output', shell='bash'))
    state = read(tmp_path, [prompt(version), call(version), result(version), status, ended(version)])
    assert state.pending_tools == ('Bash',)


@pytest.mark.parametrize('kind', ['remove', 'attachment'])
def test_carriers_never_measured_on_2_1_322_stay_unknown_there(tmp_path, kind):
    rows = [prompt('2.1.322'), call('2.1.322'), result('2.1.322'), carrier(kind, '2.1.322'), ended('2.1.322')]
    assert read(tmp_path, rows).pending_tools is None
    rows = [prompt('2.1.317'), call('2.1.317'), result('2.1.317'), carrier(kind, '2.1.317'), ended('2.1.317')]
    assert read(tmp_path, rows).pending_tools == (), 'the same carrier is measured on 2.1.317'


def test_task_stop_is_measured_on_2_1_317_only(tmp_path):
    stop = dict(task_id=TASK)
    stopped = dict(message=f'Successfully stopped task: {TASK}', task_id=TASK, task_type='local_bash', command='x')
    for version, known in (('2.1.317', True), ('2.1.322', False)):
        rows = [prompt(version), call(version), result(version),
                call(version, 3, 'TaskStop', stop, 'call-stop'), result(version, 4, 'call-stop', stopped), ended(version)]
        state = read(tmp_path, rows)
        assert (state.pending_tools == ()) is known and (state.pending_tools is None) is (not known), version


@pytest.mark.parametrize('version', ['2.1.323', '2.1.316', '2.2.0', None])
def test_an_unmeasured_version_stays_unknown(tmp_path, version):
    rows = [prompt('2.1.322'), call(version or '2.1.322'), result(version or '2.1.322'), carrier('enqueue', version),
            ended('2.1.322')]
    if version is None:  # a call with no version at all
        rows[1].pop('version')
        rows[2].pop('version')
    assert read(tmp_path, rows).pending_tools is None


def test_a_call_and_result_on_different_measured_versions_stay_unknown(tmp_path):
    rows = [prompt('2.1.317'), call('2.1.317'), result('2.1.322'), carrier('enqueue', '2.1.322'), ended('2.1.322')]
    assert read(tmp_path, rows).pending_tools is None


def test_a_2_1_322_paste_is_recorded_as_typed(tmp_path):
    typed = 'col\tumn and <pasted_content inside'
    record = tmp_path / 'record-paste.jsonl'
    record.write_text(json.dumps(prompt('2.1.322')) + '\n')
    since = AT - timedelta(seconds=1)
    assert claude.ClaudeHarness().recorded_paste(typed, record, since) == typed, 'unmeasured: the payload as typed'
    record.write_text(json.dumps(prompt('2.1.317')) + '\n')
    assert claude.ClaudeHarness().recorded_paste(typed, record, since) != typed, 'the measured 2.1.317 form'


@pytest.mark.parametrize('kind,known', [('enqueue', True), ('user', True), ('remove', False), ('attachment', False)])
def test_an_early_2_1_322_notification_is_judged_by_its_carriers_set_when_it_binds(tmp_path, kind, known):
    """Written before its call's own rows, as when the watch returns at once: held, then bound by its carrier's set."""
    rows = [prompt('2.1.322'), carrier(kind, '2.1.322', second=4.5), call('2.1.322', 4.1), result('2.1.322', 4.2),
            ended('2.1.322', 5)]
    state = read(tmp_path, rows)
    assert (state.pending_tools == ()) is known and (state.pending_tools is None) is (not known)
