"""A successful local_bash TaskStop measured on an isolated native 2.1.325 run."""
import json
from pathlib import Path

import pytest

from quarterdeck.harness import claude
from tests.unit.test_claude_background_ordinary import CALL, call, read, result


def fixture():
    path = Path(__file__).parents[1] / 'fixtures/claude/2.1.325/task-stop.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_matching_taskstop_2325_ends_its_background_bash(tmp_path):
    record, state = read(tmp_path, fixture())
    task = claude._Reader(record).background.tasks[CALL]
    assert task.phase == 'ended' and task.terminal == 'task-stop'
    assert state.pending_tools == () and state.active_tools == ()


@pytest.mark.parametrize('fault', [
    'call-version', 'result-version', 'unmeasured-version', 'missing-call-version',
    'missing-result-version', 'wrong-result-target', 'unknown-target',
    'wrong-task-type', 'no-success', 'missing-result', 'result-before-call',
])
def test_taskstop_2325_requires_measured_matching_success(tmp_path, fault):
    rows = fixture()
    stop, stopped = rows[5:7]
    value = stopped['toolUseResult']
    if fault == 'call-version': stop['version'] = '2.1.317'
    elif fault == 'result-version': stopped['version'] = '2.1.317'
    elif fault == 'unmeasured-version':
        stop['version'] = stopped['version'] = '2.1.322'
    elif fault == 'missing-call-version': del stop['version']
    elif fault == 'missing-result-version': del stopped['version']
    elif fault == 'wrong-result-target': value['task_id'] = 'task-other'
    elif fault == 'unknown-target':
        stop['message']['content'][0]['input']['task_id'] = value['task_id'] = 'task-other'
    elif fault == 'wrong-task-type': value['task_type'] = 'synthetic-unmeasured-kind'
    elif fault == 'no-success': value['message'] = 'synthetic undecided result'
    elif fault == 'missing-result': del stopped['toolUseResult']
    elif fault == 'result-before-call': stopped['timestamp'] = rows[4]['timestamp']
    record, state = read(tmp_path, rows)
    assert claude._Reader(record).background.tasks[CALL].phase == 'pending'
    assert state.pending_tools is None


def test_failed_taskstop_2325_does_not_end_the_background_bash(tmp_path):
    rows = fixture()
    rows[6]['message']['content'][0]['is_error'] = True
    rows[6]['toolUseResult'] = 'synthetic stop error'
    _, state = read(tmp_path, rows)
    assert state.pending_tools == ('Bash',)


def test_taskstop_2325_leaves_another_background_bash_pending(tmp_path):
    rows = fixture()
    other = [call(2.1, 'call-oak'), result(2.2, 'call-oak', 'task-oak')]
    for entry in other: entry['version'] = '2.1.325'
    rows[3:3] = other
    record, state = read(tmp_path, rows)
    tasks = claude._Reader(record).background.tasks
    assert tasks[CALL].phase == 'ended'
    assert tasks['call-oak'].phase == 'pending'
    assert state.pending_tools == ('Bash',)
