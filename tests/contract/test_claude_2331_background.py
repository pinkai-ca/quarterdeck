"""Synthetic identities and clocks for the signed-in background-task record forms."""

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from quarterdeck import idle
from quarterdeck.harness import claude, claude_record, json_record


VERSION = '2.1.331'
AT = datetime(2032, 4, 5, 6, 7, tzinfo=timezone.utc)
SESSION = 'synthetic-session'
SHELL_CALL = 'synthetic-shell-call'
AGENT_CALL = 'synthetic-agent-call'
STOP_CALL = 'synthetic-stop-call'
CHILD = 'synthetic-agent'
SHELL = 'synthetic-shell'
CHILD_SHELL = 'synthetic-child-shell'
CHILD_CALL = 'synthetic-child-shell-call'
OUTPUT = '/synthetic/agent.output'
INTERIM = ('This agent stopped with background work of its own still running. It may resume on its own when that '
           'work completes or reports, and the same task-id notifies again if it does; the result below may be interim.')
FINISHED = ('A task-notification fires each time this agent stops with no live background children of its own. '
            'The user can send it another message and resume it, so the same task-id may notify more than once.')


def row(kind, second, **fields):
    return dict(type=kind, timestamp=(AT + timedelta(seconds=second)).isoformat(),
                sessionId=SESSION, isSidechain=False, version=VERSION, **fields)


def prompt(second, text='synthetic prompt'):
    return row('user', second, message=dict(role='user', content=text), origin=dict(kind='human'))


def tool(second, call, name, **arguments):
    return row('assistant', second, message=dict(role='assistant', stop_reason='tool_use', content=[
        dict(type='tool_use', id=call, name=name, input=arguments),
    ]))


def result(second, call, content='synthetic result', **mirror):
    return row('user', second, message=dict(role='user', content=[
        dict(type='tool_result', tool_use_id=call, content=content),
    ]), toolUseResult=mirror)


def shell_rows(task):
    return [prompt(0), tool(1, SHELL_CALL, 'Bash', command='sleep 120', run_in_background=True),
            result(2, SHELL_CALL, backgroundTaskId=task, interrupted=False, isImage=False,
                   noOutputExpected=False, stdout='', stderr='')]


def agent_rows():
    return [tool(3, AGENT_CALL, 'Agent', description='Reply CHILD', prompt='Reply exactly CHILD.'),
            result(4, AGENT_CALL, [dict(type='text', text='synthetic launch result')],
                   isAsync=True, status='async_launched', agentId=CHILD, description='Reply CHILD',
                   resolvedModel='synthetic-model', prompt='Reply exactly CHILD.',
                   outputFile=OUTPUT, canReadOutputFile=True, canContinueAgent=True)]


def child_rows(*, background=False):
    start = row('user', 3.5, message=dict(role='user', content='Reply exactly CHILD.'))
    rows = [start, dict(type='data-flow-records', v=1, sessionId=SESSION, agentId=CHILD, records=[])]
    if background:
        refused = result(4.1, 'synthetic-foreground', 'synthetic refusal')
        refused['message']['content'][0]['is_error'] = True
        rows.extend([tool(4, 'synthetic-foreground', 'Bash', command='sleep 25'), refused,
                     tool(4.2, CHILD_CALL, 'Bash', command='sleep 25', run_in_background=True),
                     result(4.3, CHILD_CALL, backgroundTaskId=CHILD_SHELL)])
    rows.append(row('assistant', 5, message=dict(role='assistant', stop_reason='end_turn', content=[
        dict(type='text', text='CHILD'),
    ])))
    for entry in rows:
        if entry['type'] != 'data-flow-records':
            entry.update(isSidechain=True, agentId=CHILD)
    return rows


def duration(second):
    return row('system', second, subtype='turn_duration')


def compact(second):
    return row('system', second, subtype='compact_boundary', compactMetadata=dict(trigger='manual'))


def metadata(kind, **fields):
    return dict(type=kind, v=1, sessionId=SESSION, **fields)


def stop_rows(second, task=SHELL):
    mirror = dict(message=f'Successfully stopped task: {task} (sleep 120)', task_id=task,
                  task_type='local_bash', command='sleep 120')
    return [prompt(second, 'Stop the background task.'), tool(second + 1, STOP_CALL, 'TaskStop', task_id=task),
            result(second + 2, STOP_CALL, json.dumps(mirror), **mirror), duration(second + 3)]


def notice_text(kind, *, output=OUTPUT, task=CHILD, call=AGENT_CALL):
    header = f'<task-id>{task}</task-id>\n'
    if kind != 'finished':
        header += f'<tool-use-id>{call}</tool-use-id>\n'
    header += f'<output-file>{output}</output-file>\n<status>completed</status>\n'
    if kind == 'bash':
        body = '<summary>Background command "Synthetic" completed (exit code 0)</summary>\n'
    else:
        summary = 'finished' if kind == 'finished' else 'is waiting on its own background work'
        note = FINISHED if kind == 'finished' else INTERIM
        body = (f'<summary>Agent "Synthetic" {summary}</summary>\n<note>{note}</note>\n<result>CHILD</result>\n'
                '<usage><subagent_tokens>8</subagent_tokens><tool_uses>2</tool_uses>'
                '<duration_ms>1000</duration_ms></usage>\n')
    return f'<task-notification>\n{header}{body}</task-notification>'


def queue(second, operation, **fields):
    return dict(type='queue-operation', timestamp=(AT + timedelta(seconds=second)).isoformat(),
                sessionId=SESSION, operation=operation, **fields)


def agent_notice(second, kind='finished', **fields):
    text = notice_text(kind, **fields)
    prompt_id = f'synthetic-prompt-{second}'
    return [queue(second, 'enqueue', content=text), queue(second + .01, 'dequeue'),
            row('user', second + .02, message=dict(role='user', content=text),
                origin=dict(kind='task-notification', producer='session-task', runId=f'synthetic-run-{second}'),
                promptSource='system', rowSource='recorded', turnOrigin='task_notification',
                queueSkipAttachments=True, promptId=prompt_id, turnPromptId=prompt_id)]


def write(path, rows, mode='w'):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open(mode) as stream:
        stream.writelines(json.dumps(entry) + '\n' for entry in rows)


def fixture(tmp_path, root, child=None):
    record = tmp_path / f'{SESSION}.jsonl'
    write(record, root)
    if child is not None:
        write(record.with_suffix('') / 'subagents' / f'agent-{CHILD}.jsonl', child)
    return record


def child_path(record):
    return record.with_suffix('') / 'subagents' / f'agent-{CHILD}.jsonl'


@pytest.mark.parametrize('difference', ['undated-call', 'undated-ack', 'undated-stop', 'stripped-id',
                                         'wrong-task', 'wrong-session', 'later-version'])
def test_a_shell_stop_requires_bound_timed_native_rows(tmp_path, difference):
    shell = '<str 9>' if difference == 'stripped-id' else SHELL
    rows = [*shell_rows(shell), duration(3), *stop_rows(4, 'other-shell' if difference == 'wrong-task' else shell)]
    if difference.startswith('undated-'):
        rows[{'undated-call': 1, 'undated-ack': 2, 'undated-stop': 6}[difference]]['timestamp'] = '<timestamp>'
    if difference == 'wrong-session':
        rows[6]['sessionId'] = 'other-session'
    if difference == 'later-version':
        rows[6]['version'] = '2.1.332'
    state = claude.state(fixture(tmp_path, rows))
    assert state.pending_tools is None


def test_textless_dequeue_does_not_choose_a_slot_or_supply_a_receipt(tmp_path):
    before = dict(type='queue-operation', operation='enqueue', sessionId=SESSION,
                  timestamp=(AT + timedelta(seconds=5)).isoformat(), content='synthetic queued text')
    after = dict(type='queue-operation', operation='dequeue', sessionId=SESSION,
                 timestamp=(AT + timedelta(seconds=5.1)).isoformat())
    record = fixture(tmp_path, [*shell_rows('synthetic-shell'), *agent_rows(), before,
                                row('attachment', 5, attachment=dict(type='total_tokens_reminder')), after,
                                row('system', 6, subtype='turn_duration')], child_rows())
    state = claude.state(record)
    assert state.queued == (before['content'],) and state.consumed == ()


def test_measured_root_and_child_lifetimes(tmp_path):
    record = fixture(tmp_path, [*shell_rows(SHELL), *agent_rows(), duration(6)], child_rows(background=True))
    assert claude.state(record).pending_tools == ('Bash', 'Bash', 'Agent')

    interim = agent_notice(7, 'interim')
    write(record, [interim[0], metadata('data-flow-records', records=[dict(response_id='msg_Synthetic1')]),
                   interim[1]], 'a')
    waiting = claude.state(record)
    assert waiting.queue_error == 'task notification queue unresolved' and waiting.consumed == ()
    assert waiting.pending_tools is None and idle.check(waiting, explicit=True).dimension == 'tools-unknown'
    write(record, interim[2:], 'a')
    replying = claude.state(record)
    assert replying.turn_open is True and replying.queue_error is None
    assert replying.pending_tools == ('Bash', 'Bash', 'Agent')

    text = notice_text('bash', task=CHILD_SHELL, call=CHILD_CALL, output='/synthetic/child-shell.output')
    write(record, [duration(8), queue(12, 'enqueue', content=text),
                   queue(12.04, 'remove', content=text, reason='delivered_to_agent')], 'a')
    assert claude.state(record).pending_tools == ('Bash', 'Agent')
    callback = row('user', 12.03, message=dict(role='user', content='synthetic callback'), isMeta=True,
                   origin=dict(kind='task-notification', producer='session-task'), joinedNoticeOwn=[True],
                   joinedNoticeLengths=[len(text)], joinedNoticeRunIds=['synthetic-child-run'])
    end = child_rows()[-1]
    end['timestamp'] = (AT + timedelta(seconds=13)).isoformat()
    callback.update(isSidechain=True, agentId=CHILD)
    write(child_path(record), [callback, end], 'a')
    assert claude.state(record).pending_tools == ('Bash', 'Agent')

    final = agent_notice(14)
    write(record, [*final[:2], metadata('data-flow-incomplete', codes=[9]), final[2],
                   duration(15), *stop_rows(16), compact(20), metadata('data-flow-records', records=[])], 'a')
    state, turns = claude.read(record)
    assert state.pending_tools == () and state.active_tools == () and state.queue_error is None
    assert state.queued == state.consumed == () and idle.check(state) is None
    assert [(turn.started, turn.input) for turn in turns if turn.input == ''] == [
        (AT + timedelta(seconds=7.02), ''), (AT + timedelta(seconds=14.02), ''),
    ]
    inputs = claude.inputs(record)
    assert [entry.text for entry in inputs] == ['synthetic prompt', 'Stop the background task.']
    assert not inputs.skipped


def test_invalid_root_metadata_cannot_supply_a_task_queue_version_or_rest(tmp_path):
    notice = agent_notice(7)
    invalid = metadata('data-flow-records', records=[], version='2.1.317')
    record = fixture(tmp_path, [prompt(0), *agent_rows(), duration(6), notice[0], invalid, *notice[1:], duration(8)],
                     child_rows())

    state = claude.state(record)
    assert state.turn_open is False and state.queue_error is None and state.pending_tools is None
    inputs = claude.inputs(record)
    assert [entry.text for entry in inputs] == ['synthetic prompt']
    assert [item['reason'] for item in inputs.skipped] == ['unknown native data-flow metadata']


def test_terminal_overload_closes_its_own_turn_while_the_agent_is_pending(tmp_path):
    overload = row('assistant', 8, isApiErrorMessage=True, failedResponseHadNoToolCall=True,
                   error='server_error', apiErrorStatus=529, message=dict(
                       model='<synthetic>', role='assistant', type='message', stop_reason='stop_sequence',
                       stop_sequence='', content=[dict(type='text', text='Synthetic overload')]))
    record = fixture(tmp_path, [prompt(0), *agent_rows(), duration(6), prompt(7, 'Another prompt.'), overload],
                     child_rows())
    state, turns = claude.read(record)
    assert state.turn_open is False and state.pending_tools == ('Agent',)
    assert turns[-1].successful is False and turns[-1].ended == AT + timedelta(seconds=8)


@pytest.mark.parametrize('difference', ['no-dequeue', 'foreign-queue', 'different-text', 'human-origin',
                                         'untimed-notice', 'output-mismatch', 'wrong-child', 'undated-child',
                                         'old-child', 'different-child-version', 'missing-child'])
def test_an_unbound_agent_completion_preserves_a_hold(tmp_path, difference):
    notices = agent_notice(7, output='/other/output' if difference == 'output-mismatch' else OUTPUT,
                           task='other-child' if difference == 'wrong-child' else CHILD)
    if difference == 'no-dequeue':
        del notices[1]
    if difference == 'foreign-queue':
        notices[1]['sessionId'] = 'other-session'
    if difference == 'different-text':
        notices[-1]['message']['content'] = notices[-1]['message']['content'].replace('CHILD', 'OTHER')
    if difference == 'human-origin':
        notices[-1]['origin'] = dict(kind='human')
    if difference == 'untimed-notice':
        notices[-1]['timestamp'] = '<timestamp>'
    children = child_rows()
    if difference == 'undated-child':
        children[-1]['timestamp'] = '<timestamp>'
    if difference == 'old-child':
        children[0]['timestamp'] = AT.isoformat()
    if difference == 'different-child-version':
        children[-1]['version'] = '2.1.325'
    if difference == 'missing-child':
        children = None
    record = fixture(tmp_path, [prompt(0), *agent_rows(), duration(6), *notices, duration(8)], children)
    state = claude.state(record)
    assert state.consumed == () and idle.check(state) is not None
    assert state.pending_tools in (None, ('Agent',))


def test_native_notice_queue_does_not_consume_a_human_slot(tmp_path):
    record = fixture(tmp_path, [prompt(0), *agent_rows(), duration(6),
                               queue(7, 'enqueue', content='Human follow-up.'), *agent_notice(8), duration(9)],
                     child_rows())
    assert claude.state(record).queued == ('Human follow-up.',)
    history = [(entry.text, entry.kind) for entry in claude.inputs(record)]
    assert history == [('synthetic prompt', 'accepted'), ('Human follow-up.', 'queued')]


def test_a_child_record_with_no_linked_agent_result_cannot_prove_rest(tmp_path):
    record = fixture(tmp_path, [prompt(0), duration(6)], child_rows())
    assert claude.state(record).pending_tools is None


@pytest.mark.parametrize('carrier', ['late-notice', 'completed-status'])
def test_unqualified_compaction_carriers_hold_beyond_turn_completion_and_resume(tmp_path, carrier):
    if carrier == 'late-notice':
        evidence = agent_notice(8)
    else:
        evidence = [row('attachment', 8, attachment=dict(type='task_status', taskId=CHILD, status='completed',
                        taskType='<str 11>', outputFilePath=OUTPUT, canContinueAgent=True, deltaSummary=None))]
    record = fixture(tmp_path, [prompt(0), *agent_rows(), duration(6), compact(7), *evidence,
                                duration(9), prompt(10, 'Ordinary next turn.'), duration(11)], child_rows())
    state = claude.state(record)
    assert state.turn_open is False and state.queue_error is None
    hold = idle.check(state)
    assert state.pending_tools is None and hold.dimension == 'tools-unknown'
    assert idle.check(state, explicit=True).dimension == 'tools-unknown'
    harness = claude.ClaudeHarness()
    assert harness.state_for_relaunch(record, AT + timedelta(seconds=12)).pending_tools is None

    rule = '─' * 120
    screen = harness.screen(f'{rule}\n❯\xa0\n{rule}\n  ⏵⏵ bypass permissions on · /tasks to see subagents\n')
    assert idle.at_rest(screen)
    seat = SimpleNamespace(harness=harness, launched=lambda: SimpleNamespace(version=VERSION))
    reread, screen_hold, _ = idle.second(seat, state, hold, screen=screen)
    assert reread.pending_tools is None and screen_hold.dimension == 'tools-unknown'


def test_streaming_and_full_reader_agree_on_bounded_output_file_equality(tmp_path, monkeypatch):
    record = fixture(tmp_path, [prompt(0), *agent_rows(), duration(6), *agent_notice(7), duration(8)], child_rows())
    streamed, turns = claude.read(record)
    assert streamed.pending_tools == () and not streamed.projection_usage.compatibility
    assert OUTPUT not in repr(turns)
    assert next(row['toolUseResult']['outputFile'] for row in claude_record.inspect_entries(record)
                if 'toolUseResult' in row) == OUTPUT

    def full_reader(*args):
        raise claude_record.CompatibilityRefusal('synthetic full-reader test')

    json_record.release(record)
    json_record.release(child_path(record))
    monkeypatch.setattr(claude_record, '_scan_layout', full_reader)
    full, full_turns = claude.read(record)
    assert full.pending_tools == streamed.pending_tools and full.projection_usage.compatibility
    assert full_turns == turns


def bash_notice(second, **fields):
    rows = agent_notice(second, 'bash', task=SHELL, call=SHELL_CALL, **fields)
    return rows


@pytest.mark.parametrize('difference', [None, 'different-text', 'no-dequeue', 'untimed-notice', 'foreign-queue',
                                         'human-origin'])
def test_an_own_bash_notice_delivered_by_dequeue_settles_only_when_bound(tmp_path, difference):
    notices = bash_notice(7)
    if difference == 'different-text':
        notices[-1]['message']['content'] = notices[-1]['message']['content'].replace('Synthetic', 'Other')
    if difference == 'no-dequeue':
        del notices[1]
    if difference == 'untimed-notice':
        notices[-1]['timestamp'] = '<timestamp>'
    if difference == 'foreign-queue':
        notices[1]['sessionId'] = 'other-session'
    if difference == 'human-origin':
        notices[-1]['origin'] = dict(kind='human')
    record = fixture(tmp_path, [*shell_rows(SHELL), duration(3), *notices, duration(9)])
    state = claude.state(record)
    # The queue carrier alone: the shell's own end is execution evidence, read as before.
    if difference is None:
        assert state.queue_error is None and state.turn_open is False
        assert [item.text for item in claude.inputs(record)] == ['synthetic prompt']
        assert not claude.inputs(record).skipped
    else:
        assert idle.check(state) is not None
        if difference != 'human-origin':
            assert state.queue_error == 'task notification queue unresolved'


def stopped_notice(second, note=None):
    from quarterdeck.harness.claude_background import STOPPED_NOTE
    text = (f'<task-notification>\n<task-id>{SHELL}</task-id>\n<tool-use-id>{SHELL_CALL}</tool-use-id>\n'
            f'<status>stopped</status>\n<summary>Background command "Synthetic" was stopped</summary>\n'
            f'<note>{note or STOPPED_NOTE}</note>\n</task-notification>')
    return [queue(second, 'enqueue'), queue(second + .01, 'dequeue'),
            row('user', second + .02, message=dict(role='user', content=text),
                origin=dict(kind='task-notification'), rowSource='harness', promptSource='system',
                queueSkipAttachments=True, queueTranscriptOnly=True, promptId=f'synthetic-prompt-{second}')]


@pytest.mark.parametrize('note', [None, 'Another note.'])
def test_the_measured_resume_stopped_notice_is_a_named_queue_hold(tmp_path, note):
    from quarterdeck.harness.claude_background import STOPPED_RESUME
    record = fixture(tmp_path, [*shell_rows(SHELL), duration(3), *stopped_notice(5, note), duration(6)])
    state = claude.state(record)
    expected = STOPPED_RESUME if note is None else 'task notification queue unresolved'
    assert state.queue_error == expected
    assert idle.check(state) == idle.Hold('queue-unknown', f'queue unknown: {expected}')


@pytest.mark.parametrize('note', [None, 'Another note.'])
def test_the_stopped_notice_name_reaches_wake_status_and_the_move_proof(tmp_path, monkeypatch, note):
    from quarterdeck import human_wake, record_follow
    from quarterdeck.harness.base import Join
    from quarterdeck.harness.claude_background import STOPPED_RESUME
    from quarterdeck.harness.types import Screen
    record = fixture(tmp_path, [*shell_rows(SHELL), duration(3), *stopped_notice(5, note), duration(6)])
    harness = claude.ClaudeHarness()
    seat = SimpleNamespace(name='person', launched=lambda: object(), harness=harness, join=lambda: Join(record),
                           pane=lambda: SimpleNamespace(dead=False, in_mode=False))
    monkeypatch.setattr(human_wake, 'candidate_hold', lambda *args: None)
    monkeypatch.setattr('quarterdeck.turn_failure.identity', lambda seat: {'seat': seat.name})
    assert record_follow.reader_class(harness) is not None  # the incremental follower qd status reads through
    wake = human_wake.eligible(seat, Screen(text='Synthetic composer', box='', working=False))
    followed = record_follow.state(seat, record)
    hold = idle.check(harness.state(record))
    if note is None:
        assert wake == f'human wake held: record activity unknown (tools, queue); {STOPPED_RESUME}'
        assert followed.queue_error == STOPPED_RESUME
        assert hold == idle.Hold('queue-unknown', f'queue unknown: {STOPPED_RESUME}')
    else:  # any other unresolved notice stays unnamed
        assert wake == 'human wake held: record activity unknown (tools, queue)'
        assert followed.queue_error == 'task notification queue unresolved'
        assert hold.reason == 'queue unknown: task notification queue unresolved'


def test_only_the_adapters_measured_reasons_are_declared_for_naming():
    from quarterdeck.harness.capabilities import named_record_holds
    from quarterdeck.harness.claude_background import STOPPED_RESUME
    assert named_record_holds('claude') == (claude.MIXED_REMOTE_INPUT, STOPPED_RESUME)
    assert named_record_holds('codex') == named_record_holds(None) == ()
    assert 'task notification queue unresolved' not in named_record_holds('claude')
