"""Synthetic equivalents of the same-session restart and manual-compaction observations."""

from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from quarterdeck import door, human_wake, idle, inbox_watch, input_receipt, post, recovery, relaunch_intent, resting_failover
from quarterdeck.harness import claude
from quarterdeck.seat_command import qd_command
from quarterdeck.service import Service

AT = datetime(2034, 1, 2, tzinfo=timezone.utc)
OLD, NEW = '2.1.317', '2.1.331'
SESSION = 'synthetic-session-quill'
CALL, TASK, OUT = 'toolu_WatchQuill', 'synthwatch', '/synthetic/watch.output'
DESC = 'Wait for the inbox'
END = 'synthetic-duration'
NOTE = ('No completion record was found for it in the previous session. It may have been stopped '
        '(via the UI, Monitor timeout, or agent teardown — these leave no transcript marker), '
        'or it may have been running when the previous Claude Code process exited. '
        'Check the output file for partial results before assuming it completed.')
STOP_SUMMARY = "Background shell command didn't finish before the previous session ended"


def stamp(seconds):
    return (AT + timedelta(seconds=seconds)).isoformat()


def row(kind, seconds, version, **fields):
    return dict(type=kind, timestamp=stamp(seconds), version=version, isSidechain=False,
                sessionId=SESSION, **fields)


def queue(operation, seconds, **fields):
    return dict(type='queue-operation', operation=operation, timestamp=stamp(seconds),
                sessionId=SESSION, **fields)


def tool(seconds, version, call, name, **arguments):
    return row('assistant', seconds, version, message=dict(role='assistant', stop_reason='tool_use', content=[
        dict(type='tool_use', id=call, name=name, input=arguments)]))


def origin(version, command='qd inbox --wait'):
    handle = f'Command running in background with ID: {TASK}. Output is being written to: {OUT}. Synthetic.'
    return [
        row('user', 0, version, origin=dict(kind='human'),
            message=dict(role='user', content='Arm the inbox watch.')),
        tool(1, version, CALL, 'Bash', command=command, description=DESC, run_in_background=True),
        row('user', 2, version, message=dict(role='user', content=[
            dict(type='tool_result', tool_use_id=CALL, content=handle, is_error=False)]),
            toolUseResult=dict(backgroundTaskId=TASK)),
        row('system', 3, version, subtype='turn_duration', uuid=END),
    ]


def typed(seconds, prompt, text):
    return row('user', seconds, NEW, promptId=prompt, turnPromptId=prompt, turnOrigin='human',
               promptSource='typed', origin=dict(kind='human'), message=dict(role='user', content=text))


def started_watch():
    first = origin(NEW)
    first[0] = typed(0, 'prompt-first', 'Arm the inbox watch.')
    first[2]['promptId'] = 'prompt-first'
    return first


def notice(*, killed=False, task=TASK, call=CALL):
    header = f'<task-notification>\n<task-id>{task}</task-id>\n<tool-use-id>{call}</tool-use-id>\n'
    if killed:
        return (f'{header}<output-file>{OUT}</output-file>\n<status>killed</status>\n'
                f'<summary>Background command "{DESC}" was stopped</summary>\n</task-notification>')
    return (f'{header}<status>stopped</status>\n<summary>{STOP_SUMMARY}</summary>\n'
            f'<note>{NOTE}</note>\n</task-notification>')


def resume_notice(**fields):
    return row('user', 6.02, NEW, parentUuid=END, uuid='synthetic-resume-row', promptId='synthetic-prompt',
               origin=dict(kind='task-notification'), promptSource='system', rowSource='harness',
               queueSkipAttachments=True, queueTranscriptOnly=True,
               message=dict(role='user', content=notice(**fields)))


def resume():
    return [
        queue('enqueue', 4, content=notice(killed=True)), queue('dequeue', 4.01),
        dict(type='tools-used', sessionId=SESSION, tools=['Bash']),
        queue('enqueue', 6, content=notice()), queue('dequeue', 6.01),
        dict(type='derived-from', v=1, sessionId=SESSION,
             sessions=[dict(type='claude_code_session', id=SESSION, via='resume')]),
        resume_notice(),
    ]


def same_version_resume():
    move = resume()
    native = move[-1]
    del native['rowSource']
    native.update(version=OLD, entrypoint='cli', userType='external', permissionMode='bypassPermissions',
                  cwd='/synthetic/workspace', gitBranch='HEAD')
    return move[:-2] + [native]


def record_path(tmp_path):
    return tmp_path / 'runtime/seats/quill/home/projects/workspace' / f'{SESSION}.jsonl'


def record(tmp_path, rows):
    path = record_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(entry) + '\n' for entry in rows))
    return path


@pytest.mark.parametrize('version', [OLD, NEW])
def test_a_standalone_kill_pair_is_scoped_to_the_old_watch(tmp_path, version):
    path = record(tmp_path, origin(version) + resume()[:2])
    reader = claude._Reader(path)
    assert tuple(task.phase for task in reader.background.task_states()) == ('ended' if version == OLD else 'unknown',)
    if version == OLD:
        assert [entry.kind for entry in claude.inputs(path)] == ['accepted']


@pytest.mark.parametrize('move', [resume, same_version_resume], ids=['upgrade', 'same-version'])
def test_the_resume_notice_settles_the_old_watch_without_a_turn_or_receipt(tmp_path, move):
    command = qd_command(tmp_path, 'inbox', '--wait')
    carriers = move()
    path = record(tmp_path, origin(OLD, command))
    for index, entry in enumerate(carriers[:-1]):
        with path.open('a') as stream:
            stream.write(json.dumps(entry) + '\n')
        partial = claude.state(path)
        assert all(task.phase != 'ended-by-relaunch' for task in claude._Reader(path).background.task_states())
        assert partial.turns == 1 and partial.queued == partial.consumed == ()
        assert [entry.kind for entry in claude.inputs(path)] == ['accepted']
        if index not in (1, 2):  # the native exit pair has ended before the resumed pair starts
            assert partial.queue_error == 'task notification queue unresolved'
            assert idle.check(partial) is not None
    with path.open('a') as stream:
        stream.write(json.dumps(carriers[-1]) + '\n')
    harness = claude.ClaudeHarness()
    reader = claude._Reader(path)
    state = claude.state(path)
    assert (state.turns, state.pending_tools, state.consumed) == (reader.state().turns, (), ())
    assert [task.phase for task in reader.background.task_states()] == ['ended-by-relaunch']
    assert state.turn_open is False and state.turns == 1 and state.last_input_at == AT
    assert state.queued == state.consumed == state.pending_tools == state.active_tools == ()
    assert [(entry.kind, entry.text) for entry in harness.inputs(path)] == [('accepted', 'Arm the inbox watch.')]
    assert not harness.inputs(path).skipped
    assert harness.person_inputs(path, ()) == [AT]
    assert idle.check(state) is None
    assert idle.check(harness.state_for_relaunch(path, AT + timedelta(seconds=8))) is None


@pytest.mark.parametrize('move', [resume, same_version_resume], ids=['upgrade', 'same-version'])
def test_the_resumed_record_admits_the_ordinary_wake_and_restart(tmp_path, monkeypatch, move):
    carriers = move()
    version = carriers[-1]['version']
    path = record(tmp_path, origin(OLD) + carriers)
    harness = claude.ClaudeHarness()
    screen = harness.screen('\n'.join(['─' * 80, '❯\xa0', '─' * 80,
                                       '  ⏵⏵ bypass permissions on (shift+tab to cycle)']))
    pane = SimpleNamespace(id='%synthetic', pid=1, dead=False, in_mode=False)
    seat = SimpleNamespace(
        name='quill', harness=harness, session=SimpleNamespace(human=True), ship=SimpleNamespace(runtime=tmp_path),
        trusted=tmp_path / 'trusted', pane=lambda: pane, launched=lambda: SimpleNamespace(
            pane=pane.id, pid=pane.pid, at=stamp(5), harness='claude', version=version),
        record=lambda: path, join=lambda: SimpleNamespace(record=path), restart=lambda: 'Synthetic restart.')
    monkeypatch.setattr(human_wake, 'candidate_hold', lambda *args: None)
    monkeypatch.setattr(door, 'Door', lambda seat: SimpleNamespace(screen=lambda: screen))
    monkeypatch.setattr(resting_failover, 'require_clear', lambda seat: None)
    monkeypatch.setattr(recovery, 'held', lambda seat: None)
    monkeypatch.setattr(relaunch_intent, 'section', lambda *args, **kwargs: nullcontext())

    assert human_wake.eligible(seat, screen) is None
    assert Service.relaunch(None, seat, True) == 'Synthetic restart.'
    assert seat.defer_opening

    record(tmp_path, origin(OLD) + carriers[:-1])
    assert 'queue' in human_wake.eligible(seat, screen)
    assert idle.check(harness.state_for_relaunch(path, AT + timedelta(seconds=5)), explicit=True).dimension == 'tools-unknown'
    if version == OLD:
        # The explicit restart can use the independently measured old-version footer.
        assert Service.relaunch(None, seat, True) == 'Synthetic restart.\nat rest by its record and its screen'
        monkeypatch.setattr(door, 'Door', lambda seat: SimpleNamespace(screen=lambda: harness.screen('')))
    assert Service.relaunch(None, seat, True) is None


@pytest.mark.parametrize('fault', ['no-tools-used', 'extra-user-field', 'not-transcript-only',
                                  'foreign-session', 'foreign-parent', 'missing-stop-dequeue'])
def test_unmeasured_same_version_restarts_keep_the_old_watch_hold(tmp_path, fault):
    move = same_version_resume()
    user = move[-1]
    if fault == 'no-tools-used':
        move.pop(2)
    elif fault == 'extra-user-field':
        user['rowSource'] = 'harness'
    elif fault == 'not-transcript-only':
        user['queueTranscriptOnly'] = False
    elif fault == 'foreign-session':
        user['sessionId'] = 'synthetic-other-session'
    elif fault == 'foreign-parent':
        user['parentUuid'] = 'synthetic-other-turn'
    else:
        move.pop(4)
    path = record(tmp_path, origin(OLD) + move)
    reader = claude._Reader(path)
    assert all(task.phase != 'ended-by-relaunch' for task in reader.background.task_states())
    assert idle.check(reader.state()) is not None


def test_synthetic_rearmed_watch_can_rest_after_the_measured_same_version_resume(tmp_path, monkeypatch):
    replacement = origin(OLD)
    for entry in replacement:
        elapsed = (datetime.fromisoformat(entry['timestamp']) - AT).total_seconds()
        entry['timestamp'] = stamp(10 + elapsed)
    for block in (replacement[1]['message']['content'][0], replacement[2]['message']['content'][0]):
        key = 'id' if block['type'] == 'tool_use' else 'tool_use_id'
        block[key] = 'toolu_RearmedQuill'
    result = replacement[2]['message']['content'][0]
    result['content'] = result['content'].replace(TASK, 'replacement')
    replacement[2]['toolUseResult']['backgroundTaskId'] = 'replacement'
    replacement[-1]['uuid'] = 'synthetic-rearm-duration'
    path = record(tmp_path, origin(OLD) + same_version_resume() + replacement)
    reader = claude._Reader(path)
    assert [task.phase for task in reader.background.task_states()] == ['ended-by-relaunch', 'pending']
    assert reader.state().pending_tools == ('Bash',)
    assert idle.check(reader.state()).dimension == 'tools-pending'
    seat = SimpleNamespace()
    monkeypatch.setattr(inbox_watch, 'current', lambda candidate, at: candidate is seat and at.isoformat() == stamp(11))
    assert idle.check(reader.state(), seat) is None


@pytest.mark.parametrize('fault', [
    'other-command', 'foreign-call', 'foreign-ack', 'missing-handle', 'other-task', 'other-tool',
    'foreign-kill', 'foreign-stop', 'foreign-dequeue', 'foreign-row', 'wrong-version', 'missing-session',
    'changed-note', 'changed-kind', 'other-parent', 'other-relaunch', 'other-description',
    'second-enqueue', 'intervening-input',
])
def test_an_unjoined_or_ambiguous_notice_cannot_settle_the_watch(tmp_path, fault):
    first, move = origin(OLD), resume()
    if fault == 'other-command':
        first[1]['message']['content'][0]['input']['command'] = 'qd inbox --wait && synthetic'
    elif fault in ('foreign-call', 'foreign-ack'):
        first[1 if fault == 'foreign-call' else 2]['sessionId'] = 'foreign'
    elif fault == 'missing-handle':
        first[2]['message']['content'][0]['content'] = ''
    elif fault == 'other-task':
        move[3]['content'] = notice(task='other-task')
        move[6] = resume_notice(task='other-task')
    elif fault == 'other-tool':
        move[3]['content'] = notice(call='toolu_Other')
        move[6] = resume_notice(call='toolu_Other')
    elif fault in ('foreign-kill', 'foreign-stop', 'foreign-dequeue', 'foreign-row'):
        move[{'foreign-kill': 0, 'foreign-stop': 3, 'foreign-dequeue': 4, 'foreign-row': 6}[fault]]['sessionId'] = 'foreign'
    elif fault == 'wrong-version':
        move[6]['version'] = '2.1.332'
    elif fault == 'missing-session':
        del move[6]['sessionId']
    elif fault == 'changed-note':
        move[6]['message']['content'] = notice().replace('partial results', 'results')
    elif fault == 'changed-kind':
        move[6]['queueTranscriptOnly'] = False
    elif fault == 'other-parent':
        move[6]['parentUuid'] = 'other-parent'
    elif fault == 'other-relaunch':
        move[5]['sessions'][0]['via'] = 'branch'
    elif fault == 'other-description':
        move[0]['content'] = notice(killed=True).replace(DESC, 'Other command')
    elif fault == 'second-enqueue':
        move.insert(4, queue('enqueue', 6.005, content=notice()))
    else:
        move.insert(4, row('user', 6.005, NEW, message=dict(role='user', content='Synthetic new input.')))
    path = record(tmp_path, first + move)
    reader = claude._Reader(path)
    assert all(task.phase != 'ended-by-relaunch' for task in reader.background.task_states())
    assert idle.check(reader.state()) is not None


def test_a_real_queued_input_remains_queued_and_is_never_claimed_as_a_watch_dequeue(tmp_path):
    text = 'A person typed this.'
    path = record(tmp_path, origin(OLD) + [queue('enqueue', 3.5, content=text)] + resume())
    state = claude.state(path)
    assert text in state.queued and text not in state.consumed
    assert idle.check(state) is not None


def test_an_unrelated_agent_stays_pending_when_the_old_watch_ends(tmp_path):
    initial = origin(OLD)
    initial.insert(-1, tool(2.5, OLD, 'toolu_SyntheticAgent', 'Agent', prompt='Synthetic child.'))
    path = record(tmp_path, initial + resume())
    reader = claude._Reader(path)
    assert [task.phase for task in reader.background.task_states()] == ['ended-by-relaunch']
    assert 'Agent' in {active.name for active in reader.state().active_tools}
    assert idle.check(reader.state()) is not None


def compact(command):
    return [
        row('system', 5.2, NEW, subtype='compact_boundary', compactMetadata=dict(trigger='manual')),
        row('attachment', 5.1, NEW, attachment=dict(type='task_status', taskId=TASK, taskType='local_bash',
            description=DESC, status='running', deltaSummary=None, outputFilePath=OUT,
            shell=dict(kind='bash', command=command, toolUseId=CALL))),
    ]


def test_a_compaction_restoration_retains_the_acknowledged_watch_and_needs_live_registration(tmp_path, monkeypatch):
    command = qd_command(tmp_path, 'inbox', '--wait')
    path = record(tmp_path, origin(NEW, command) + compact(command))
    state = claude.state(path)
    assert state.turn_open is False and state.turns == 1
    assert state.pending_tools == ('Bash',) and state.active_tools[0].inbox_watch_started.isoformat() == stamp(1)
    assert idle.check(state).dimension == 'tools-pending'
    seat = SimpleNamespace()
    monkeypatch.setattr(inbox_watch, 'current', lambda actual, started: actual is seat and started.isoformat() == stamp(1))
    assert idle.check(state, seat) is None


@pytest.mark.parametrize('fault', ['no-compact', 'auto', 'missing-task', 'other-task', 'other-tool',
                                   'other-output', 'other-command', 'outside-launch', 'other-session', 'later-version'])
def test_a_running_attachment_needs_its_own_compaction_and_watch_bindings(tmp_path, fault):
    command = 'qd inbox --wait'
    boundary, status = compact(command)
    attachment = status['attachment']
    if fault == 'no-compact':
        boundary['subtype'] = 'turn_duration'
    elif fault == 'auto':
        boundary['compactMetadata']['trigger'] = 'auto'
    elif fault == 'missing-task':
        del attachment['taskId']
    elif fault == 'other-task':
        attachment['taskId'] = 'other-task'
    elif fault == 'other-tool':
        attachment['shell']['toolUseId'] = 'toolu_Other'
    elif fault == 'other-output':
        attachment['outputFilePath'] = '/synthetic/foreign.output'
    elif fault == 'other-command':
        attachment['shell']['command'] = 'sleep 60'
    elif fault == 'outside-launch':
        status['timestamp'] = stamp(0.5)
    elif fault == 'other-session':
        status['sessionId'] = 'synthetic-foreign'
    else:
        status['version'] = '2.1.332'
    path = record(tmp_path, origin(NEW) + [boundary, status])
    assert claude.state(path).pending_tools is None


def completed_notice():
    return (f'<task-notification>\n<task-id>{TASK}</task-id>\n<tool-use-id>{CALL}</tool-use-id>\n'
            f'<output-file>{OUT}</output-file>\n<status>completed</status>\n'
            f'<summary>Background command "{DESC}" completed (exit code 0)</summary>\n'
            '</task-notification>')


def answer(second, identity):
    return row('assistant', second, NEW, uuid=identity, message=dict(role='assistant', stop_reason='end_turn',
               content=[dict(type='text', text='Synthetic response.')]))


def duration(seconds, parent, identity):
    return row('system', seconds, NEW, subtype='turn_duration', uuid=identity, parentUuid=parent, durationMs=1200)


def immediate():
    text = completed_notice()
    return [
        queue('enqueue', 3, content=text),
        dict(type='data-flow-records', sessionId=SESSION, v=1, records=[]),
        row('attachment', 2.5, NEW, attachment=dict(type='total_tokens_reminder')),
        queue('dequeue', 4),
        answer(3.9, 'synthetic-first-answer'),
        duration(3.95, 'synthetic-first-answer', END),
        row('user', 4.1, NEW, uuid='synthetic-callback', parentUuid=END, promptSource='system', rowSource='recorded',
            turnOrigin='task_notification', promptId='prompt-callback', turnPromptId='prompt-callback',
            queueSkipAttachments=True, origin=dict(kind='task-notification', producer='session-task', runId='run-watch'),
            message=dict(role='user', content=text)),
        answer(5, 'synthetic-callback-answer'),
        duration(5.1, 'synthetic-callback-answer', 'synthetic-callback-end'),
    ]


def test_an_immediate_watch_completion_starts_its_real_callback_turn(tmp_path):
    first = started_watch()[:-1]  # the initial duration is emitted after dequeue in file order
    rows = first + immediate()
    path = record(tmp_path, rows[:-2])
    pending, ended = claude.read(path)
    assert pending.turn_open is True and pending.pending_tools == () and pending.queue_error is None
    assert len(ended) == 1
    state, turns = claude.read(record(tmp_path, rows))
    assert state.turn_open is False and idle.check(state) is None
    assert [turn.input for turn in turns] == ['Arm the inbox watch.', '']
    assert [entry.text for entry in claude.inputs(path)] == ['Arm the inbox watch.']
    assert not claude.inputs(path).skipped
    assert input_receipt.confirmed(claude.ClaudeHarness(), path, completed_notice(), AT) is None


@pytest.mark.parametrize('fault', ['unrelated-tool', 'later-user', 'foreign-session', 'dequeue-before-enqueue',
                                   'wrong-parent', 'unbound-notice', 'intervening-queue', 'no-timed-end'])
def test_unmeasured_immediate_dispositions_keep_the_queue_unknown(tmp_path, fault):
    first, rows = started_watch()[:-1], immediate()
    if fault == 'unrelated-tool':
        first[1]['message']['content'][0]['input']['command'] = 'sleep 5'
    elif fault == 'later-user':
        rows.insert(3, typed(3.2, 'another-prompt', 'Unrelated request.'))
    elif fault == 'foreign-session':
        rows[3]['sessionId'] = 'foreign'
    elif fault == 'dequeue-before-enqueue':
        rows[3]['timestamp'] = stamp(2.9)
    elif fault == 'wrong-parent':
        rows[6]['parentUuid'] = 'another-end'
    elif fault == 'unbound-notice':
        rows[6]['message']['content'] = completed_notice().replace(TASK, 'foreign-task')
    elif fault == 'intervening-queue':
        rows.insert(3, queue('enqueue', 3.2, content='Unrelated pending input.'))
    else:
        del rows[5]
    path = record(tmp_path, first + rows)
    assert claude.state(path).queue_error == 'task notification queue unresolved'


def absorbed():
    text = completed_notice()
    foreground = tool(5, NEW, 'toolu_Foreground', 'Bash', command='sleep 8')
    foreground['uuid'] = 'foreground-call'
    followup = tool(14, NEW, 'toolu_ForegroundAgain', 'Bash', command='sleep 8')
    followup['uuid'] = 'followup-call'
    return [
        typed(4, 'prompt-busy', 'Run foreground work.'),
        foreground,
        queue('enqueue', 6, content=text),
        queue('remove', 12, content=text, reason='absorbed_mid_turn',
              commandUuid='synthetic-command', deliveryId='synthetic-delivery'),
        row('user', 11.9, NEW, promptId='prompt-busy', uuid='foreground-result', parentUuid='foreground-call',
            sourceToolAssistantUUID='foreground-call', message=dict(role='user', content=[
                dict(type='tool_result', tool_use_id='toolu_Foreground', content='Synthetic result.', is_error=False)])),
        row('attachment', 6, NEW, parentUuid='foreground-result', uuid='synthetic-attachment', dataFlowOn=True, attachment=dict(
            type='queued_command', prompt=text, source_uuid='synthetic-command', delivery_id='synthetic-delivery',
            commandMode='task-notification', origin=dict(kind='task-notification', producer='session-task'),
            rowSource='recorded', runId='run-attachment', timestamp=stamp(6))),
        dict(type='api-request-blob', sessionId=SESSION),
        row('attachment', 12, NEW, attachment=dict(type='total_tokens_reminder')),
        followup,
        row('user', 22, NEW, promptId='prompt-busy', message=dict(role='user', content=[
            dict(type='tool_result', tool_use_id='toolu_ForegroundAgain', content='Synthetic result.', is_error=False)])),
        answer(23, 'synthetic-busy-answer'),
        duration(24, 'synthetic-busy-answer', 'synthetic-busy-end'),
    ]


@pytest.mark.parametrize('enqueue_written_first', [False, True])
def test_a_busy_watch_outlives_the_remove_until_the_same_turn_has_a_timed_end(tmp_path, enqueue_written_first):
    busy = absorbed()
    if enqueue_written_first:
        busy[1], busy[2] = busy[2], busy[1]
        busy[8:8] = [dict(type=kind, sessionId=SESSION) for kind in ('last-prompt', 'custom-title', 'agent-name',
                                                                  'mode', 'permission-mode', 'session-owner', 'atis-latch')]
    rows = started_watch() + busy
    path = record(tmp_path, rows[:-1])
    harness = claude.ClaudeHarness()
    unfinished = harness.state(path)
    assert unfinished.turn_open is True and idle.check(unfinished).dimension == 'turn-open'
    assert harness.inputs(path).skipped
    with path.open('a') as stream:
        stream.write(json.dumps(rows[-1]) + '\n')  # a duration alone must refresh cached input diagnostics
    reader = claude._Reader(path)
    state = harness.state(path)
    assert [(task.phase, task.terminal) for task in reader.background.task_states()] == [('ended', 'completed')]
    assert state.turn_open is False and state.turns == 2
    assert state.queued == state.consumed == state.pending_tools == state.active_tools == ()
    assert [entry.text for entry in harness.inputs(path)] == ['Arm the inbox watch.', 'Run foreground work.']
    assert not harness.inputs(path).skipped
    assert harness.person_inputs(path, ()) == [AT, AT + timedelta(seconds=4)]
    assert input_receipt.confirmed(harness, path, completed_notice(), AT) is None
    assert idle.check(state) is None


@pytest.mark.parametrize('fault', [
    'other-task', 'foreign-queue', 'wrong-remove-text', 'textless-dequeue', 'other-reason', 'stale-delivery',
    'other-result-call', 'other-prompt', 'other-source', 'other-attachment-time', 'other-attachment-text',
    'other-origin', 'redacted-attachment-text', 'no-data-flow', 'top-level-row-source', 'later-foreground',
    'unrelated-user', 'compaction', 'untimed-end', 'unrelated-end',
])
def test_an_unqualified_busy_completion_never_releases_the_hold(tmp_path, fault):
    rows = absorbed()
    removal, result, attachment = rows[3:6]
    if fault == 'other-task':
        rows[2]['content'] = rows[2]['content'].replace(TASK, 'foreign-task')
    elif fault == 'foreign-queue':
        removal['sessionId'] = 'foreign'
    elif fault == 'wrong-remove-text':
        removal['content'] += '\n'
    elif fault == 'textless-dequeue':
        rows[3] = queue('dequeue', 12)
    elif fault == 'other-reason':
        removal['reason'] = 'delivered_to_agent'
    elif fault == 'stale-delivery':
        attachment['attachment']['delivery_id'] = 'different-delivery'
    elif fault == 'other-result-call':
        result['message']['content'][0]['tool_use_id'] = 'toolu_Other'
    elif fault == 'other-prompt':
        result['promptId'] = 'prompt-other'
    elif fault == 'other-source':
        attachment['attachment']['source_uuid'] = 'different-source'
    elif fault == 'other-attachment-time':
        attachment['timestamp'] = attachment['attachment']['timestamp'] = stamp(6.1)
    elif fault == 'other-attachment-text':
        attachment['attachment']['prompt'] += '\n'
    elif fault == 'other-origin':
        attachment['attachment']['origin']['producer'] = 'unknown'
    elif fault == 'redacted-attachment-text':
        attachment['attachment']['prompt'] = '<str 389>'
    elif fault == 'no-data-flow':
        del attachment['dataFlowOn']
    elif fault == 'top-level-row-source':
        attachment['rowSource'] = 'recorded'
    elif fault == 'later-foreground':
        rows[1]['timestamp'] = stamp(6.1)
        rows[1], rows[2] = rows[2], rows[1]
    elif fault == 'unrelated-user':
        rows.insert(6, typed(13, 'unrelated-prompt', 'A new prompt.'))
    elif fault == 'compaction':
        rows.insert(6, row('system', 13, NEW, subtype='compact_boundary', compactMetadata=dict(trigger='manual')))
    elif fault == 'untimed-end':
        rows[-1]['timestamp'] = 'unknown'
    else:
        rows[-1]['parentUuid'] = 'unrelated-assistant'

    path = record(tmp_path, started_watch() + rows)
    reader = claude._Reader(path)
    assert all(task.terminal != 'completed' for task in reader.background.task_states())
    assert reader.state().pending_tools is None and reader.state().queue_error is not None
    assert input_receipt.confirmed(claude.ClaudeHarness(), path, completed_notice(), AT) is None


def test_the_measured_older_busy_attachment_keeps_its_existing_projection(tmp_path):
    rows = started_watch() + absorbed()
    for entry in rows:
        if entry.get('version') == NEW:
            entry['version'] = OLD
    for entry in rows:
        if entry.get('attachment', {}).get('type') == 'queued_command':
            del entry['attachment']['rowSource']
            del entry['dataFlowOn']
    path = record(tmp_path, rows)
    reader = claude._Reader(path)
    state = reader.state()
    assert [(task.phase, task.terminal) for task in reader.background.task_states()] == [('ended', 'completed')]
    assert state.turn_open is False and state.turns == 2 and state.queued == () and state.queue_error is None
    assert state.consumed == (completed_notice(),)


REMOTE_URL = 'https://example.invalid/remote/synthetic'
REMOTE_EVENT = 'event_00000000-0000-4000-8000-000000000031'


def bridge_pointer():
    return dict(type='bridge-session', sessionId=SESSION, bridgeSessionId='synthetic-bridge', lastSequenceNum=0,
                ownerAccountUuid='synthetic-account', ownerOrganizationUuid='synthetic-organization')


def bridge_status(seconds, parent, identity, version=NEW):
    return row('system', seconds, version, subtype='bridge_status', uuid=identity, parentUuid=parent,
               url=REMOTE_URL, content=f'/remote-control is active · Continue here, on your phone, or at {REMOTE_URL}',
               isMeta=False, userType='external', entrypoint='cli', cwd='/synthetic/workspace', gitBranch='HEAD',
               dataFlowOn=True)


def flow(kind, **fields):
    return dict(type=kind, v=1, sessionId=SESSION, **fields)


def remote_origin(version):
    status = bridge_status(-1, None, 'synthetic-original-bridge-status', version)
    if version == OLD:
        del status['dataFlowOn']
    return [bridge_pointer(), status, *origin(version)]


def remote_resume(version):
    last_prompt = dict(type='last-prompt', sessionId=SESSION, lastPrompt='Arm the inbox watch.', leafUuid=END)
    cost = dict(type='cost-state', sessionId=SESSION, totalCostUSD=0.1, totalAPIDuration=10,
                totalAPIDurationWithoutRetries=10, totalToolDuration=10, totalLinesAdded=0,
                totalLinesRemoved=0, totalDuration=100, startTime=100, modelUsage={'synthetic': {}},
                hasUnknownModelCost=False)
    killed, killed_dequeue, tools, stopped, stopped_dequeue, derived, resumed = resume()
    resumed.update(entrypoint='cli', userType='external', permissionMode='bypassPermissions',
                   cwd='/synthetic/workspace', gitBranch='HEAD', dataFlowOn=True)
    pointer, status = bridge_pointer(), bridge_status(7, resumed['uuid'], 'synthetic-resumed-bridge-status')
    suffix = [stopped, stopped_dequeue]
    if version == OLD:
        suffix += [derived, resumed, flow('data-flow-incomplete', codes=[9]),
                   flow('data-flow-records', records=[]), pointer, status,
                   flow('data-flow-records', records=[dict(tool_name='RemoteControlLinked', event_id=REMOTE_EVENT)])]
    else:
        suffix = [derived, flow('data-flow-incomplete', codes=[9, 10]), *suffix, resumed, pointer, status]
    return [last_prompt, cost, killed, killed_dequeue, tools, *suffix]


def remote_restart_checkpoint():
    suffix = remote_resume(NEW)
    incomplete = next(entry for entry in suffix if entry['type'] == 'data-flow-incomplete')
    incomplete['codes'] = [9]
    prompt, cost = suffix[:2]
    return suffix + [dict(prompt, leafUuid=suffix[-1]['uuid']), dict(cost)]


@pytest.mark.parametrize(('version', 'resume_rows'), [
    (OLD, remote_resume(OLD)), (NEW, remote_resume(NEW)), (NEW, remote_restart_checkpoint()[:-2]),
], ids=['upgrade', 'same-version', 'same-version-code-9'])
def test_remote_resume_settles_only_at_the_measured_end(tmp_path, version, resume_rows):
    prefix, suffix = remote_origin(version), resume_rows
    path = record(tmp_path, prefix)
    harness = claude.ClaudeHarness()
    for entry in suffix:
        with path.open('a') as stream:
            stream.write(json.dumps(entry))
        if entry is suffix[-1]:
            assert idle.check(claude.state(path)) is not None
            assert all(task.phase != 'ended-by-relaunch' for task in claude._Reader(path).background.task_states())
            assert claude.inputs(path).skipped
        with path.open('a') as stream:
            stream.write('\n')
        state = claude.state(path)
        cold = claude._Reader(path)
        complete = entry is suffix[-1]
        ended = [task for task in cold.background.task_states() if task.phase == 'ended-by-relaunch']
        assert bool(ended) == complete
        assert replace(state, projection_usage=None) == cold.state()
        assert state.turns == 1 and state.last_input_at == AT
        assert [item.text for item in claude.inputs(path)] == ['Arm the inbox watch.']
        assert (idle.check(state) is None) == complete
    assert claude.person_inputs(path, ()) == [AT]
    assert not claude.inputs(path).skipped
    assert not state.turn_open and state.queued == state.consumed == state.pending_tools == state.active_tools == ()


@pytest.mark.parametrize('checkpoint', [True, False])
def test_the_older_same_version_resume_still_works_after_remote_startup(tmp_path, checkpoint):
    suffix = same_version_resume()
    if checkpoint:
        suffix = remote_resume(OLD)[:2] + suffix
    path = record(tmp_path, remote_origin(OLD) + suffix)
    reader = claude._Reader(path)
    assert [task.phase for task in reader.background.task_states()] == ['ended-by-relaunch']
    assert idle.check(reader.state()) is None
    assert [item.text for item in claude.inputs(path)] == ['Arm the inbox watch.']
    assert not claude.inputs(path).skipped
    assert claude.person_inputs(path, ()) == [AT]


@pytest.mark.parametrize('version', [OLD, NEW], ids=['upgrade', 'same-version'])
@pytest.mark.parametrize('fault', ['checkpoint', 'cost-order', 'wrong-task', 'lineage', 'codes', 'code-order',
                                   'code-10', 'reverse-codes', 'duplicate-codes',
                                   'notice-parent', 'notice-envelope', 'bridge-id', 'bridge-cursor',
                                   'bridge-parent', 'bridge-session', 'bridge-version'])
def test_a_remote_resume_requires_the_measured_order_and_identity(tmp_path, version, fault):
    suffix = remote_resume(version)
    lineage = next(entry for entry in suffix if entry['type'] == 'derived-from')
    incomplete = next(entry for entry in suffix if entry['type'] == 'data-flow-incomplete')
    stopped = next(entry for entry in suffix if entry['type'] == 'user')
    pointer = next(entry for entry in suffix if entry['type'] == 'bridge-session')
    status = next(entry for entry in suffix if entry.get('subtype') == 'bridge_status')
    if fault == 'checkpoint':
        suffix[0]['leafUuid'] = 'other-leaf'
    elif fault == 'cost-order':
        suffix[0], suffix[1] = suffix[1], suffix[0]
    elif fault == 'wrong-task':
        suffix[2]['content'] = notice(killed=True, task='other-task')
    elif fault == 'lineage':
        lineage['sessions'][0]['via'] = 'branch'
    elif fault == 'codes':
        incomplete['codes'] = [9, 11]
    elif fault == 'code-10':
        incomplete['codes'] = [10]
    elif fault == 'reverse-codes':
        incomplete['codes'] = [10, 9]
    elif fault == 'duplicate-codes':
        incomplete['codes'] = [9, 9]
    elif fault == 'code-order':
        suffix.remove(incomplete)
        suffix.insert(5, incomplete)
    elif fault == 'notice-parent':
        stopped['parentUuid'] = 'other-leaf'
    elif fault == 'notice-envelope':
        stopped['dataFlowOn'] = False
    elif fault == 'bridge-id':
        pointer['bridgeSessionId'] = 'other-bridge'
    elif fault == 'bridge-cursor':
        pointer['lastSequenceNum'] = 1
    elif fault == 'bridge-parent':
        status['parentUuid'] = END
    elif fault == 'bridge-session':
        status['sessionId'] = 'other-session'
    else:
        status['version'] = '2.1.332'
    path = record(tmp_path, remote_origin(version) + suffix)
    reader = claude._Reader(path)
    assert all(task.phase != 'ended-by-relaunch' for task in reader.background.task_states())
    assert idle.check(reader.state()) is not None
    assert claude.inputs(path).skipped


@pytest.mark.parametrize(('fault', 'classified'), [
    ('none', [True, True]), ('missing-prompt', [False]), ('wrong-leaf', [False, False]),
    ('wrong-session', [True, False]), ('order', [False, False]), ('cost-shape', [True, False]),
])
def test_the_optional_restart_checkpoint_needs_its_bridge_status_anchor(tmp_path, fault, classified):
    *resume_rows, prompt, cost = remote_restart_checkpoint()
    checkpoint = [prompt, cost]
    if fault == 'missing-prompt':
        checkpoint = [cost]
    elif fault == 'wrong-leaf':
        prompt['leafUuid'] = END
    elif fault == 'wrong-session':
        cost['sessionId'] = 'other-session'
    elif fault == 'order':
        checkpoint = [cost, prompt]
    elif fault == 'cost-shape':
        cost['hasUnknownModelCost'] = 0
    path = record(tmp_path, remote_origin(NEW) + resume_rows)
    settled = claude.state(path)
    assert idle.check(settled) is None
    queue = claude._Reader(path).watch_relaunch
    for entry, private in zip(checkpoint, classified, strict=True):
        assert queue.take(entry, None, queue_empty=True) is private
        with path.open('a') as stream:
            stream.write(json.dumps(entry) + '\n')
        assert idle.check(claude.state(path)) is None
        assert not claude.inputs(path).skipped


def remote_continuation():
    return [
        dict(type='last-prompt', sessionId=SESSION, leafUuid='synthetic-resumed-bridge-status',
             lastPrompt='Arm the inbox watch.'),
        flow('data-flow-records', records=[
            dict(tool_name='RemoteControlLinked', event_id=REMOTE_EVENT, no_message=True),
            dict(tool_name='RemoteControlInput', event_id='event_00000000-0000-4000-8000-000000000032',
                 no_message=True),
        ]),
    ]


def test_the_upgrade_continuation_keeps_quiet_time_unknown_without_a_safe_upper_bound(tmp_path):
    rows = remote_origin(OLD) + remote_resume(OLD) + remote_continuation()
    path = record(tmp_path, rows)
    assert idle.check(claude.state(path)) is None
    assert claude.state(path).turns == 1
    assert [item.text for item in claude.inputs(path)] == ['Arm the inbox watch.']
    assert not claude.inputs(path).skipped
    with pytest.raises(ValueError, match='an input row has no time'):
        claude.person_inputs(path, ())

    framework = [typed(11, 'wake', human_wake.LINE), typed(12, 'brief', 'qd > briefed as quill')]
    ordinary_metadata = flow('data-flow-records', records=[])
    stale = typed(6, 'stale', 'An old prompt placed later.')
    record(tmp_path, rows + [ordinary_metadata, *framework, stale])
    with pytest.raises(ValueError, match='an input row has no time'):
        claude.person_inputs(path, (human_wake.LINE,))

    actual = typed(13, 'person', 'A person typed this.')
    record(tmp_path, rows + [ordinary_metadata, *framework, actual])
    with pytest.raises(ValueError, match='an input row has no time'):
        claude.person_inputs(path, (human_wake.LINE,))


@pytest.mark.parametrize('fault', ['fresh', 'wrong-checkpoint', 'wrong-link', 'same-id', 'extra-field',
                                   'stripped-event', 'untagged-event', 'snapshot', 'foreign'])
def test_only_the_witnessed_upgrade_continuation_is_admitted(tmp_path, fault):
    checkpoint, delta = remote_continuation()
    linked, input_event = delta['records']
    prefix = remote_origin(OLD) + remote_resume(OLD)
    if fault == 'fresh':
        prefix = remote_origin(NEW) + remote_resume(NEW)
    elif fault == 'wrong-checkpoint':
        checkpoint['leafUuid'] = END
    elif fault == 'wrong-link':
        linked['event_id'] = input_event['event_id']
    elif fault == 'same-id':
        input_event['event_id'] = linked['event_id']
    elif fault == 'extra-field':
        input_event['response_id'] = 'msg_synthetic'
    elif fault == 'stripped-event':
        input_event['tool_name'] = '<str 18>'
    elif fault == 'untagged-event':
        input_event['no_message'] = False
    elif fault == 'snapshot':
        delta['snapshot'] = True
    else:
        delta['sessionId'] = 'foreign'
    path = record(tmp_path, prefix + [checkpoint, delta])
    assert claude.state(path).pending_tools is None
    assert claude.inputs(path).skip_counts['data-flow-records', 'unknown native data-flow metadata'] == 1
    with pytest.raises(ValueError, match='unknown native data-flow metadata'):
        claude.person_inputs(path, ())


TYPED_EVENT = 'event_00000000-0000-4000-8000-000000000059'


def remote_typed(seconds, text, *, blobs=2):
    first = typed(seconds, 'new-prompt', text)
    first.update(uuid='new-input', dataFlowOn=True)
    chain = [first]
    for kind in ('deferred_tools_delta', 'agent_listing_delta', 'mcp_instructions_delta',
                 'skill_listing', 'total_tokens_reminder', 'session_context'):
        chain.append(row('attachment', seconds, NEW, uuid=f'new-{kind}', parentUuid=chain[-1]['uuid'],
                         dataFlowOn=True, attachment={'type': kind}))
    chain[-1]['session_id'] = SESSION
    delta = flow('data-flow-records', records=[dict(tool_name='RemoteControlInput', event_id=TYPED_EVENT)])
    writes = [dict(type='api-request-blob', sessionId=SESSION, hash=digest,
                   message={'role': role, 'content': [{'type': 'text', 'text': content}]})
              for digest, role, content in [('1' * 64, 'user', text), ('2' * 64, 'system', 'Synthetic context.')]]
    request = dict(type='api-request', sessionId=SESSION, id='new-request', timestamp=stamp(seconds + 1),
                   version=NEW, querySource='repl_main_thread', shapeHash='3' * 64, params={},
                   keep=0, tail=['0' * 64, '1' * 64, '2' * 64], cacheControl=[], requestDigest='4' * 64)
    if blobs < 2:
        request.update(keep=2, base='previous-request')
    return [*chain, delta, *writes[:blobs], request]


@pytest.mark.parametrize('blobs', [0, 1, 2])
@pytest.mark.parametrize('text', ['A person typed this.', human_wake.LINE, human_wake.LINE + ' person comment'])
def test_one_timed_input_binds_its_remote_event_after_the_request_write(tmp_path, blobs, text):
    prefix = remote_origin(OLD) + remote_resume(OLD)
    suffix = remote_typed(9, text, blobs=blobs)
    path = record(tmp_path, prefix + suffix[:7])
    assert claude.person_inputs(path, (human_wake.LINE,)) == ([AT] if text == human_wake.LINE else
                                                           [AT, AT + timedelta(seconds=9)])
    for entry in suffix[7:]:
        with path.open('a') as stream:
            stream.write(json.dumps(entry))
        assert claude.state(path).pending_tools is None
        with pytest.raises(ValueError, match='unknown native data-flow metadata'):
            claude.person_inputs(path, (human_wake.LINE,))
        with path.open('a') as stream:
            stream.write('\n')
        state = claude.state(path)
        assert replace(state, projection_usage=None) == claude._Reader(path).state()
        assert (state.pending_tools == ()) == (entry is suffix[-1])
        assert bool(claude.inputs(path).skipped) == (entry is not suffix[-1])
    assert state.turn_open and state.turns == 1 and state.queue_error is None
    assert [item.text for item in claude.inputs(path)] == ['Arm the inbox watch.', text]
    expected = [AT] if text == human_wake.LINE else [AT, AT + timedelta(seconds=9)]
    assert claude.person_inputs(path, (human_wake.LINE,)) == expected


@pytest.mark.parametrize('fault', [
    'prompt-link', 'sidechain', 'older-version', 'untimed', 'attachment-parent', 'attachment-session',
    'attachment-order', 'attachment-id', 'context-session', 'extra-row', 'late-attachment', 'event-session',
    'no-message', 'snapshot', 'duplicate-blob', 'blob-session', 'blob-order', 'hash-mismatch', 'masked-hashes',
    'request-session', 'request-source', 'request-version', 'request-time', 'trailing-request',
])
def test_a_remote_input_with_a_broken_join_keeps_quiet_time_unknown(tmp_path, fault):
    suffix = remote_typed(9, human_wake.LINE)
    typed_row, *attachments, event, user_blob, system_blob, request = suffix
    if fault == 'prompt-link':
        typed_row['turnPromptId'] = 'other-prompt'
    elif fault == 'sidechain':
        typed_row['isSidechain'] = True
    elif fault == 'older-version':
        typed_row['version'] = OLD
    elif fault == 'untimed':
        typed_row['timestamp'] = ''
    elif fault == 'attachment-parent':
        attachments[1]['parentUuid'] = typed_row['uuid']
    elif fault == 'attachment-session':
        attachments[1]['sessionId'] = 'other-session'
    elif fault == 'attachment-order':
        attachments[1]['attachment'], attachments[2]['attachment'] = (
            attachments[2]['attachment'], attachments[1]['attachment'])
    elif fault == 'attachment-id':
        attachments[-1]['uuid'] = typed_row['uuid']
    elif fault == 'context-session':
        attachments[-1]['session_id'] = 'other-session'
    elif fault == 'extra-row':
        suffix.insert(7, flow('data-flow-records', records=[]))
    elif fault == 'late-attachment':
        suffix[6], suffix[7] = suffix[7], suffix[6]
    elif fault == 'event-session':
        event['sessionId'] = 'other-session'
    elif fault == 'no-message':
        event['records'][0]['no_message'] = True
    elif fault == 'snapshot':
        event['snapshot'] = True
    elif fault == 'duplicate-blob':
        suffix.insert(-1, user_blob)
    elif fault == 'blob-session':
        user_blob['sessionId'] = 'other-session'
    elif fault == 'blob-order':
        suffix[-3], suffix[-2] = system_blob, user_blob
    elif fault == 'hash-mismatch':
        user_blob['hash'] = '5' * 64
    elif fault == 'masked-hashes':
        user_blob['hash'] = system_blob['hash'] = '<str 64>'
        request['tail'] = ['<str 64>'] * 3
    elif fault == 'request-session':
        request['sessionId'] = 'other-session'
    elif fault == 'request-source':
        request['querySource'] = 'agent:unrelated'
    elif fault == 'request-version':
        request['version'] = OLD
    elif fault == 'request-time':
        request['timestamp'] = stamp(8)
    else:
        suffix.insert(-1, dict(type='file-history-snapshot'))

    path = record(tmp_path, remote_origin(OLD) + remote_resume(OLD) + suffix)
    assert claude.state(path).pending_tools is None
    assert claude.inputs(path).skip_counts['data-flow-records', 'unknown native data-flow metadata'] == 1
    with pytest.raises(ValueError, match='unknown native data-flow metadata'):
        claude.person_inputs(path, (human_wake.LINE,))


@pytest.mark.parametrize(('version', 'resume_rows'), [
    (OLD, remote_resume(OLD)), (NEW, remote_resume(NEW)), (NEW, remote_restart_checkpoint()[:-2]),
], ids=['upgrade', 'same-version', 'same-version-code-9'])
def test_a_remote_resume_allows_one_fixed_wake_receipt_and_watch_rearm(tmp_path, monkeypatch, version, resume_rows):
    rows = remote_origin(version) + resume_rows
    path = record(tmp_path, rows)
    harness = claude.ClaudeHarness()
    screen = harness.screen('\n'.join(['─' * 80, '❯\xa0', '─' * 80,
                                       '  ⏵⏵ bypass permissions on (shift+tab to cycle)']))
    pane = SimpleNamespace(dead=False, in_mode=False)
    seat = SimpleNamespace(harness=harness, record=lambda: path, session=SimpleNamespace(human=True),
                           name='quill', ship=SimpleNamespace(runtime=tmp_path), pane=lambda: pane,
                           launched=lambda: SimpleNamespace(), join=lambda: SimpleNamespace(record=path))
    monkeypatch.setattr(human_wake, 'candidate_hold', lambda *args: None)
    since = AT + timedelta(seconds=8)
    store = post.Store(tmp_path)
    waiting = store.new('order', 'ship', seat.name, 'Synthetic waiting post.')
    attempts = post.Attempts(tmp_path)
    assert [item.id for item in attempts.undelivered(store, seat.name)] == [waiting.id]
    assert not post.watch_armed(tmp_path, seat.name)
    assert idle.check(harness.state(path)) is None
    assert human_wake.eligible(seat, screen) is None
    assert input_receipt.confirmed(harness, path, human_wake.LINE, since) is None

    wake_rows = remote_typed(9, human_wake.LINE)
    rearmed = started_watch()[1:]
    for seconds, entry in enumerate(rearmed, 11):
        entry['timestamp'] = stamp(seconds)
    rearmed[0]['message']['content'][0]['id'] = 'toolu_Rearmed'
    result = rearmed[1]['message']['content'][0]
    result['tool_use_id'] = 'toolu_Rearmed'
    result['content'] = result['content'].replace(TASK, 'rearmed')
    rearmed[1]['toolUseResult']['backgroundTaskId'] = 'rearmed'
    rearmed[-1]['uuid'] = 'synthetic-rearm-end'
    record(tmp_path, rows + wake_rows)
    assert input_receipt.confirmed(harness, path, human_wake.LINE, since) == 'delivered'
    assert human_wake.eligible(seat, screen) is not None
    assert claude.person_inputs(path, (human_wake.LINE,)) == [AT]
    delivered, opened = post.wait(tmp_path, seat.name, stop_after=0)
    assert [attempt.message for attempt in opened] == [waiting.id] and waiting.body in delivered
    post.printed(tmp_path, opened)
    assert attempts.latest(seat.name)[waiting.id] == 'watch'

    record(tmp_path, rows + wake_rows + rearmed)
    assert sum(item.text == human_wake.LINE for item in harness.inputs(path)) == 1
    reader = claude._Reader(path)
    assert [task.phase for task in reader.background.task_states()] == ['ended-by-relaunch', 'pending']
    assert reader.state().pending_tools == ('Bash',)
    monkeypatch.setattr(inbox_watch, 'current', lambda candidate, at: candidate is seat and at.isoformat() == stamp(11))
    assert idle.check(reader.state(), seat) is None
