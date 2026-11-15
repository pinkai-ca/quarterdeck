"""Invented records using the measured completion/checkpoint schemas."""

import copy
import json

import pytest

from quarterdeck.harness import codex


AT = '2030-01-01T00:00:00+00:00'


def row(kind, payload):
    return {'timestamp': AT, 'type': kind, 'payload': payload}


def completion(item):
    return row('event_msg', {'type': 'item_completed', 'thread_id': 'thread',
                            'turn_id': 'turn', 'started_at_ms': 10,
                            'completed_at_ms': 20, 'item': item})


def variants():
    sleep = {'type': 'Extension', 'id': 'sleep', 'kind': 'clock.sleep', 'durationMs': 10}
    compaction = {'type': 'ContextCompaction', 'id': 'compaction'}
    result = dict(type='text_result', domain='example.invalid', ref_id='reference',
                  snippet='invented', thumbnail_url='', title='example', url='https://example.invalid')
    web = {'type': 'Extension', 'id': 'web', 'kind': 'web.search', 'query': 'example',
           'action': {'type': 'openPage', 'url': 'https://example.invalid'}, 'results': [result]}
    find = copy.deepcopy(web)
    find['action'] = dict(type='findInPage', url='https://example.invalid', pattern='example')
    return [completion(item) for item in (sleep, compaction, web, find)]


def checkpoint():
    payload = dict(compaction_response_id='response', first_window_id='first',
                   latest_token_usage_record={}, message='invented summary', previous_window_id='previous',
                   replacement_history=[], replacement_history_metadata=[], resume_metadata={},
                   retained_context={}, window_id='current', window_number=1)
    return dict(row('compacted', payload), ordinal=10)


METADATA = variants() + [checkpoint()]


def write(tmp_path, rows, *, newline=True):
    record = tmp_path / 'record.jsonl'
    record.write_text('\n'.join(json.dumps(item) for item in rows) + ('\n' if newline else ''))
    return record


def session():
    return row('session_meta', {'id': 'session'})


def call(name='example', kind='function_call'):
    payload = dict(type=kind, call_id='call', name=name, status='completed')
    payload['input' if kind == 'custom_tool_call' else 'arguments'] = '{}'
    return row('response_item', payload)


@pytest.mark.parametrize('metadata', METADATA)
def test_measured_metadata_preserves_readable_idle_and_outstanding_call(tmp_path, metadata):
    record = write(tmp_path, [session(), metadata])
    assert codex.state(record).pending_tools == ()
    record = write(tmp_path, [session(), call(), metadata])
    assert codex.state(record).pending_tools == ('example',)
    assert [tool.name for tool in codex.state(record).active_tools] == ['example']


@pytest.mark.parametrize('metadata', METADATA)
def test_metadata_never_restores_an_unknown_execution_set(tmp_path, metadata):
    unknown = row('response_item', {'type': 'future_item'})
    record = write(tmp_path, [session(), unknown, metadata])
    assert codex.state(record).pending_tools is None


@pytest.mark.parametrize('metadata', METADATA)
def test_unterminated_metadata_remains_unknown_until_newline(tmp_path, metadata):
    record = write(tmp_path, [session(), metadata], newline=False)
    assert codex.state(record).pending_tools is None
    with record.open('a') as stream:
        stream.write('\n')
    assert codex.state(record).pending_tools == ()


@pytest.mark.parametrize('metadata', METADATA)
@pytest.mark.parametrize('change', ['extra', 'wrong_type', 'empty_id'])
def test_unmeasured_metadata_remains_unknown(tmp_path, metadata, change):
    metadata = copy.deepcopy(metadata)
    target = metadata['payload'] if metadata['type'] == 'compacted' else metadata['payload']['item']
    if change == 'extra':
        target['future'] = True
    elif change == 'wrong_type':
        target['window_number' if metadata['type'] == 'compacted' else 'id'] = False
    else:
        target['window_id' if metadata['type'] == 'compacted' else 'id'] = ''
    assert codex.state(write(tmp_path, [session(), metadata])).pending_tools is None


@pytest.mark.parametrize('field,value', [('durationMs', -1), ('durationMs', True),
                                        ('kind', 'future.extension')])
def test_unmeasured_sleep_variants_are_unknown(tmp_path, field, value):
    metadata = variants()[0]
    metadata['payload']['item'][field] = value
    assert codex.state(write(tmp_path, [session(), metadata])).pending_tools is None


@pytest.mark.parametrize('target', ['action', 'result'])
def test_unmeasured_web_variants_are_unknown(tmp_path, target):
    metadata = variants()[2]
    item = metadata['payload']['item']
    (item['action'] if target == 'action' else item['results'][0])['type'] = 'future_variant'
    assert codex.state(write(tmp_path, [session(), metadata])).pending_tools is None


@pytest.mark.parametrize('output', ['Script completed\nWall time 1 seconds\nOutput:\n',
                                    'Script running with cell ID 1\nWall time 1 seconds\nOutput:\n'])
def test_exec_wrapper_output_and_turn_end_do_not_prove_descendants_ended(tmp_path, output):
    rows = [session(), row('event_msg', {'type': 'task_started'}),
            call('functions.exec', 'custom_tool_call'),
            row('response_item', {'type': 'custom_tool_call_output', 'call_id': 'call', 'output': output}),
            *METADATA, row('event_msg', {'type': 'task_complete'})]
    state = codex.state(write(tmp_path, rows))
    assert state.turn_open is False
    assert state.pending_tools is None


def test_checkpoint_does_not_replay_retained_inputs_or_tools(tmp_path):
    metadata = checkpoint()
    metadata['payload']['replacement_history'] = [call(), row('response_item', {
        'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'retained'}]})]
    first = row('response_item', {'type': 'message', 'role': 'user',
                                 'content': [{'type': 'input_text', 'text': 'first'}]})
    second = copy.deepcopy(first)
    second['payload']['content'][0]['text'] = 'second'
    record = write(tmp_path, [session(), first, metadata, second])
    inputs = codex.inputs(record)
    assert not inputs.skip_counts
    assert [item.text for item in inputs] == ['first', 'second']
    assert codex.state(record).pending_tools == ()


@pytest.mark.parametrize('mutation', ['future', 'partial', 'ordinal', 'timestamp'])
def test_unknown_checkpoint_is_also_unknown_input_history(tmp_path, mutation):
    metadata = checkpoint()
    if mutation == 'future':
        metadata['future'] = True
    elif mutation == 'ordinal':
        metadata['ordinal'] = True
    elif mutation == 'timestamp':
        metadata['timestamp'] = 'unreadable'
    record = write(tmp_path, [session(), metadata], newline=mutation != 'partial')
    assert codex.inputs(record).skip_counts
    assert codex.state(record).pending_tools is None
