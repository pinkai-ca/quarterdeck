"""Synthetic identities/text in shapes measured on an isolated 2.1.325 session."""

from copy import deepcopy
import json

import pytest

from quarterdeck import idle
from quarterdeck.harness import claude
from quarterdeck.harness.json_record import release


def row(kind, second=0, **fields):
    return dict(type=kind, version='2.1.325', timestamp=f'2032-04-05T06:07:{second:02d}Z', **fields)


def call(identity, background=False):
    return dict(type='tool_use', id=identity, name='Bash',
                input=dict(command='synthetic command', run_in_background=background))


def result(identity, second, background=False):
    value = dict(stdout='', stderr='', interrupted=False, isImage=False, noOutputExpected=False)
    if background:
        value['backgroundTaskId'] = 'synthetic-task'
    return row('user', second, toolUseResult=value, message=dict(role='user', content=[dict(
        type='tool_result', tool_use_id=identity, content='synthetic result', is_error=False)]))


def notification():
    return ('<task-notification>\n<task-id>synthetic-task</task-id>\n'
            '<tool-use-id>synthetic-background</tool-use-id>\n'
            '<output-file>/synthetic/output</output-file>\n<status>completed</status>\n'
            '<summary>synthetic summary</summary>\n</task-notification>')


def initial(*, fragmented=False):
    calls = [call('synthetic-first'), call('synthetic-second'), call('synthetic-background', True)]
    messages = [[block] for block in calls] if fragmented else [calls]
    return [row('user', message=dict(role='user', content='synthetic prompt')),
            dict(type='ai-title', aiTitle='synthetic title', sessionId='synthetic-session'),
            *(row('assistant', 1, message=dict(id='synthetic-message', role='assistant', content=blocks))
              for blocks in messages)]


def finish():
    text = notification()
    return [result('synthetic-second', 2), result('synthetic-first', 3),
            result('synthetic-background', 4, True), row('system', 5, subtype='turn_duration'),
            # Queue carriers have no version; the preceding native row supplies it.
            dict(type='queue-operation', operation='enqueue', content=text, timestamp='2032-04-05T06:07:06Z'),
            dict(type='queue-operation', operation='dequeue', timestamp='2032-04-05T06:07:07Z'),
            row('user', 8, origin=dict(kind='task-notification', producer='session-task', runId='synthetic-run'),
                message=dict(role='user', content=text)),
            row('assistant', 9, message=dict(role='assistant', content=[dict(type='text', text='synthetic reply')])),
            row('system', 10, subtype='turn_duration')]


def write(record, rows, mode='w'):
    with record.open(mode) as stream:
        stream.writelines(json.dumps(entry) + '\n' for entry in rows)


@pytest.mark.parametrize('fragmented', [False, True], ids=['one-row', 'shared-api-message'])
def test_parallel_results_and_background_next_turn_end_idle(tmp_path, fragmented):
    record = tmp_path / 'record.jsonl'
    write(record, initial(fragmented=fragmented))
    assert claude.state(record).pending_tools == ('Bash', 'Bash', 'Bash')
    tail = finish()
    write(record, tail[:4], 'a')
    pending = claude.state(record)
    assert pending.turn_open is False and pending.pending_tools == ('Bash',)
    assert idle.check(pending).dimension == 'tools-pending'
    write(record, tail[4:6], 'a')
    queued = claude.state(record)
    assert queued.pending_tools == () and queued.queued == (notification(),)
    write(record, tail[6:], 'a')
    ended = claude.state(record)
    assert ended.turn_open is False and ended.pending_tools == () and ended.queued == ()
    assert len(ended.completed_turns) == 2 and idle.check(ended) is None
    assert [len(turn.tools) for turn in ended.completed_turns] == [3, 0]
    receipts = claude.inputs(record)
    assert [(entry.text, entry.kind) for entry in receipts] == [
        ('synthetic prompt', 'accepted'), (notification(), 'consumed')]
    assert not receipts.skipped
    release(record)
    assert claude.state(record) == ended


def test_blob_copies_before_and_after_native_calls_never_change_evidence(tmp_path):
    record = tmp_path / 'record.jsonl'
    start, tail = initial(), finish()
    copies = [dict(type='api-request-blob', message=deepcopy(entry['message']))
              for entry in [start[-1], *tail[:3]]]
    # Native results occur in a different order from the calls; copied results precede both.
    write(record, [*copies, *start, *copies])
    assert claude.state(record).pending_tools == ('Bash', 'Bash', 'Bash')
    write(record, [*tail, *copies], 'a')
    ended = claude.state(record)
    assert ended.pending_tools == () and idle.check(ended) is None
    assert [len(turn.tools) for turn in ended.completed_turns] == [3, 0]
    assert not claude.inputs(record).skipped


def test_copied_result_cannot_close_a_native_call(tmp_path):
    record = tmp_path / 'record.jsonl'
    write(record, [*initial(), dict(type='api-request-blob', message=result('synthetic-first', 2)['message']),
                   row('system', 3, subtype='turn_duration')])
    state = claude.state(record)
    assert state.turn_open is False and state.pending_tools == ('Bash', 'Bash', 'Bash')


@pytest.mark.parametrize('difference', ['missing-title', 'extra-key', 'wrong-title', 'empty-session'])
def test_only_the_measured_title_shape_is_known_even_after_cached_inputs(tmp_path, difference):
    record = tmp_path / 'record.jsonl'
    write(record, [*initial(), *finish()])
    assert not claude.inputs(record).skipped
    malformed = dict(type='ai-title', aiTitle='synthetic title', sessionId='synthetic-session')
    if difference == 'missing-title':
        del malformed['aiTitle']
    elif difference == 'extra-key':
        malformed['unmeasured'] = True
    elif difference == 'wrong-title':
        malformed['aiTitle'] = []
    else:
        malformed['sessionId'] = ''
    write(record, [malformed], 'a')
    assert claude.state(record).pending_tools is None
    assert claude.inputs(record).skipped[-1]['reason'] == 'unknown title bookkeeping'


@pytest.mark.parametrize('difference', ['version', 'producer', 'missing-run', 'empty-run', 'extra-key'])
def test_the_new_user_notification_origin_is_recognized_only_as_measured(difference):
    entry = finish()[6]
    assert claude.user_notice(entry)
    if difference == 'version':
        entry['version'] = '2.1.326'
    elif difference == 'producer':
        entry['origin']['producer'] = 'synthetic-other'
    elif difference == 'missing-run':
        del entry['origin']['runId']
    elif difference == 'empty-run':
        entry['origin']['runId'] = ''
    else:
        entry['origin']['unmeasured'] = True
    assert not claude.user_notice(entry)


def test_original_user_notification_origin_is_still_recognized():
    entry = finish()[6]
    entry['origin'] = {'kind': 'task-notification'}
    assert claude.user_notice(entry)


@pytest.mark.parametrize('position', [2, 5, 9], ids=['call', 'result', 'user-notice'])
def test_newer_unmeasured_execution_versions_remain_unknown(tmp_path, position):
    rows = [*initial(), *finish()]
    rows[position]['version'] = '2.1.326'
    record = tmp_path / 'record.jsonl'
    write(record, rows)
    assert claude.state(record).pending_tools is None


def test_an_unrelated_unknown_is_not_cleared_by_a_completed_background_task(tmp_path):
    record = tmp_path / 'record.jsonl'
    write(record, [*initial(), row('synthetic-unknown', 1), *finish()])
    assert claude.state(record).pending_tools is None
