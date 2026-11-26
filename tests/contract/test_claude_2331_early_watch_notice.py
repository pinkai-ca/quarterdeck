"""Synthetic form of the seat's own watch completion enqueued before the rows of the call that started the watch.

Built from the sanitized row-by-row measurement of one such sequence on 2.1.331 (key sets, fixed values, row order,
relative times and equalities): no text, id or time of any real record. The watch returned within seconds, so its
completion was enqueued first; then, in file order, the same request's thinking row, a text row, the Bash call that
started the watch, its result naming the task, a second tool call, a metadata run, the absorbed remove, the second
call's result and the queued_command attachment.
"""

from dataclasses import replace
from datetime import timedelta
import json

import pytest

from quarterdeck.harness import claude, claude_background, claude_record

from .test_claude_2331_watch_notice_absorption import (
    DESC, ENQUEUED, OUT, SESSION, TASK, UNRESOLVED, WATCH_CALL, assert_unknown, attachment, queue, record, remove,
    row, stamp, watch_notice, write)

REQUEST = dict(requestId='synthetic-request-id', requestRef='synthetic-request')
SECOND_CALL = 'toolu_SyntheticSecond'
LATE_METADATA = ['last-prompt', 'custom-title', 'agent-name', 'mode', 'permission-mode', 'session-owner',
                 'atis-latch', 'history-suppression', 'bridge-session']


def turn():
    """A typed turn already running before the enqueue; the request that starts the watch is written after it."""
    return [row('user', 0, uuid='synthetic-prompt-row', promptId='synthetic-prompt', origin=dict(kind='human'),
                message=dict(role='user', content='Work, and arm the watch.'))]


def model_row(second, identity, block, *, parent, **fields):
    return row('assistant', second, uuid=identity, parentUuid=parent, session_id=SESSION, madeBy='model',
               message=dict(role='assistant', stop_reason='tool_use', content=[block]), **{**REQUEST, **fields})


def metadata(kind):
    """The run's rows by their measured own-session shapes; the last two are measured only in this form."""
    if kind == 'bridge-session':
        return dict(type=kind, sessionId=SESSION, bridgeSessionId='synthetic-bridge-'.ljust(28, '0'),
                    lastSequenceNum=0, noHistoryBackfill=True, ownerAccountUuid='synthetic-account-'.ljust(36, '0'),
                    ownerOrganizationUuid='synthetic-organization-'.ljust(36, '0'))
    if kind == 'history-suppression':
        return dict(type=kind, sessionId=SESSION, cause='migration', ts=stamp(ENQUEUED + 1.3))
    return dict(type=kind, sessionId=SESSION)


def late(command='qd inbox --wait'):
    """The measured late-binding sequence, from the enqueue to the attachment."""
    enqueue = queue(ENQUEUED, 'enqueue', content=watch_notice())
    thinking = model_row(ENQUEUED - 4.9, 'synthetic-thinking', dict(type='thinking', thinking=''),
                         parent='synthetic-prompt-row', thinkingDisplay='summarized', thinkingDurationMs=1)
    text = model_row(ENQUEUED - 4.9, 'synthetic-text', dict(type='text', text='Arming the watch.'),
                     parent=thinking['uuid'])
    bash = dict(type='tool_use', id=WATCH_CALL, name='Bash',
                input=dict(command=command, description=DESC, run_in_background=True))
    call = model_row(ENQUEUED - 3.9, 'synthetic-watch-call', bash, parent=text['uuid'], toolInputStreamMs=1,
                     wireToolInputs=True)
    ack = row('user', ENQUEUED - 3.85, uuid='synthetic-watch-ack', parentUuid=call['uuid'],
              sourceToolAssistantUUID=call['uuid'], session_id=SESSION, promptId='synthetic-prompt', sandboxed=False,
              message=dict(role='user', content=[dict(
                  type='tool_result', tool_use_id=WATCH_CALL, is_error=False,
                  content=(f'{claude_record.BACKGROUND_HEADER}{TASK}. Output is being written to: {OUT}. '
                           + claude_background.BASH_ACK_GUIDANCE))]),
              toolUseResult=dict(backgroundTaskId=TASK, stdout='', stderr='', interrupted=False, isImage=False,
                                 noOutputExpected=False))
    second = model_row(ENQUEUED - 1.9, 'synthetic-second-call', dict(type='tool_use', id=SECOND_CALL, name='Read',
                                                                     input=dict(file_path='/synthetic/file')),
                       parent=ack['uuid'], toolInputStreamMs=1, wireToolInputs=True)
    result = row('user', ENQUEUED + 9, uuid='synthetic-second-result', parentUuid=second['uuid'],
                 sourceToolAssistantUUID=second['uuid'], session_id=SESSION, promptId='synthetic-prompt',
                 sandboxed=False, message=dict(role='user', content=[dict(
                     type='tool_result', tool_use_id=SECOND_CALL, content='Synthetic result.', is_error=False)]),
                 toolUseResult=dict(stdout='synthetic', stderr='', interrupted=False, isImage=False,
                                    noOutputExpected=False))
    return [enqueue, thinking, text, call, ack, second, *map(metadata, LATE_METADATA), remove(ENQUEUED + 9), result,
            attachment(result['uuid'])]


def tail():
    blobs = [dict(type='api-request-blob', hash=f'synthetic-{n}', message={}, sessionId=SESSION) for n in range(3)]
    answer = row('assistant', ENQUEUED + 10.8, uuid='synthetic-answer', parentUuid='synthetic-queued-attachment',
                 session_id=SESSION, madeBy='model', requestRef='synthetic-next',
                 message=dict(role='assistant', stop_reason='end_turn', content=[dict(type='text', text='Done.')]))
    return [*blobs, answer, row('system', ENQUEUED + 11, subtype='turn_duration', uuid='synthetic-end',
                                parentUuid=answer['uuid'], durationMs=21000)]


def index(rows, identity):
    return next(i for i, entry in enumerate(rows) if entry.get('uuid') == identity)


def test_the_late_bound_own_watch_notice_settles_exactly_its_watch(tmp_path):
    path = record(tmp_path, turn() + late() + tail())
    reader = claude._Reader(path)
    state = reader.state()
    assert [(task.call, task.phase, task.terminal) for task in reader.background.task_states()] == [
        (WATCH_CALL, 'ended', 'completed')]
    assert reader.task_queue.pending is None and not reader.task_queue.unknown and not reader.task_queue.causes
    assert state.queue_error is None and state.turn_open is False
    assert state.queued == state.consumed == state.active_tools == ()
    assert getattr(state, 'absorbed_watch', None) is None  # the durable binding is not this reading's
    inputs = claude.inputs(path)
    # The measured history-suppression row is the queue's own here: it never reaches the generic unknown handling.
    assert [item.text for item in inputs] == ['Work, and arm the watch.'] and not inputs.skipped
    assert state.pending_tools == () and reader.tools_known
    cold_state, cold_turns = claude.read(record(tmp_path / 'cold', turn() + late() + tail()))
    warm_state, warm_turns = claude.read(path)
    assert replace(warm_state, last_write=cold_state.last_write) == cold_state and warm_turns == cold_turns


def test_the_hold_grants_nothing_until_the_attachment_line_is_complete(tmp_path):
    """Each row is appended in two writes, half a line first: warm and cold readings agree at every length."""
    rows = late()
    path = record(tmp_path, turn())
    assert claude.state(path).queue_error is None
    for number, entry in enumerate(rows):
        line = json.dumps(entry)
        for part, piece in enumerate((line[:len(line) // 2], line[len(line) // 2:], '\n')):
            with path.open('a') as stream:
                stream.write(piece)
            settled = number == len(rows) - 1 and part == 2
            reader = claude._Reader(path)
            state = reader.state()
            cold = record(tmp_path / 'cold' / f'{number}-{len(piece)}', [])
            cold.write_text(path.read_text())
            assert claude.state(cold) == replace(claude.state(path), last_write=claude.state(cold).last_write)
            if number == part == 0:
                assert state.queue_error is None  # half an enqueue line is no notice yet
            elif settled:
                assert state.queue_error is None and state.turn_open is True
                assert [task.terminal for task in reader.background.task_states()] == ['completed']
            else:
                # The deferral settles nothing and proves no idle.
                assert state.queue_error == UNRESOLVED and state.pending_tools is None
                assert all(task.terminal is None for task in reader.background.task_states())


def test_the_stopped_notice_keeps_its_own_cause_beside_the_late_bound_form(tmp_path):
    text = ('<task-notification>\n<task-id>oldtask</task-id>\n<tool-use-id>toolu_SyntheticOld</tool-use-id>\n'
            '<status>stopped</status>\n<summary>Background command "Synthetic" was stopped</summary>\n'
            f'<note>{claude_background.STOPPED_NOTE}</note>\n</task-notification>')
    latch = [queue(-3, 'enqueue', content=text), queue(-2.99, 'dequeue'),
             row('user', -2.98, uuid='synthetic-stopped-row', message=dict(role='user', content=text),
                 origin=dict(kind='task-notification'), rowSource='harness', promptSource='system',
                 queueSkipAttachments=True, queueTranscriptOnly=True, promptId='synthetic-stopped-prompt')]
    path = record(tmp_path, latch + turn() + late() + tail())
    assert claude.state(path).queue_error == claude_background.STOPPED_RESUME
    rows = late()
    rows.insert(index(rows, 'synthetic-text'), metadata('last-prompt'))  # and the late form broken
    assert claude.state(record(tmp_path / 'broken', latch + turn() + rows + tail())).queue_error == UNRESOLVED


def faulted(fault):
    rows = late()
    thinking, text, call, ack, second = (rows[index(rows, identity)] for identity in (
        'synthetic-thinking', 'synthetic-text', 'synthetic-watch-call', 'synthetic-watch-ack', 'synthetic-second-call'))
    command = call['message']['content'][0]['input']
    if fault == 'text-before-thinking':
        rows[1], rows[2] = text, thinking
    elif fault == 'no-text-row':
        rows.remove(text)
        call['parentUuid'] = thinking['uuid']
    elif fault == 'two-text-rows':
        rows.insert(index(rows, call['uuid']), dict(text, uuid='synthetic-text-2', parentUuid=text['uuid']))
        call['parentUuid'] = 'synthetic-text-2'
    elif fault == 'no-thinking-row':
        rows.remove(thinking)
        text['parentUuid'] = 'synthetic-prompt-row'
    elif fault == 'metadata-in-prefix':
        rows.insert(index(rows, call['uuid']), metadata('last-prompt'))
    elif fault == 'turn-end-in-prefix':
        rows.insert(index(rows, call['uuid']), row('system', ENQUEUED, subtype='turn_duration', uuid='synthetic-x'))
    elif fault == 'sidechain-in-prefix':
        rows.insert(index(rows, call['uuid']), dict(text, uuid='synthetic-child', isSidechain=True))
    elif fault == 'text-other-request':
        text['requestId'] = 'synthetic-other-request'
    elif fault == 'call-other-request':
        call['requestRef'] = 'synthetic-other-ref'
    elif fault == 'text-unlinked':
        text['parentUuid'] = 'synthetic-prompt-row'
    elif fault == 'call-unlinked':
        call['parentUuid'] = thinking['uuid']
    elif fault == 'text-with-tool-use':
        text['message']['content'].append(dict(type='tool_use', id='toolu_SyntheticExtra', name='Read', input={}))
    elif fault == 'call-other-tool':
        call['message']['content'][0]['name'] = 'Read'
    elif fault == 'call-foreground':
        command['run_in_background'] = False
    elif fault == 'call-ordinary-command':
        command['command'] = 'sleep 120'
    elif fault == 'call-other-ship':
        command['command'] = '/elsewhere/runtime/bin/qd inbox --wait'
    elif fault == 'call-other-id':
        call['message']['content'][0]['id'] = 'toolu_OtherCall'
    elif fault == 'result-other-task':
        ack['toolUseResult']['backgroundTaskId'] = 'othertask'
    elif fault == 'result-unlinked':
        ack['sourceToolAssistantUUID'] = text['uuid']
    elif fault == 'result-no-prompt':
        del ack['promptId']
    elif fault == 'result-error':
        ack['message']['content'][0]['is_error'] = True
    elif fault == 'result-after-enqueue':
        ack['timestamp'] = stamp(ENQUEUED + .1)
    elif fault == 'result-before-call':
        ack['timestamp'] = stamp(ENQUEUED - 4)
    elif fault == 'result-not-adjacent':
        rows.remove(ack)
        rows.insert(index(rows, second['uuid']) + 1, ack)
        second['parentUuid'] = call['uuid']
    elif fault == 'missing-result':
        rows.remove(ack)
    elif fault == 'missing-second-call':
        rows.remove(second)
    elif fault == 'bound-row-thinking':
        second['message']['content'] = [dict(type='thinking', thinking='')]
    elif fault == 'second-call-unlinked':
        second['parentUuid'] = text['uuid']
    elif fault == 'second-call-other-request':
        second['requestId'] = 'synthetic-other-request'
    elif fault == 'metadata-foreign-session':
        rows[index(rows, second['uuid']) + 1]['sessionId'] = 'foreign-session'
    elif fault == 'unmeasured-metadata':
        rows.insert(index(rows, second['uuid']) + 1, metadata('cost-state'))
    elif fault == 'remove-text':
        next(r for r in rows if r.get('operation') == 'remove')['content'] += '\n'
    elif fault == 'attachment-source':
        rows[-1]['attachment']['source_uuid'] = 'other-command'
    elif fault == 'attachment-time':
        rows[-1]['timestamp'] = stamp(ENQUEUED + .1)
    elif fault == 'attachment-parent-is-binding-result':
        rows[-1]['parentUuid'] = ack['uuid']
    elif fault == 'missing-attachment':
        rows.pop()
    elif fault == 'command-not-text':
        command['command'] = ['qd', 'inbox', '--wait']
    elif fault == 'input-not-a-mapping':
        call['message']['content'][0]['input'] = ['qd inbox --wait']
    elif fault == 'message-not-a-mapping':
        text['message'] = 'Arming the watch.'
    elif fault == 'thinking-at-enqueue':
        thinking['timestamp'] = stamp(ENQUEUED)
    elif fault == 'text-after-enqueue':
        text['timestamp'] = stamp(ENQUEUED + .1)
    elif fault == 'call-after-enqueue':
        call['timestamp'] = stamp(ENQUEUED + .1)
    elif fault == 'call-undated':
        del call['timestamp']
    elif fault == 'text-is-the-thinking-row':
        text['uuid'] = thinking['uuid']
        call['parentUuid'] = thinking['uuid']
    elif fault == 'call-is-the-text-row':
        call['uuid'] = text['uuid']
        ack['parentUuid'] = ack['sourceToolAssistantUUID'] = text['uuid']
    elif fault == 'second-call-is-the-call-row':
        second['uuid'] = call['uuid']
        rows[index(rows, 'synthetic-second-result')]['parentUuid'] = call['uuid']
        rows[index(rows, 'synthetic-second-result')]['sourceToolAssistantUUID'] = call['uuid']
    elif fault.startswith(('suppression-', 'bridge-')):
        kind = 'history-suppression' if fault.startswith('suppression-') else 'bridge-session'
        entry = next(r for r in rows if r.get('type') == kind)
        change = fault.split('-', 1)[1]
        if change == 'other-cause':
            entry['cause'] = 'other'
        elif change == 'extra-key':
            entry['version'] = '2.1.331'
        elif change == 'bad-time':
            entry['ts'] = 'later'
        elif change == 'foreign-session':
            entry['sessionId'] = 'foreign-session'
        elif change == 'sequence-true':
            entry['lastSequenceNum'] = True
        elif change == 'sequence-one':
            entry['lastSequenceNum'] = 1
        elif change == 'backfill-false':
            entry['noHistoryBackfill'] = False
        elif change == 'no-backfill':
            del entry['noHistoryBackfill']
        elif change == 'empty-owner':
            entry['ownerAccountUuid'] = ''
        elif change == 'short-owner':
            entry['ownerOrganizationUuid'] = entry['ownerOrganizationUuid'][:-1]
        elif change == 'long-bridge':
            entry['bridgeSessionId'] += '0'
        elif change == 'id-not-text':
            entry['bridgeSessionId'] = 0
        elif change == 'cause-not-text':
            entry['cause'] = ['migration']
        else:
            raise AssertionError(fault)
    elif fault == 'bookkeeping-before-second-call':
        entry = next(r for r in rows if r.get('type') == 'history-suppression')
        rows.remove(entry)
        rows.insert(index(rows, second['uuid']), entry)
    elif fault == 'bookkeeping-after-remove':
        entry = next(r for r in rows if r.get('type') == 'history-suppression')
        rows.remove(entry)
        rows.insert(next(i for i, r in enumerate(rows) if r.get('operation') == 'remove') + 1, entry)
    elif fault == 'late-bridge-after-remove':
        entry = next(r for r in rows if r.get('type') == 'bridge-session')
        rows.remove(entry)
        rows.insert(next(i for i, r in enumerate(rows) if r.get('operation') == 'remove') + 1, entry)
    elif fault == 'context-before-thinking':
        rows.insert(1, row('attachment', ENQUEUED, uuid='synthetic-deferred', parentUuid='synthetic-prompt-row',
                           attachment=dict(type='deferred_tools_record')))
    elif fault == 'bookkeeping-before-thinking':
        rows.insert(1, metadata('last-prompt'))
    elif fault == 'second-pending-notice':
        rows.insert(1, queue(ENQUEUED + .5, 'enqueue', content=watch_notice()))
    elif fault == 'second-notice-in-prefix':
        rows.insert(index(rows, call['uuid']), queue(ENQUEUED + .5, 'enqueue', content=watch_notice()))
    elif fault == 'dequeue-in-prefix':
        rows.insert(index(rows, call['uuid']), queue(ENQUEUED + .5, 'dequeue'))
    else:
        raise AssertionError(fault)
    return rows


FAULTS = [
    'text-before-thinking', 'no-text-row', 'two-text-rows', 'no-thinking-row', 'metadata-in-prefix',
    'turn-end-in-prefix', 'sidechain-in-prefix', 'text-other-request', 'call-other-request', 'text-unlinked',
    'call-unlinked', 'text-with-tool-use', 'call-other-tool', 'call-foreground', 'call-ordinary-command',
    'call-other-ship', 'call-other-id', 'result-other-task', 'result-unlinked', 'result-no-prompt', 'result-error',
    'result-after-enqueue', 'result-before-call', 'result-not-adjacent', 'missing-result', 'missing-second-call',
    'bound-row-thinking', 'second-call-unlinked', 'second-call-other-request', 'metadata-foreign-session',
    'unmeasured-metadata', 'remove-text', 'attachment-source', 'attachment-time',
    'attachment-parent-is-binding-result', 'missing-attachment', 'second-pending-notice', 'second-notice-in-prefix',
    'dequeue-in-prefix', 'command-not-text', 'input-not-a-mapping', 'message-not-a-mapping', 'thinking-at-enqueue',
    'text-after-enqueue', 'call-after-enqueue', 'call-undated', 'text-is-the-thinking-row', 'call-is-the-text-row',
    'second-call-is-the-call-row', 'suppression-other-cause', 'suppression-extra-key', 'suppression-bad-time',
    'suppression-foreign-session', 'bridge-extra-key', 'bridge-foreign-session', 'bridge-sequence-true',
    'bridge-sequence-one', 'bridge-backfill-false', 'bridge-no-backfill', 'bridge-empty-owner', 'bridge-short-owner',
    'bridge-long-bridge', 'bridge-id-not-text', 'suppression-cause-not-text', 'bookkeeping-before-second-call',
    'bookkeeping-after-remove', 'late-bridge-after-remove', 'context-before-thinking', 'bookkeeping-before-thinking',
]


@pytest.mark.parametrize('fault', FAULTS)
def test_another_order_binding_or_boundary_leaves_the_queue_unknown(tmp_path, fault):
    assert_unknown(record(tmp_path, turn() + faulted(fault) + tail()))


def test_a_watch_already_bound_before_the_enqueue_does_not_take_the_late_path(tmp_path):
    """The call and its result written before the enqueue, then the same text row after it: not the measured order."""
    rows = late()
    prefix = [rows[index(rows, identity)] for identity in ('synthetic-watch-call', 'synthetic-watch-ack')]
    rest = [entry for entry in rows if entry not in prefix]
    assert_unknown(record(tmp_path, turn() + prefix + rest + tail()))


def test_the_late_metadata_stays_unmeasured_in_the_earlier_forms(tmp_path):
    from .test_claude_2331_watch_notice_absorption import armed, form, tail as earlier_tail
    for kind in ('history-suppression', 'bridge-session'):
        rows = form('R')
        rows.insert(len(rows) - 1, metadata(kind))
        assert_unknown(record(tmp_path / kind, armed() + rows + earlier_tail()))


WINDOW_ROWS = {
    'system-row': lambda: row('system', ENQUEUED + .1, subtype='informational', uuid='synthetic-info', content='x'),
    'turn-end': lambda: row('system', ENQUEUED + .1, subtype='turn_duration', uuid='synthetic-window-end'),
    'other-attachment': lambda: row('attachment', ENQUEUED + .1, uuid='synthetic-window-attachment',
                                    parentUuid='synthetic-watch-ack', attachment=dict(type='edited_text_file')),
    'sidechain-row': lambda: dict(row('assistant', ENQUEUED + .1, uuid='synthetic-window-child',
                                      parentUuid='synthetic-child-parent', madeBy='model', message=dict(
                                          role='assistant', stop_reason='tool_use',
                                          content=[dict(type='thinking', thinking='')])), isSidechain=True),
    'foreign-metadata': lambda: dict(type='last-prompt', sessionId='foreign-session'),
    'late-only-bookkeeping': lambda: metadata('history-suppression'),
    'unknown-kind': lambda: dict(type='synthetic-unmeasured', sessionId=SESSION),
}


@pytest.mark.parametrize('kind', sorted(WINDOW_ROWS))
@pytest.mark.parametrize('name', ['R', 'A-thinking', 'A-tool-use'])
def test_an_unmeasured_row_before_the_opening_row_leaves_the_queue_unknown(tmp_path, name, kind):
    """Between the enqueue and the row that opens the absorption only the measured bookkeeping may come."""
    from .test_claude_2331_watch_notice_absorption import armed, form, tail as earlier_tail
    rows = form(name)
    rows.insert(1, WINDOW_ROWS[kind]())
    assert_unknown(record(tmp_path, armed() + rows + earlier_tail()))


@pytest.mark.parametrize('name', ['R', 'A-thinking', 'A-tool-use'])
def test_the_measured_bookkeeping_before_the_opening_row_still_settles(tmp_path, name):
    from .test_claude_2331_watch_notice_absorption import armed, form, tail as earlier_tail
    rows = form(name)
    rows[1:1] = [dict(type='file-history-delta', sessionId=SESSION), metadata('last-prompt'),
                 row('attachment', ENQUEUED + .1, uuid='synthetic-window-deferred', parentUuid='synthetic-watch-ack',
                     attachment=dict(type='deferred_tools_record'))]
    path = record(tmp_path, armed() + rows + earlier_tail())
    reader = claude._Reader(path)
    assert reader.state().queue_error is None
    assert [task.terminal for task in reader.background.task_states()] == ['completed']


def timed(ms):
    """The enqueue's time moved by whole milliseconds, in the stamp's own form."""
    from .test_claude_2331_watch_notice_absorption import AT
    return (AT + timedelta(seconds=ENQUEUED, milliseconds=ms)).isoformat()


def retimed(rows, row_ms, embedded_ms=None):
    """The attachment's row time and its own timestamp, each relative to the enqueue; the same unless given."""
    rows[-1]['timestamp'] = timed(row_ms)
    rows[-1]['attachment']['timestamp'] = timed(row_ms if embedded_ms is None else embedded_ms)
    return rows


def test_the_late_bound_attachment_settles_one_millisecond_before_its_enqueue(tmp_path):
    """As measured: the attachment and its own timestamp both 1 ms before the enqueue."""
    path = record(tmp_path, turn() + retimed(late(), -1) + tail())
    reader = claude._Reader(path)
    state = reader.state()
    assert state.queue_error is None and not reader.task_queue.unknown and reader.task_queue.pending is None
    assert [(task.call, task.terminal) for task in reader.background.task_states()] == [(WATCH_CALL, 'completed')]
    cold_state, cold_turns = claude.read(record(tmp_path / 'cold', turn() + retimed(late(), -1) + tail()))
    warm_state, warm_turns = claude.read(path)
    assert replace(warm_state, last_write=cold_state.last_write) == cold_state and warm_turns == cold_turns


ATTACHMENT_TIMES = {
    'two-ms-before': (-2, None),
    'one-ms-after': (1, None),
    'row-early-own-at-enqueue': (-1, 0),
    'row-at-enqueue-own-early': (0, -1),
    'row-early-own-two-ms-early': (-1, -2),
}


@pytest.mark.parametrize('case', sorted(ATTACHMENT_TIMES))
def test_the_late_bound_attachment_outside_the_millisecond_or_disagreeing_stays_unknown(tmp_path, case):
    assert_unknown(record(tmp_path, turn() + retimed(late(), *ATTACHMENT_TIMES[case]) + tail()))


@pytest.mark.parametrize('value', ['later', '', None, 0])
def test_a_malformed_attachment_time_stays_unknown_in_the_late_bound_form(tmp_path, value):
    rows = late()
    rows[-1]['attachment']['timestamp'] = value
    assert_unknown(record(tmp_path / 'own', turn() + rows + tail()))
    rows = late()
    rows[-1]['timestamp'] = value
    assert_unknown(record(tmp_path / 'row', turn() + rows + tail()))


@pytest.mark.parametrize('name', ['R', 'R-result-only', 'A-thinking', 'A-tool-use'])
def test_the_earlier_forms_keep_the_enqueue_time_exactly(tmp_path, name):
    """The millisecond is the late-bound form's alone; elsewhere the row and its own time must both be the enqueue's."""
    from .test_claude_2331_watch_notice_absorption import armed, form, tail as earlier_tail
    path = record(tmp_path / 'exact', armed() + retimed(form(name), 0) + earlier_tail())
    assert claude.state(path).queue_error is None
    for row_ms, embedded_ms in ((-1, None), (0, -1), (-1, 0)):
        rows = retimed(form(name), row_ms, embedded_ms)
        assert_unknown(record(tmp_path / f'{row_ms}-{embedded_ms}', armed() + rows + earlier_tail()))
