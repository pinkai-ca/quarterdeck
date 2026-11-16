"""Ordinary synthetic crew notice/queue shapes; no office record or protected fixture."""

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from quarterdeck import human_wake, idle
from quarterdeck.harness import claude
from quarterdeck.harness.types import Join, Screen


AT = datetime(2032, 4, 5, 6, 7, tzinfo=timezone.utc)
SESSION = 'synthetic-conversation'
VERSION = '2.1.317'
NOTICE = ('<task-notification>\n<task-id>synthetic-task</task-id>\n'
          '<tool-use-id>synthetic-call</tool-use-id>\n<output-file>/synthetic/output</output-file>\n'
          '<status>completed</status>\n<summary>Synthetic background task ended.</summary>\n'
          '</task-notification>')


def row(kind, second, **fields):
    result = dict(type=kind, timestamp=(AT + timedelta(seconds=second)).isoformat(),
                  sessionId=SESSION, **fields)
    if kind != 'queue-operation':
        result.update(version=VERSION, isSidechain=False)
    return result


def prompt(second=0, text='Synthetic input'):
    return row('user', second, message=dict(role='user', content=text))


def enqueue(second=4, text=NOTICE):
    return row('queue-operation', second, operation='enqueue', content=text)


def end(second):
    return row('system', second, subtype='turn_duration')


def initial():
    return [prompt(), row('assistant', 1, message=dict(role='assistant', content=[dict(
        type='tool_use', id='synthetic-call', name='Bash',
        input=dict(command='synthetic background command', run_in_background=True))])),
        row('user', 2, message=dict(role='user', content=[dict(type='tool_result',
            tool_use_id='synthetic-call', content='Synthetic acknowledgement.', is_error=False)]),
            toolUseResult=dict(stdout='', stderr='', interrupted=False, isImage=False,
                               noOutputExpected=False, backgroundTaskId='synthetic-task')),
        end(3)]


def write(path, rows, *, append=False):
    with path.open('a' if append else 'w') as stream:
        for entry in rows:
            stream.write(json.dumps(entry) + '\n')
    return claude.state(path)


@pytest.mark.parametrize('carrier', ['user', 'attachment'])
def test_own_notice_taken_in_is_not_queued(tmp_path, carrier):
    record = tmp_path / 'synthetic.jsonl'
    before = write(record, initial() + [enqueue()])
    assert before.queued == (NOTICE,)
    if carrier == 'user':
        receipt = row('user', 5, origin=dict(kind='task-notification', producer='session-task',
                                           runId='synthetic-run'),
                      message=dict(role='user', content=NOTICE))
    else:
        receipt = row('attachment', 5, attachment=dict(type='queued_command',
            commandMode='task-notification', origin=dict(kind='task-notification', producer='session-task'),
            prompt=NOTICE, runId='synthetic-run', source_uuid='synthetic-source',
            delivery_id='synthetic-delivery', timestamp=(AT + timedelta(seconds=5)).isoformat()))
    after = write(record, [receipt], append=True)
    assert after.queued == ()


@pytest.mark.parametrize('text', [NOTICE, 'Synthetic ordinary queued input'], ids=['notice', 'prompt'])
def test_completed_turn_leaves_only_the_still_pending_queue(tmp_path, text):
    record = tmp_path / 'synthetic.jsonl'
    prefix = initial() if text == NOTICE else [prompt(), end(3)]
    state = write(record, prefix + [enqueue(text=text), prompt(5, 'Synthetic later turn'), end(7)])
    assert state.queued == ()
    assert state.queue_error is None
    assert idle.check(state) is None
    assert text not in state.consumed  # completion expires queue state; it fabricates no receipt
    inputs = claude.inputs(record)
    assert any(item.text == text and item.kind == 'queued' for item in inputs)
    pending = 'Synthetic input after the last completion'
    state = write(record, [enqueue(8, pending)], append=True)
    assert state.queued == (pending,)
    assert idle.check(state).dimension == 'queue-pending'


def test_incremental_queue_expiry_matches_a_cold_read_and_keeps_equal_new_input(tmp_path):
    record, cold = tmp_path / 'incremental.jsonl', tmp_path / 'cold.jsonl'
    rows = initial() + [enqueue(text='Synthetic repeated input'), prompt(5)]
    assert write(record, rows).queued == ('Synthetic repeated input',)
    suffix = [end(7), enqueue(8, 'Synthetic repeated input')]
    incremental = write(record, suffix, append=True)
    full = write(cold, rows + suffix)
    assert incremental.queued == full.queued == ('Synthetic repeated input',)
    assert incremental.completed_turns == full.completed_turns
    assert write(record, [prompt(9, 'Synthetic repeated input')], append=True).queued == ()


def test_killed_task_notice_followed_by_a_completed_turn_no_longer_holds_idle(tmp_path):
    killed = NOTICE.replace('<status>completed</status>', '<status>killed</status>')
    state = write(tmp_path / 'synthetic.jsonl', initial() + [enqueue(text=killed), prompt(5), end(7)])
    assert state.queued == () and state.pending_tools == ()
    assert idle.check(state) is None


@pytest.mark.parametrize('change', ['future-version', 'extra-field', 'other-origin', 'other-session', 'earlier-time'])
def test_an_unmeasured_attachment_is_not_a_queue_receipt(tmp_path, change):
    receipt = row('attachment', 5, attachment=dict(type='queued_command',
        commandMode='task-notification', origin=dict(kind='task-notification', producer='session-task'),
        prompt=NOTICE, runId='synthetic-run', source_uuid='synthetic-source',
        delivery_id='synthetic-delivery', timestamp=(AT + timedelta(seconds=5)).isoformat()))
    if change == 'future-version':
        receipt['version'] = 'synthetic-unmeasured'
    elif change == 'extra-field':
        receipt['attachment']['futureField'] = True
    elif change == 'other-origin':
        receipt['attachment']['origin'] = {'kind': 'synthetic-other'}
    elif change == 'other-session':
        receipt['sessionId'] = 'synthetic-other-conversation'
    else:
        receipt['attachment']['timestamp'] = (AT + timedelta(seconds=1)).isoformat()
    state = write(tmp_path / 'synthetic.jsonl', initial() + [enqueue(), receipt])
    assert state.queued == (NOTICE,)
    assert idle.check(state) is not None


@pytest.mark.parametrize('change', ['missing-time', 'future-time', 'missing-session', 'other-session',
                                  'extra-field', 'future-version', 'backward-completion', 'malformed-notice'])
def test_unmeasured_or_unordered_input_is_not_expired_by_a_completion(tmp_path, change):
    queued, completed = enqueue(), end(7)
    if change == 'missing-time':
        queued.pop('timestamp')
    elif change == 'future-time':
        queued['timestamp'] = (AT + timedelta(seconds=9)).isoformat()
    elif change == 'missing-session':
        queued.pop('sessionId')
    elif change == 'other-session':
        queued['sessionId'] = 'synthetic-other-conversation'
    elif change == 'extra-field':
        queued['futureField'] = True
    elif change == 'future-version':
        completed['version'] = 'synthetic-unmeasured'
    elif change == 'backward-completion':
        completed['timestamp'] = (AT + timedelta(seconds=1)).isoformat()
    else:
        queued['content'] = '<task-notification>Unmeasured synthetic shape</task-notification>'
    state = write(tmp_path / 'synthetic.jsonl', initial() + [queued, prompt(5), completed])
    assert state.queued == (queued['content'],)
    assert idle.check(state) is not None


def test_assistant_stop_or_new_prompt_does_not_expire_the_queue(tmp_path):
    record = tmp_path / 'synthetic.jsonl'
    rows = initial() + [enqueue(), prompt(5), row('assistant', 6, message=dict(
        role='assistant', stop_reason='end_turn', content=[dict(type='text', text='Synthetic reply.')]))]
    assert write(record, rows).queued == (NOTICE,)
    assert write(record, [prompt(8, 'Synthetic next input')], append=True).queued == (NOTICE,)
    assert write(record, [end(10)], append=True).queued == ()


def test_completing_a_turn_cannot_end_a_background_tool_or_resolve_an_unknown_queue_operation(tmp_path):
    record = tmp_path / 'synthetic.jsonl'
    pending = 'Synthetic queued input'
    state = write(record, initial() + [enqueue(text=pending), prompt(5), end(7)])
    assert state.queued == ()
    assert state.pending_tools == ('Bash',)
    assert idle.check(state).dimension == 'tools-pending'
    state = write(record, [enqueue(8, pending), row('queue-operation', 9,
                  operation='synthetic-unmeasured', content=pending), prompt(10), end(12)], append=True)
    assert state.queued == (pending,)
    assert state.queue_error == 'unknown queue operation'


def test_completed_notice_releases_idle_and_positive_human_wake_eligibility(tmp_path, monkeypatch):
    record = tmp_path / 'synthetic.jsonl'
    before = write(record, initial() + [enqueue(), prompt(5)])
    assert before.queued == (NOTICE,)
    pane = SimpleNamespace(dead=False, in_mode=False)
    seat = SimpleNamespace(launched=lambda: object(), pane=lambda: pane,
                           join=lambda: Join(record), harness=SimpleNamespace(state=claude.state))
    # Isolate the record consumer: the existing metadata and delivery gates are unchanged.
    monkeypatch.setattr(human_wake, 'candidate_hold', lambda *args: None)
    screen = Screen(text='Synthetic empty composer', box='', working=False)
    assert human_wake.eligible(seat, screen) == 'human wake waiting for recorded work to end'
    state = write(record, [end(7)], append=True)
    assert idle.check(state) is None
    assert human_wake.eligible(seat, screen) is None
