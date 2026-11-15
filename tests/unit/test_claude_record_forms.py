"""Five crew-measured native forms, recreated without native identities or content."""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from quarterdeck.harness import claude
from quarterdeck.harness.claude_background import RESUME_STOPPED
from tests.unit.test_claude_background_ordinary import AT, CALL, TASK, call, ended, initial, notice, prompt, read, result, row, text
from tests.unit.test_claude_monitor_notice import MONITOR_TASK, monitor_started


def load(name):
    path = Path(__file__).parents[1] / 'fixtures/claude/2.1.317/record-forms.json'
    return json.loads(path.read_text())[name]


def test_textless_dequeue_consumes_the_adjacent_timed_notice_and_preserves_receipt(tmp_path):
    record, state = read(tmp_path, load('textless-dequeue'))
    assert state.queued == ()
    receipts = [item for item in claude.inputs(record) if item.text.startswith('<task-notification>')]
    assert len(receipts) == 1 and receipts[0].kind == 'consumed'
    assert receipts[0].at == AT + timedelta(seconds=4)
    assert receipts[0].consumed_at == AT + timedelta(seconds=4.1)


@pytest.mark.parametrize('fault', ['version', 'same-time', 'older-time', 'late', 'missing-time',
                                  'intervening-enqueue', 'intervening-remove', 'ordinary-input', 'malformed'])
def test_textless_dequeue_requires_the_exact_measured_relation(tmp_path, fault):
    rows = load('textless-dequeue')
    if fault == 'version':
        for entry in rows: entry['version'] = '2.1.399'
    elif fault in ('same-time', 'older-time', 'late'):
        seconds = {'same-time': 4, 'older-time': 3.9, 'late': 5.001}[fault]
        rows[-1]['timestamp'] = (AT + timedelta(seconds=seconds)).isoformat()
    elif fault == 'missing-time':
        del rows[-1]['timestamp']
    elif fault.startswith('intervening-'):
        operation = fault.removeprefix('intervening-')
        rows.insert(-1, row('queue-operation', 4.05, operation=operation, content='synthetic different input'))
    elif fault == 'ordinary-input': rows[-2]['content'] = 'synthetic ordinary input'
    elif fault == 'malformed': rows[-2]['content'] = '<task-notification>synthetic malformed'
    _, state = read(tmp_path, rows)
    assert state.queued


def test_textless_dequeue_does_not_reconsume_a_notice_already_taken_by_a_user(tmp_path):
    rows = load('textless-dequeue')
    rows.insert(-1, notice('user', 4.05))
    record, state = read(tmp_path, rows)
    assert state.queued == ()
    receipts = [item for item in claude.inputs(record) if item.text.startswith('<task-notification>')]
    assert len(receipts) == 1
    assert receipts[0].consumed_at == AT + timedelta(seconds=4.05)


def test_terminal_notice_between_call_and_handle_result_ends_the_matching_task(tmp_path):
    record, state = read(tmp_path, load('notice-before-result'))
    assert state.pending_tools == () and state.active_tools == ()
    task = claude._Reader(record).background.tasks[CALL]
    assert task.started == AT + timedelta(seconds=1)
    assert task.latest == AT + timedelta(seconds=1.5)
    assert task.terminal == 'failed' and not task.invalid


def test_terminal_notice_before_the_call_still_invalidates_the_task(tmp_path):
    rows = load('notice-before-result')
    rows[2]['timestamp'] = (AT + timedelta(seconds=.5)).isoformat()
    _, state = read(tmp_path, rows)
    assert state.pending_tools is None


def test_failed_background_invocation_with_string_result_creates_no_task(tmp_path):
    record, state = read(tmp_path, load('background-error-string'))
    assert state.pending_tools == () and state.active_tools == ()
    reader = claude._Reader(record)
    assert not reader.background.tasks and not reader.background.unknown_calls


@pytest.mark.parametrize('fault', ['not-error', 'unmeasured', 'missing-version', 'time-before-call', 'handle'])
def test_failed_background_result_requires_the_measured_complete_failure_shape(tmp_path, fault):
    rows = load('background-error-string')
    if fault == 'not-error': rows[2]['message']['content'][0]['is_error'] = False
    elif fault == 'unmeasured':
        for entry in rows: entry['version'] = '2.1.399'
    elif fault == 'missing-version': del rows[2]['version']
    elif fault == 'time-before-call': rows[2]['timestamp'] = AT.isoformat()
    elif fault == 'handle': rows[2]['toolUseResult'] = {'backgroundTaskId': TASK}
    _, state = read(tmp_path, rows)
    assert state.pending_tools is None


def test_taskstop_of_known_monitor_never_becomes_an_unbound_bash_stop(tmp_path):
    record, state = read(tmp_path, load('monitor-stop'))
    assert state.pending_tools == ()
    background = claude._Reader(record).background
    assert MONITOR_TASK in background.stopped_monitors and not background.unbound


def test_monitor_stop_does_not_end_another_background_command(tmp_path):
    chain = load('monitor-stop')[1:]
    for entry in chain:
        entry['timestamp'] = (datetime.fromisoformat(entry['timestamp']) + timedelta(seconds=4)).isoformat()
    rows = initial() + [prompt(4)] + chain
    _, state = read(tmp_path, rows)
    assert state.pending_tools == ('Bash',)


def test_a_monitor_stop_with_no_known_monitor_stays_unknown(tmp_path):
    rows = load('monitor-stop')
    del rows[1:3]
    _, state = read(tmp_path, rows)
    assert state.pending_tools is None


def test_old_resume_stopped_task_ends_by_relaunch_without_claiming_completion(tmp_path):
    record, state = read(tmp_path, load('old-resume-stopped'))
    assert state.pending_tools is None
    reader = claude._Reader(record)
    projection = list(reader.background.task_states(AT + timedelta(seconds=10)))
    assert projection[0].phase == 'ended-by-relaunch' and projection[0].terminal is None
    assert reader.state(launched_at=AT + timedelta(seconds=10)).pending_tools == ()
    assert reader.background.tasks[CALL].phase == 'unknown'
    assert reader.state().pending_tools is None


@pytest.mark.parametrize('version', ['2.1.317', '2.1.325'])
def test_old_pending_command_ends_by_measured_program_exit(tmp_path, version):
    rows = initial()
    for entry in rows: entry['version'] = version
    record, state = read(tmp_path, rows)
    assert state.pending_tools == ('Bash',)
    reader = claude._Reader(record)
    assert list(reader.background.task_states(AT + timedelta(seconds=10)))[0].phase == 'ended-by-relaunch'
    assert reader.state(launched_at=AT + timedelta(seconds=10)).pending_tools == ()


@pytest.mark.parametrize('fault', ['no-launch', 'new-command', 'new-result', 'new-running', 'invalid', 'unclassified-notice'])
def test_relaunch_never_settles_unproved_or_current_work(tmp_path, fault):
    rows = load('old-resume-stopped');launch = AT + timedelta(seconds=10)
    if fault == 'no-launch': launch = None
    elif fault == 'new-command': launch = AT + timedelta(seconds=1)
    elif fault == 'new-result': rows[2]['timestamp'] = (AT + timedelta(seconds=11)).isoformat()
    elif fault == 'new-running': rows.append(row('attachment', 11, attachment=dict(type='task_status', taskId=TASK, taskType='local_bash', status='running')))
    elif fault == 'invalid': rows.append(notice('user', 6, task='synthetic wrong task'))
    elif fault == 'unclassified-notice': rows[-1]['message']['content'] = text(status='stopped', note=True)
    record, _ = read(tmp_path, rows)
    assert claude._Reader(record).state(launched_at=launch).pending_tools != ()
