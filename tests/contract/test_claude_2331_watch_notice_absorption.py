"""Synthetic forms of the seat's own watch notice absorbed into a running turn, on 2.1.331.

Built from the sanitized measurement of eight such sequences (key sets, fixed values, row order and equalities):
no text, id or time of any real record. Form R writes the remove before the turn's assistant rows; form A writes an
assistant row first, whose first block is thinking or tool_use.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from quarterdeck.harness import claude, claude_background, claude_record


VERSION = '2.1.331'
AT = datetime(2033, 6, 7, 8, 9, tzinfo=timezone.utc)
SESSION = 'synthetic-absorb-session'
WATCH_CALL, TASK, OUT = 'toolu_SyntheticWatch', 'synthwatch', '/synthetic/tasks/synthwatch.output'
DESC = 'Wait for the inbox'
ENQUEUED = 10


def stamp(second):
    return (AT + timedelta(seconds=second)).isoformat()


def row(kind, second, **fields):
    return dict(type=kind, timestamp=stamp(second), sessionId=SESSION, isSidechain=False, version=VERSION, **fields)


def queue(second, operation, **fields):
    return dict(type='queue-operation', operation=operation, timestamp=stamp(second), sessionId=SESSION, **fields)


def assistant(second, identity, block, *, parent):
    return row('assistant', second, uuid=identity, parentUuid=parent, session_id=SESSION, madeBy='model',
               requestRef='synthetic-request', message=dict(role='assistant', stop_reason='tool_use', content=[block]))


def tool_result(second, identity, call, parent):
    return row('user', second, uuid=identity, parentUuid=parent, sourceToolAssistantUUID=parent, session_id=SESSION,
               promptId='synthetic-prompt', message=dict(role='user', content=[
                   dict(type='tool_result', tool_use_id=call, content='Synthetic result.', is_error=False)]),
               toolUseResult=dict(stdout='synthetic', stderr='', interrupted=False, isImage=False,
                                  noOutputExpected=False))


def watch_notice(task=TASK, call=WATCH_CALL, output=OUT):
    return (f'<task-notification>\n<task-id>{task}</task-id>\n<tool-use-id>{call}</tool-use-id>\n'
            f'<output-file>{output}</output-file>\n<status>completed</status>\n'
            f'<summary>Background command "{DESC}" completed (exit code 0)</summary>\n</task-notification>')


def armed(command='qd inbox --wait'):
    """A turn arms the inbox watch and goes on working; nothing here is a typed turn the watch queue owns."""
    prompt = row('user', 0, uuid='synthetic-prompt-row', promptId='synthetic-prompt', origin=dict(kind='human'),
                 message=dict(role='user', content='Arm the watch and work.'))
    call = assistant(1, 'synthetic-watch-call', dict(type='tool_use', id=WATCH_CALL, name='Bash', input=dict(
        command=command, description=DESC, run_in_background=True)), parent=prompt['uuid'])
    ack = row('user', 2, uuid='synthetic-watch-ack', parentUuid=call['uuid'], sourceToolAssistantUUID=call['uuid'],
              session_id=SESSION, promptId='synthetic-prompt', message=dict(role='user', content=[dict(
                  type='tool_result', tool_use_id=WATCH_CALL, is_error=False,
                  content=(f'{claude_record.BACKGROUND_HEADER}{TASK}. Output is being written to: {OUT}. '
                           + claude_background.BASH_ACK_GUIDANCE))]),
              toolUseResult=dict(backgroundTaskId=TASK, stdout='', stderr='', interrupted=False, isImage=False,
                                 noOutputExpected=False))
    return [prompt, call, ack]


def attachment(parent, text=None):
    text = watch_notice() if text is None else text
    return row('attachment', ENQUEUED, uuid='synthetic-queued-attachment', parentUuid=parent, session_id=SESSION,
               slug='synthetic-slug', renderedRole='system', userType='external', entrypoint='cli',
               cwd='/synthetic', gitBranch='main', dataFlowOn=True, rendered=[dict(content=text)],
               renderedInHumanTurn=[dict(content=text)],
               attachment=dict(type='queued_command', prompt=text, source_uuid='synthetic-command',
                               delivery_id='synthetic-delivery', commandMode='task-notification',
                               origin=dict(kind='task-notification', producer='session-task'),
                               rowSource='recorded', runId='synthetic-run', timestamp=stamp(ENQUEUED)))


def remove(second, text=None):
    return queue(second, 'remove', content=watch_notice() if text is None else text, reason='absorbed_mid_turn',
                 commandUuid='synthetic-command', deliveryId='synthetic-delivery')


def form(name):
    """The rows from the enqueue to the attachment, in each measured order."""
    enqueue = queue(ENQUEUED, 'enqueue', content=watch_notice())
    thinking = assistant(11, 'synthetic-thinking', dict(type='thinking', thinking=''), parent='synthetic-watch-ack')
    read = assistant(12, 'synthetic-read', dict(type='tool_use', id='toolu_SyntheticRead', name='Read',
                                                input=dict(file_path='/synthetic/file')), parent=thinking['uuid'])
    result = tool_result(14, 'synthetic-read-result', 'toolu_SyntheticRead', read['uuid'])
    if name == 'R':  # remove first, then the turn's thinking, tool call and result
        return [enqueue, remove(14), thinking, read, result, attachment(result['uuid'])]
    if name == 'R-result-only':  # the turn's call was written before the enqueue; only its result follows
        thinking['timestamp'], read['timestamp'] = stamp(ENQUEUED - 2), stamp(ENQUEUED - 1)
        return [thinking, read, enqueue, remove(14), result, attachment(result['uuid'])]
    if name == 'A-thinking':  # an assistant row first, with context rows between, the result before the remove
        thinking['timestamp'] = stamp(ENQUEUED - 4)  # written after the enqueue, timed at its request's start
        return [enqueue, row('attachment', 11, uuid='synthetic-deferred', parentUuid='synthetic-watch-ack',
                             attachment=dict(type='deferred_tools_record')),
                dict(type='file-history-delta', sessionId=SESSION),
                thinking, read, result, remove(14.1), attachment(result['uuid'])]
    if name == 'A-tool-use':  # the first assistant block is tool_use, the remove between it and its result
        read['parentUuid'] = 'synthetic-watch-ack'
        foreground = assistant(12, 'synthetic-foreground', dict(type='tool_use', id='toolu_SyntheticFore',
                                                                name='Bash', input=dict(command='sleep 3')),
                               parent='synthetic-watch-ack')
        result = tool_result(15, 'synthetic-fore-result', 'toolu_SyntheticFore', foreground['uuid'])
        return [enqueue, foreground, dict(type='api-request-blob', sessionId=SESSION), remove(15), result,
                attachment(result['uuid'])]
    raise AssertionError(name)


def tail():
    answer = row('assistant', 20, uuid='synthetic-answer', parentUuid='synthetic-queued-attachment',
                 session_id=SESSION, madeBy='model', requestRef='synthetic-request',
                 message=dict(role='assistant', stop_reason='end_turn', content=[dict(type='text', text='Done.')]))
    return [answer, row('system', 21, subtype='turn_duration', uuid='synthetic-end', parentUuid=answer['uuid'],
                        durationMs=21000)]


def write(path, rows, mode='w'):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open(mode) as stream:
        stream.writelines(json.dumps(entry) + '\n' for entry in rows)


def record(tmp_path, rows):
    path = tmp_path / 'runtime/seats/quill/home/projects/workspace' / f'{SESSION}.jsonl'
    write(path, rows)
    return path


FORMS = ['R', 'R-result-only', 'A-thinking', 'A-tool-use']
UNRESOLVED = 'task notification queue unresolved'


@pytest.mark.parametrize('name', FORMS)
def test_an_absorbed_own_watch_notice_settles_exactly_its_watch(tmp_path, name):
    path = record(tmp_path, armed() + form(name) + tail())
    reader = claude._Reader(path)
    state = reader.state()
    assert [(task.call, task.phase, task.terminal) for task in reader.background.task_states()] == [
        (WATCH_CALL, 'ended', 'completed')]
    assert state.queue_error is None and state.pending_tools == () and state.turn_open is False
    assert state.queued == state.consumed == state.active_tools == ()
    assert getattr(state, 'absorbed_watch', None) is None  # the durable binding is not this reading's
    inputs = claude.inputs(path)
    assert [item.text for item in inputs] == ['Arm the watch and work.'] and not inputs.skipped
    cold_state, cold_turns = claude.read(record(tmp_path / 'cold', armed() + form(name) + tail()))
    warm_state, warm_turns = claude.read(path)
    assert replace(warm_state, last_write=cold_state.last_write) == cold_state and warm_turns == cold_turns


@pytest.mark.parametrize('name', FORMS)
def test_the_hold_lasts_until_the_attachment_line_is_complete(tmp_path, name):
    rows = form(name)
    first = next(i for i, entry in enumerate(rows) if entry.get('operation') == 'enqueue')
    path = record(tmp_path, armed() + rows[:first])
    assert claude.state(path).queue_error is None
    for entry in rows[first:-1]:
        write(path, [entry], 'a')
        state = claude.state(path)
        assert state.queue_error == UNRESOLVED and state.pending_tools is None
        assert claude.inputs(path).skipped
    with path.open('a') as stream:
        stream.write(json.dumps(rows[-1]))
    assert claude.state(path).queue_error == UNRESOLVED
    with path.open('a') as stream:
        stream.write('\n')
    state = claude.state(path)
    assert state.queue_error is None and state.turn_open is True
    assert not claude.inputs(path).skipped


def test_the_attachment_may_omit_the_slug_as_the_earlier_measure_did(tmp_path):
    rows = form('R')
    del rows[-1]['slug']
    assert claude.state(record(tmp_path, armed() + rows + tail())).queue_error is None


def broken(rows, fault):
    """One measured equality or field broken on a form's rows."""
    enqueue, removal, last = rows[0], next(r for r in rows if r.get('operation') == 'remove'), rows[-1]
    value = last['attachment']
    if fault == 'remove-text':
        removal['content'] += '\n'
    elif fault == 'remove-reason':
        removal['reason'] = 'delivered_to_agent'
    elif fault == 'remove-same-ids':
        removal['deliveryId'] = removal['commandUuid']
    elif fault == 'remove-foreign-session':
        removal['sessionId'] = 'foreign-session'
    elif fault == 'remove-before-enqueue':
        removal['timestamp'] = stamp(ENQUEUED - 1)
    elif fault == 'remove-extra-key':
        removal['extra'] = True
    elif fault == 'attachment-prompt':
        value['prompt'] += '\n'
    elif fault == 'attachment-source':
        value['source_uuid'] = 'other-command'
    elif fault == 'attachment-delivery':
        value['delivery_id'] = 'other-delivery'
    elif fault == 'attachment-time':
        last['timestamp'] = stamp(ENQUEUED + .1)
    elif fault == 'attachment-parent':
        last['parentUuid'] = 'synthetic-thinking'
    elif fault == 'attachment-origin':
        value['origin'] = dict(kind='task-notification', producer='other')
    elif fault == 'attachment-mode':
        value['commandMode'] = 'prompt'
    elif fault == 'attachment-row-source':
        value['rowSource'] = 'harness'
    elif fault == 'attachment-role':
        last['renderedRole'] = 'user'
    elif fault == 'attachment-extra-key':
        last['extra'] = True
    elif fault == 'attachment-foreign-session':
        last['sessionId'] = 'foreign-session'
    elif fault == 'missing-remove':
        rows.remove(removal)
    elif fault == 'missing-attachment':
        rows.pop()
    elif fault == 'missing-result':
        rows[:] = [r for r in rows if not claude_background.root_tool_result(r)]
    elif fault == 'result-unlinked':
        next(r for r in rows if claude_background.root_tool_result(r))['sourceToolAssistantUUID'] = 'other-row'
    elif fault == 'result-other-call':
        next(r for r in rows if claude_background.root_tool_result(r))['message']['content'][0]['tool_use_id'] = (
            'toolu_Unproved')
    elif fault == 'assistant-after-result':
        rows.insert(len(rows) - 1, assistant(16, 'synthetic-late', dict(type='thinking', thinking=''),
                                             parent='synthetic-read-result'))
    elif fault == 'text-before-tools':
        rows.insert(1, assistant(11, 'synthetic-speech', dict(type='text', text='Working.'),
                                 parent='synthetic-watch-ack'))
    elif fault == 'unmeasured-system-row':
        rows.insert(len(rows) - 1, row('system', 16, subtype='informational', uuid='synthetic-info', content='x'))
    elif fault == 'unmeasured-attachment':
        rows.insert(len(rows) - 1, row('attachment', 16, uuid='synthetic-other-attachment',
                                       parentUuid='synthetic-read-result', attachment=dict(type='edited_text_file')))
    elif fault == 'sidechain-row':
        rows.insert(len(rows) - 1, dict(assistant(16, 'synthetic-child', dict(type='thinking', thinking=''),
                                                  parent='synthetic-child-parent'), isSidechain=True))
    elif fault == 'result-twice':
        first = next(r for r in rows if claude_background.root_tool_result(r))
        rows.insert(rows.index(first) + 1, dict(first, uuid='synthetic-second-result'))
        rows[-1]['parentUuid'] = 'synthetic-second-result'
    elif fault == 'foreign-bookkeeping':
        rows.insert(len(rows) - 1, dict(type='api-request-blob', sessionId='foreign-session'))
    elif fault == 'turn-ended':
        rows.insert(len(rows) - 1, row('system', 16, subtype='turn_duration', uuid='synthetic-early-end'))
    elif fault == 'typed-prompt':
        rows.insert(len(rows) - 1, row('user', 16, uuid='synthetic-typed', origin=dict(kind='human'),
                                       message=dict(role='user', content='Another request.')))
    elif fault == 'dequeue':
        rows.insert(rows.index(removal), queue(12, 'dequeue'))
    elif fault == 'other-queued-command':
        rows.insert(rows.index(removal) + 1, attachment('synthetic-read-result', text='Another queued command.'))
    else:
        raise AssertionError(fault)
    return rows


FAULTS = ['remove-text', 'remove-reason', 'remove-same-ids', 'remove-foreign-session', 'remove-before-enqueue',
          'remove-extra-key', 'attachment-prompt', 'attachment-source', 'attachment-delivery', 'attachment-time',
          'attachment-parent', 'attachment-origin', 'attachment-mode', 'attachment-row-source', 'attachment-role',
          'attachment-extra-key', 'attachment-foreign-session', 'missing-remove', 'missing-attachment',
          'missing-result', 'result-unlinked', 'result-other-call', 'result-twice', 'assistant-after-result',
          'text-before-tools',
          'unmeasured-system-row', 'unmeasured-attachment', 'sidechain-row', 'foreign-bookkeeping', 'turn-ended',
          'typed-prompt', 'dequeue', 'other-queued-command']


def assert_unknown(path):
    reader = claude._Reader(path)
    state = reader.state()
    assert all(task.terminal != 'completed' for task in reader.background.task_states())
    assert state.queue_error == UNRESOLVED and state.pending_tools is None
    assert claude.inputs(path).skipped


@pytest.mark.parametrize('name', ['R', 'A-thinking'])
@pytest.mark.parametrize('fault', FAULTS)
def test_a_broken_or_missing_equality_leaves_the_queue_unknown(tmp_path, name, fault):
    assert_unknown(record(tmp_path, armed() + broken(form(name), fault) + tail()))


@pytest.mark.parametrize('name', FORMS)
def test_a_second_matching_pending_notice_leaves_the_queue_unknown(tmp_path, name):
    rows = form(name)
    first = next(i for i, entry in enumerate(rows) if entry.get('operation') == 'enqueue')
    rows.insert(first + 1, queue(ENQUEUED + .5, 'enqueue', content=watch_notice()))
    assert_unknown(record(tmp_path, armed() + rows + tail()))


@pytest.mark.parametrize('foreign', ['other-ship-watch', 'ordinary-command', 'other-task', 'other-call',
                                     'other-output', 'unregistered-call'])
@pytest.mark.parametrize('name', ['R', 'A-tool-use'])
def test_a_notice_that_is_not_the_seats_own_watch_leaves_the_queue_unknown(tmp_path, name, foreign):
    start, rows = armed(), form(name)
    text = None
    if foreign == 'other-ship-watch':
        start = armed('/elsewhere/runtime/bin/qd inbox --wait')
    elif foreign == 'ordinary-command':
        start = armed('sleep 120')
    elif foreign == 'other-task':
        text = watch_notice(task='othertask')
    elif foreign == 'other-call':
        text = watch_notice(call='toolu_OtherCall')
    elif foreign == 'other-output':
        text = watch_notice(output='/synthetic/tasks/other.output')
    else:
        start = start[:1]  # the watch's call and acknowledgement were never recorded here
    if text is not None:
        for entry in rows:
            if entry.get('type') == 'queue-operation':
                entry['content'] = text
            elif entry.get('type') == 'attachment' and entry['attachment'].get('type') == 'queued_command':
                entry['attachment']['prompt'] = text
                entry['rendered'] = entry['renderedInHumanTurn'] = [dict(content=text)]
    path = record(tmp_path, start + rows + tail())
    state = claude.state(path)
    assert state.queue_error == UNRESOLVED and state.pending_tools is None
    assert claude.inputs(path).skipped


@pytest.mark.parametrize('fault', ['call-not-last', 'call-never-recorded', 'call-from-another-session',
                                   'call-already-closed', 'call-before-turn-end', 'call-before-prompt'])
def test_a_result_closes_only_the_open_call_written_last_before_the_enqueue(tmp_path, fault):
    rows = form('R-result-only')
    read = rows[1]
    if fault == 'call-already-closed':
        rows.insert(2, dict(tool_result(ENQUEUED - .5, 'synthetic-earlier-result', 'toolu_SyntheticRead',
                                        read['uuid'])))
    elif fault == 'call-before-turn-end':
        rows.insert(2, row('system', ENQUEUED - .5, subtype='turn_duration', uuid='synthetic-old-end'))
    elif fault == 'call-before-prompt':
        rows.insert(2, row('user', ENQUEUED - .5, uuid='synthetic-next-prompt', origin=dict(kind='human'),
                           message=dict(role='user', content='Next request.')))
    elif fault == 'call-not-last':
        rows.insert(2, assistant(ENQUEUED - .5, 'synthetic-other-call', dict(
            type='tool_use', id='toolu_SyntheticOther', name='Read', input=dict(file_path='/synthetic/other')),
            parent=read['uuid']))
    elif fault == 'call-never-recorded':
        rows.remove(read)
    else:
        read['sessionId'] = 'foreign-session'
    assert_unknown(record(tmp_path, armed() + rows + tail()))


def test_an_ended_watch_cannot_be_settled_again(tmp_path):
    stop = [assistant(4, 'synthetic-stop', dict(type='tool_use', id='toolu_SyntheticStop', name='TaskStop',
                                                input=dict(task_id=TASK)), parent='synthetic-watch-ack')]
    mirror = dict(message=f'Successfully stopped task: {TASK} (qd inbox --wait)', task_id=TASK,
                  task_type='local_bash', command='qd inbox --wait')
    stopped = tool_result(5, 'synthetic-stop-result', 'toolu_SyntheticStop', 'synthetic-stop')
    stopped['toolUseResult'] = mirror
    path = record(tmp_path, armed() + stop + [stopped] + form('R') + tail())
    assert claude.state(path).queue_error == UNRESOLVED
