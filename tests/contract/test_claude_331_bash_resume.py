"""Synthetic replay of a crew restart with an acknowledged ordinary Bash task."""

from contextlib import nullcontext
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from quarterdeck import door, idle, input_receipt, recovery, relaunch_intent, resting_failover, service
from quarterdeck.harness import claude, json_record
from quarterdeck.service import Service

from .test_claude_own_watch import AT, CALL, DESC, END, NEW, OUT, SESSION, TASK, origin, queue, record, resume, row, stamp, typed


COMMAND = 'sleep 90'
DESCRIPTION = 'Synthetic ordinary Bash work'
FIRST = 'Run ordinary background work.'
BRIEF = 'qd > briefed as worker'
CALL_ROW = 'synthetic-call-row'
ACK = (f'Command running in background with ID: {TASK}. Output is being written to: {OUT}. '
       'You will be notified when it completes — do not poll that file or sleep while waiting. '
       'Read the file only if the user asks for progress, or if you need a line it prints before you can go on, '
       "such as a server's ready line or port.")


def before_restart(command=COMMAND):
    first, call, ack, end = origin(NEW, command)
    first['message']['content'] = FIRST
    call['uuid'] = CALL_ROW
    call['message']['content'][0]['input']['description'] = DESCRIPTION
    ack.update(sourceToolAssistantUUID=CALL_ROW, parentUuid=CALL_ROW)
    ack['message']['content'][0]['content'] = ACK
    ack['toolUseResult'].update(stdout='', stderr='', interrupted=False, isImage=False, noOutputExpected=False)
    prompt = dict(type='last-prompt', sessionId=SESSION, leafUuid=END, lastPrompt=FIRST)
    cost = dict(type='cost-state', sessionId=SESSION, totalCostUSD=0, totalAPIDuration=0,
                totalAPIDurationWithoutRetries=0, totalToolDuration=0, totalLinesAdded=0,
                totalLinesRemoved=0, totalDuration=100, startTime=100, modelUsage={}, hasUnknownModelCost=False)
    return [first, call, ack, end, prompt, cost]


def snapshot(message, seconds):
    return dict(type='file-history-snapshot', messageId=message, isSnapshotUpdate=False,
                snapshot=dict(messageId=message, timestamp=stamp(seconds), trackedFileBackups={}))


def resume_rows():
    killed, killed_dequeue, tools, stopped, stopped_dequeue, _, resumed = resume()
    killed['content'] = killed['content'].replace(DESC, DESCRIPTION)
    resumed.update(timestamp=stamp(6.013), entrypoint='cli', userType='external',
                   permissionMode='bypassPermissions', cwd='/synthetic/workspace', gitBranch='HEAD', dataFlowOn=True)
    return [killed, killed_dequeue, tools, snapshot('synthetic-nonpersisted', 6), stopped, stopped_dequeue,
            queue('enqueue', 6.014), queue('dequeue', 6.027), snapshot('synthetic-brief', 6.065), resumed]


def brief_turn(parent):
    brief = typed(6.062, 'synthetic-brief-prompt', BRIEF)
    brief.update(uuid='synthetic-brief', parentUuid=parent, promptSource='queued')
    reply = row('assistant', 7, NEW, uuid='synthetic-reply', parentUuid=brief['uuid'],
                message=dict(role='assistant', stop_reason='end_turn', content=[dict(type='text', text='Ready.')]))
    end = row('system', 8, NEW, uuid='synthetic-next-end', parentUuid=reply['uuid'], subtype='turn_duration')
    return [brief, reply, end]


def append(path, entry, *, complete=True):
    with path.open('a') as stream:
        stream.write(json.dumps(entry) + ('\n' if complete else ''))


def test_the_measured_resume_settles_only_after_the_native_carrier(tmp_path):
    suffix = resume_rows()
    path = record(tmp_path, before_restart())
    assert claude.state(path).active_tools[0].inbox_watch_started is None
    assert claude.state(path).pending_tools == ('Bash',)
    assert claude.inputs(path)[0].text == FIRST
    for entry in suffix[:-1]:
        append(path, entry)
        state = claude.state(path)
        assert state.queue_error == 'task notification queue unresolved'
        assert state.turns == 1 and state.queued == state.consumed == ()
        assert idle.check(state, explicit=True).dimension == 'tools-unknown'
        assert claude.inputs(path).skipped

    carrier = suffix[-1]
    append(path, carrier, complete=False)
    assert idle.check(claude.state(path)) is not None
    assert claude.inputs(path).skipped
    with path.open('a') as stream:
        stream.write('\n')
    state, turns = claude.read(path)
    assert state.turns == len(turns) == 1 and not state.turn_open
    assert state.queued == state.consumed == state.active_tools == state.pending_tools == ()
    assert state.last_input_at == AT and state.queue_error is None
    assert [item.text for item in claude.inputs(path)] == [FIRST]
    assert not claude.inputs(path).skipped
    harness = claude.ClaudeHarness()
    since = AT + timedelta(seconds=5)
    assert input_receipt.confirmed(harness, path, carrier['message']['content'], since) is None
    assert input_receipt.confirmed(harness, path, BRIEF, since) is None
    assert AT + timedelta(seconds=6.013) not in claude.person_inputs(path, ())

    brief, reply, end = brief_turn(carrier['uuid'])
    append(path, brief)
    assert input_receipt.confirmed(harness, path, BRIEF, since) == 'delivered'
    append(path, reply)
    assert idle.check(claude.state(path)).dimension == 'turn-open'
    append(path, end)
    warm, turns = claude.read(path)
    assert [turn.input for turn in turns] == [FIRST, BRIEF]
    assert idle.check(warm) is None
    assert idle.check(harness.state_for_relaunch(path, since), explicit=True) is None

    json_record.release(path)
    assert claude.read(path) == (warm, turns)
    assert [item.text for item in claude.inputs(path)] == [FIRST, BRIEF]
    task, = claude._Reader(path).background.task_states()
    assert (task.phase, task.terminal, task.inbox_watch_started) == ('ended-by-relaunch', None, None)


@pytest.mark.parametrize('fault', [
    'wrong-task', 'wrong-call', 'foreign-session', 'wrong-prior-leaf', 'wrong-checkpoint',
    'wrong-description', 'wrong-output', 'wrong-ack-task', 'wrong-ack-source', 'interrupted-ack', 'unreadable-ack',
    'wrong-version', 'watch', 'no-checkpoint', 'wrong-tools', 'missing-snapshot', 'snapshot-id',
    'snapshot-time', 'stop-before-snapshot', 'missing-dequeue', 'order', 'empty-content',
    'undated-notice', 'old-notice', 'late-notice', 'changed-note', 'other-carrier', 'other-input',
])
def test_a_different_sequence_cannot_settle_ordinary_bash(tmp_path, fault):
    before = before_restart(command='qd inbox --wait' if fault == 'watch' else COMMAND)
    rows = resume_rows()
    killed, _, tools, checkpoint, stopped, _, textless, _, _, carrier = rows
    if fault in ('wrong-task', 'wrong-call', 'wrong-output', 'wrong-description'):
        old = {'wrong-task': TASK, 'wrong-call': CALL, 'wrong-output': OUT, 'wrong-description': DESCRIPTION}[fault]
        killed['content'] = killed['content'].replace(old, 'different-synthetic-value')
    elif fault == 'foreign-session':
        textless['sessionId'] = 'different-session'
    elif fault == 'wrong-prior-leaf':
        carrier['parentUuid'] = 'different-leaf'
    elif fault == 'wrong-checkpoint':
        before[-2]['leafUuid'] = 'different-leaf'
    elif fault == 'wrong-ack-task':
        before[2]['message']['content'][0]['content'] = ACK.replace(TASK, 'different-task')
    elif fault == 'wrong-ack-source':
        before[2]['sourceToolAssistantUUID'] = 'different-call-row'
    elif fault == 'interrupted-ack':
        before[2]['toolUseResult']['interrupted'] = True
    elif fault == 'unreadable-ack':
        before[2]['message']['content'][0]['content'] = 'synthetic unreadable acknowledgement'
    elif fault == 'wrong-version':
        carrier['version'] = '2.1.332'
    elif fault == 'no-checkpoint':
        before.pop()
    elif fault == 'wrong-tools':
        tools['tools'] = ['Read']
    elif fault == 'missing-snapshot':
        rows.remove(checkpoint)
    elif fault == 'snapshot-id':
        checkpoint['snapshot']['messageId'] = 'different-checkpoint'
    elif fault == 'snapshot-time':
        rows[-2]['snapshot']['timestamp'] = stamp(6.020)
    elif fault == 'stop-before-snapshot':
        stopped['timestamp'] = stamp(5.99)
    elif fault == 'missing-dequeue':
        del rows[7]
    elif fault == 'order':
        rows[5], rows[6] = rows[6], rows[5]
    elif fault == 'empty-content':
        textless['content'] = ''
    elif fault in ('undated-notice', 'old-notice', 'late-notice'):
        carrier['timestamp'] = {'undated-notice': 'unknown', 'old-notice': stamp(6.009),
                                'late-notice': stamp(6.030)}[fault]
    elif fault == 'changed-note':
        carrier['message']['content'] = carrier['message']['content'].replace('partial results', 'results')
    elif fault == 'other-carrier':
        carrier['queueTranscriptOnly'] = False
    elif fault == 'other-input':
        rows.insert(5, typed(6.005, 'unrelated-prompt', 'An unrelated queued reply.'))
    path = record(tmp_path, before + rows)
    reader = claude._Reader(path)
    assert all(task.phase != 'ended-by-relaunch' for task in reader.background.task_states())
    assert idle.check(reader.state()) is not None
    assert idle.check(reader.state(launched_at=AT + timedelta(seconds=5)), explicit=True) is not None


def test_ordinary_and_explicit_gates_use_the_settled_record(tmp_path, monkeypatch):
    suffix = resume_rows()
    path = record(tmp_path, before_restart() + suffix[:-1])
    harness = claude.ClaudeHarness()
    pane = SimpleNamespace(id='%synthetic', pid=1, dead=False)
    launch = SimpleNamespace(pane=pane.id, pid=pane.pid, at=stamp(5), harness='claude', version=NEW)
    seat = SimpleNamespace(name='worker', harness=harness, session=SimpleNamespace(human=False),
                           ship=SimpleNamespace(runtime=tmp_path), trusted=tmp_path / 'trusted',
                           spec=SimpleNamespace(pool_choices=()), is_up=True, is_working=lambda: False,
                           pane=lambda: pane, launched=lambda: launch, record=lambda: path,
                           restart=lambda: 'Synthetic restart.')
    controls = SimpleNamespace(begin_model_control=lambda *_: 'Control reached its own gate.')
    stand_in = lambda seat: SimpleNamespace(screen=lambda: None)
    monkeypatch.setattr(service, 'Door', stand_in)
    monkeypatch.setattr(door, 'Door', stand_in)
    monkeypatch.setattr(resting_failover, 'require_clear', lambda seat: None)
    monkeypatch.setattr(recovery, 'held', lambda seat: None)
    monkeypatch.setattr(relaunch_intent, 'section', lambda *args, **kwargs: nullcontext())

    assert Service.control(controls, seat, 'model', 'synthetic-model') is None
    assert controls.request_wait[0] == 'tools-unknown'
    assert Service.not_idle(None, seat) is not None
    assert Service._wall_idle_hold(None, seat) is not None
    assert Service.relaunch(None, seat, True) is None

    append(path, suffix[-1])
    for entry in brief_turn(suffix[-1]['uuid']):
        append(path, entry)
    assert Service.not_idle(None, seat) is None
    assert Service._wall_idle_hold(None, seat) is None
    assert Service.control(controls, seat, 'model', 'synthetic-model') == 'Control reached its own gate.'
    assert Service.relaunch(None, seat, True) == 'Synthetic restart.'
