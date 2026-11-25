"""Synthetic identities and clocks for the signed-in background-task record forms."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from quarterdeck import idle
from quarterdeck.harness import claude, claude_background, claude_record, json_record


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


def absorbed_bash_transcript():
    output = '/synthetic/shell.output'
    text = notice_text('bash', task=SHELL, call=SHELL_CALL, output=output)
    cwd_hint = ('Session cwd remains /synthetic/background; directory changes made by the backgrounded command '
                'do not apply to subsequent commands.')
    acknowledgement = (f'{claude_record.BACKGROUND_HEADER}{SHELL}. Output is being written to: {output}. '
                       + claude_background.BASH_ACK_GUIDANCE + '\n' + cwd_hint)
    start = [prompt(0), tool(1, SHELL_CALL, 'Bash', command='sleep 120', description='Synthetic',
                            run_in_background=True),
             result(2, SHELL_CALL, acknowledgement, backgroundTaskId=SHELL, interrupted=False, isImage=False,
                    noOutputExpected=False, stdout='', stderr='', backgroundCwdHint=cwd_hint)]
    start[0].update(uuid='synthetic-prompt-row', promptSource='typed', turnOrigin='human',
                    promptId='synthetic-prompt', turnPromptId='synthetic-prompt')
    start[1].update(uuid='synthetic-background-call', parentUuid=start[0]['uuid'])
    start[2].update(uuid='synthetic-background-result', promptId='synthetic-prompt', parentUuid=start[1]['uuid'],
                    sourceToolAssistantUUID=start[1]['uuid'], cwd='/synthetic/working')
    start[2]['message']['content'][0]['is_error'] = False

    def assistant(second, identity, parent, block):
        return row('assistant', second, uuid=identity, parentUuid=parent, session_id=SESSION, madeBy='model',
                   requestRef='synthetic-request', message=dict(role='assistant', stop_reason='tool_use', content=[block]))

    start.append(row('attachment', 4, uuid='synthetic-deferred-tools', parentUuid=start[2]['uuid'],
                     attachment=dict(type='deferred_tools_record')))
    thinking = assistant(5.1, 'synthetic-thinking', start[-1]['uuid'], dict(type='thinking', thinking=''))
    speech = assistant(5.2, 'synthetic-text', thinking['uuid'], dict(type='text', text='Synthetic working.'))
    foreground_call = 'synthetic-foreground-call'
    foreground = assistant(5.3, 'synthetic-foreground', speech['uuid'], dict(
        type='tool_use', id=foreground_call, name='Bash', input=dict(command='sleep 5'), caller=dict(type='direct')))
    foreground_result = result(6.9, foreground_call)
    foreground_result.update(uuid='synthetic-foreground-result', parentUuid=foreground['uuid'],
                             sourceToolAssistantUUID=foreground['uuid'], session_id=SESSION, promptId='synthetic-prompt')
    foreground_result['message']['content'][0]['is_error'] = False
    attachment = row('attachment', 5, uuid='synthetic-attachment', parentUuid=foreground_result['uuid'],
                     session_id=SESSION, renderedRole='system', userType='external', entrypoint='cli', cwd='/synthetic',
                     gitBranch='main', dataFlowOn=True, rendered=[dict(content=text)],
                     renderedInHumanTurn=[dict(content=text)],
                     attachment=dict(type='queued_command', prompt=text, source_uuid='synthetic-command',
                                     delivery_id='synthetic-delivery', commandMode='task-notification',
                                     origin=dict(kind='task-notification', producer='session-task'), rowSource='recorded',
                                     runId='synthetic-run', timestamp=(AT + timedelta(seconds=5)).isoformat()))
    sequence = [queue(5, 'enqueue', content=text), thinking, speech, foreground,
                queue(7, 'remove', content=text, reason='absorbed_mid_turn', commandUuid='synthetic-command',
                      deliveryId='synthetic-delivery'), foreground_result, attachment]
    wire_copy = dict(type='api-request-blob', sessionId=SESSION, hash='a' * 64,
                     message=dict(role='system', content=[dict(type='text', text=text)]))
    return start, sequence, [wire_copy, duration(8)]


def early_enqueued_bash_transcript():
    start, sequence, tail = absorbed_bash_transcript()
    typed, background, acknowledgement, _ = start
    enqueue, thinking, _, foreground, remove, foreground_result, attachment = sequence
    context = [typed]
    for kind in ('environment', 'model', 'deferred_tools_delta', 'agent_listing_delta', 'mcp_instructions_delta',
                 'skill_listing', 'total_tokens_reminder', 'session_context', 'date', 'credential_org',
                 'prompt_snapshot'):
        context.append(row('attachment', .1, uuid=f'synthetic-{kind}', parentUuid=context[-1]['uuid'],
                           session_id=SESSION, attachment=dict(type=kind)))
    thinking.update(timestamp=row('assistant', .5)['timestamp'], parentUuid=context[-1]['uuid'])
    background.update(parentUuid=thinking['uuid'], session_id=SESSION)
    acknowledgement['session_id'] = SESSION
    del acknowledgement['toolUseResult']['backgroundCwdHint']
    acknowledgement['message']['content'][0]['content'] = f'{claude_record.BACKGROUND_HEADER}{SHELL}.'
    foreground.update(timestamp=row('assistant', 3)['timestamp'], parentUuid=acknowledgement['uuid'])
    del foreground['message']['content'][0]['caller']
    for assistant in (thinking, background, foreground):
        assistant.pop('requestRef', None)
        assistant.pop('madeBy', None)
        assistant['requestId'] = 'synthetic-first-request'
    snapshot = row('attachment', 6.95, uuid='synthetic-result-snapshot', parentUuid=foreground_result['uuid'],
                   session_id=SESSION, attachment=dict(type='prompt_snapshot', cliPrefix='', contextRendering={},
                   echoWireToolInputs=False, inlineTools=[], keptReminders=[], reminderFold={}, systemPrompt=[],
                   systemTurns=[], toolChangeHeader='', tools=[]))
    attachment['parentUuid'] = snapshot['uuid']
    return context, [enqueue, thinking, background, acknowledgement, foreground, remove,
                     foreground_result, snapshot, attachment], tail


@pytest.mark.parametrize('transcript', [absorbed_bash_transcript, early_enqueued_bash_transcript],
                         ids=['direct-result-parent', 'snapshot-parent'])
def test_an_absorbed_bash_notice_settles_the_existing_turn_incrementally(tmp_path, transcript):
    start, sequence, tail = transcript()
    record = fixture(tmp_path, start)
    expected = ('Bash',) if transcript is absorbed_bash_transcript else ()
    assert claude.state(record).pending_tools == expected
    for entry in sequence[:-1]:
        write(record, [entry], 'a')
        state = claude.state(record)
        assert state.pending_tools is None and state.turn_open is True
        assert state.queue_error == 'task notification queue unresolved'
        assert state.queued == state.consumed == ()
        assert claude.inputs(record).skipped
    with record.open('a') as stream:
        stream.write(json.dumps(sequence[-1]))
    assert claude.state(record).queue_error == 'task notification queue unresolved'
    with record.open('a') as stream:
        stream.write('\n')
    state = claude.state(record)
    assert state.queue_error is None and state.pending_tools == () and state.turn_open is True
    assert not claude.inputs(record).skipped
    write(record, tail, 'a')
    state, turns = claude.read(record)
    cold_state, cold_turns = claude.read(fixture(tmp_path / 'cold', start + sequence + tail))
    assert replace(state, last_write=cold_state.last_write) == cold_state and turns == cold_turns
    assert state.queue_error is None and state.pending_tools == () and state.turn_open is False
    assert state.active_tools == () and state.queued == state.consumed == ()
    assert [turn.input for turn in turns] == ['synthetic prompt']
    inputs = claude.inputs(record)
    assert [item.text for item in inputs] == ['synthetic prompt'] and not inputs.skipped


def test_an_absorbed_bash_notice_can_belong_to_a_native_callback_turn(tmp_path):
    start, sequence, tail = absorbed_bash_transcript()
    callback = agent_notice(-.02, 'bash', task='prior-task', call='prior-call')
    callback[-1].update(uuid=start[0]['uuid'], promptId='synthetic-prompt', turnPromptId='synthetic-prompt')
    record = fixture(tmp_path, [prompt(-2, 'prior prompt'), duration(-1)] + callback + start[1:] + sequence + tail)
    reader = claude._Reader(record)
    state = reader.state()
    assert reader.background.tasks[SHELL_CALL].terminal == 'completed'
    assert state.queue_error is None and state.turn_open is False
    assert state.pending_tools is None  # the earlier notice has separate unresolved ownership
    assert [turn.input for turn in reader.turns] == ['prior prompt', '']
    inputs = claude.inputs(record)
    assert [item.text for item in inputs] == ['prior prompt'] and not inputs.skipped


@pytest.mark.parametrize('fault', ['mismatched-mirror', 'different-sentence', 'trailing-slash', 'wrong-output'])
def test_a_direct_notice_requires_its_acknowledgement_output_and_measured_hint(tmp_path, fault):
    start, sequence, tail = absorbed_bash_transcript()
    acknowledgement = start[2]
    mirror = acknowledgement['toolUseResult']
    block = acknowledgement['message']['content'][0]
    if fault == 'mismatched-mirror':
        mirror['backgroundCwdHint'] = mirror['backgroundCwdHint'].replace('/background', '/different')
    elif fault == 'wrong-output':
        block['content'] = block['content'].replace('/synthetic/shell.output', '/synthetic/other.output')
    else:
        text, hint = block['content'].split('\n')
        if fault == 'different-sentence':
            hint = hint.replace('Session cwd remains', 'Working directory is')
        else:
            hint = hint.replace('/background;', '/background/;')
        block['content'] = text + '\n' + hint
        mirror['backgroundCwdHint'] = hint
    assert_absorbed_notice_hold(tmp_path, start, sequence, tail)


@pytest.mark.parametrize('transcript', [absorbed_bash_transcript, early_enqueued_bash_transcript],
                         ids=['direct-result-parent', 'snapshot-parent'])
@pytest.mark.parametrize('fault', ['no-attachment', 'no-remove', 'different-remove-text', 'different-attachment-text',
                                   'wrong-task', 'wrong-call', 'foreign-session', 'broken-assistant-chain',
                                   'wrong-parent', 'wrong-result', 'different-command', 'different-delivery',
                                   'untimed-attachment', 'wrong-origin', 'extra-field', 'later-version',
                                   'different-result-prompt', 'other-turn', 'duration', 'compaction'])
def test_an_unmatched_absorbed_bash_notice_holds_the_queue(tmp_path, transcript, fault):
    start, sequence, tail = transcript()
    enqueue, attachment = sequence[0], sequence[-1]
    foreground, remove, result = sequence[-5:-2] if transcript is early_enqueued_bash_transcript else sequence[-4:-1]
    if fault == 'no-attachment':
        sequence.remove(attachment)
    elif fault == 'no-remove':
        sequence.remove(remove)
    elif fault == 'different-remove-text':
        remove['content'] += '\n'
    elif fault == 'different-attachment-text':
        attachment['attachment']['prompt'] += '\n'
    elif fault in ('wrong-task', 'wrong-call'):
        key, identity = ('task-id', SHELL) if fault == 'wrong-task' else ('tool-use-id', SHELL_CALL)
        payload = enqueue['content'].replace(f'<{key}>{identity}</{key}>', f'<{key}>unrelated</{key}>')
        enqueue['content'] = remove['content'] = attachment['attachment']['prompt'] = payload
    elif fault == 'foreign-session':
        attachment['sessionId'] = 'synthetic-foreign-session'
    elif fault == 'broken-assistant-chain':
        foreground['parentUuid'] = 'synthetic-unrelated-assistant'
    elif fault == 'wrong-parent':
        attachment['parentUuid'] = 'synthetic-unrelated-result'
    elif fault == 'wrong-result':
        result['message']['content'][0]['tool_use_id'] = 'unrelated-tool'
    elif fault == 'different-result-prompt':
        result['promptId'] = 'synthetic-other-prompt'
    elif fault == 'other-turn':
        start.append(row('user', 4, uuid='synthetic-other-prompt-row', promptId='synthetic-other-prompt',
                         turnPromptId='synthetic-other-prompt', promptSource='typed', turnOrigin='human',
                         origin=dict(kind='human'), message=dict(role='user', content='another prompt')))
    elif fault == 'duration':
        sequence.insert(1, duration(5.05))
    elif fault == 'compaction':
        sequence.insert(1, compact(5.05))
    elif fault == 'different-command':
        attachment['attachment']['source_uuid'] = 'synthetic-other-command'
    elif fault == 'different-delivery':
        attachment['attachment']['delivery_id'] = 'synthetic-other-delivery'
    elif fault == 'untimed-attachment':
        del attachment['timestamp']
    elif fault == 'wrong-origin':
        attachment['attachment']['origin']['producer'] = 'unknown'
    elif fault == 'extra-field':
        attachment['attachment']['unknown'] = True
    elif fault == 'later-version':
        attachment['version'] = '2.1.332'
    assert_absorbed_notice_hold(tmp_path, start, sequence, tail)


@pytest.mark.parametrize('fault', ['root-ancestry', 'request', 'background-source', 'background-tool',
                                   'background-task', 'background-prompt', 'background-text', 'snapshot-parent',
                                   'no-snapshot', 'snapshot-shape', 'outer-timestamp', 'inner-timestamp'])
def test_an_early_enqueue_requires_the_typed_turn_chain_and_its_own_background_call(tmp_path, fault):
    start, sequence, tail = early_enqueued_bash_transcript()
    _, thinking, background, acknowledgement, foreground, _, _, snapshot, attachment = sequence
    if fault == 'root-ancestry':
        thinking['parentUuid'] = 'unrelated-context'
    elif fault == 'request':
        foreground['requestId'] = 'other-request'
    elif fault == 'background-source':
        acknowledgement['sourceToolAssistantUUID'] = 'different-source'
    elif fault == 'background-tool':
        background['message']['content'][0]['id'] = 'different-call'
    elif fault == 'background-task':
        acknowledgement['toolUseResult']['backgroundTaskId'] = 'different-task'
    elif fault == 'background-prompt':
        acknowledgement['promptId'] = 'different-turn'
    elif fault == 'background-text':
        acknowledgement['message']['content'][0]['content'] = f'{claude_record.BACKGROUND_HEADER}{SHELL}-other.'
    elif fault == 'snapshot-parent':
        snapshot['parentUuid'] = 'unrelated-result'
    elif fault == 'no-snapshot':
        sequence.remove(snapshot)
    elif fault == 'snapshot-shape':
        del snapshot['attachment']['systemPrompt']
    elif fault == 'outer-timestamp':
        attachment['timestamp'] = snapshot['timestamp']
    elif fault == 'inner-timestamp':
        attachment['attachment']['timestamp'] = snapshot['timestamp']
    assert_absorbed_notice_hold(tmp_path, start, sequence, tail)


def assert_absorbed_notice_hold(tmp_path, start, sequence, tail):
    record = fixture(tmp_path, start)
    claude.state(record)
    claude.inputs(record)
    for entry in sequence + tail:
        write(record, [entry], 'a')
        claude.state(record)
        claude.inputs(record)
    state = claude.state(record)
    cold = fixture(tmp_path / 'cold', start + sequence + tail)
    cold_state = claude.state(cold)
    assert replace(state, last_write=cold_state.last_write) == cold_state
    assert state.pending_tools is None
    assert state.queue_error == 'task notification queue unresolved'
    assert state.queued == state.consumed == ()
    assert claude.inputs(record).skipped == claude.inputs(cold).skipped
    assert claude.inputs(record).skipped


@pytest.mark.parametrize('transcript', [absorbed_bash_transcript, early_enqueued_bash_transcript],
                         ids=['direct-result-parent', 'snapshot-parent'])
@pytest.mark.parametrize('cause', ['stopped', 'ordinary-queue'])
def test_an_absorbed_bash_notice_keeps_an_independent_queue_hold(tmp_path, transcript, cause):
    start, sequence, tail = transcript()
    if cause == 'stopped':
        extra, expected = stopped_notice(-3), claude_background.STOPPED_RESUME
    else:
        extra, expected = [queue(-3, 'popAll')], 'popAll identity unresolved'
    record = fixture(tmp_path, extra + start + sequence + tail)
    assert claude.state(record).queue_error == expected


def test_an_older_bash_notice_keeps_its_original_queue_reading(tmp_path):
    start, sequence, tail = absorbed_bash_transcript()
    for entry in start + sequence + tail:
        if 'version' in entry:
            entry['version'] = '2.1.317'
    state = claude.state(fixture(tmp_path, start + sequence + tail))
    assert state.queue_error is None and state.pending_tools == ()
    assert state.consumed == (sequence[0]['content'],)


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
