"""Synthetic native completion metadata, separate from response-call lifetimes."""
from copy import deepcopy
import json

import pytest

from quarterdeck import idle
from quarterdeck.harness import codex


AT = '2025-01-02T03:04:05+00:00'
ITEMS = [
    {'type': 'UserMessage', 'id': 'native-example', 'client_id': 'client-example',
     'content': [{'type': 'text', 'text': 'example', 'text_elements': []}]},
    {'type': 'AgentMessage', 'id': 'native-example', 'phase': 'commentary',
     'content': [{'type': 'Text', 'text': 'example'}]},
    {'type': 'Reasoning', 'id': 'native-example', 'raw_content': [], 'summary_text': []},
    {'type': 'CommandExecution', 'id': 'native-example', 'command': ['example'], 'cwd': '/example',
     'duration': {'secs': 0, 'nanos': 1}, 'exit_code': 0, 'parsed_cmd': [],
     'process_id': 'process-example', 'source': 'example', 'status': 'completed',
     'aggregated_output': ''},
    {'type': 'FileChange', 'id': 'native-example', 'changes': {}, 'status': 'completed',
     'stdout': '', 'stderr': ''},
]


def row(kind, payload):
    return {'type': kind, 'timestamp': AT, 'payload': payload}


def completed(item):
    return row('event_msg', {'type': 'item_completed', 'thread_id': 'thread-example',
                           'turn_id': 'turn-example', 'started_at_ms': 10, 'completed_at_ms': 20,
                           'item': deepcopy(item)})


def history(*middle):
    return [row('session_meta', {}), row('event_msg', {'type': 'task_started'}),
            row('response_item', {'type': 'message', 'role': 'user',
                                  'content': [{'type': 'input_text', 'text': 'example question'}]}),
            *middle, row('event_msg', {'type': 'task_complete'})]


def write(tmp_path, rows):
    path = tmp_path / 'synthetic.jsonl'
    path.write_text(''.join(json.dumps(value) + '\n' for value in rows))
    return path


@pytest.mark.parametrize('item', ITEMS, ids=lambda item: item['type'])
def test_measured_completion_metadata_retains_idle_evidence(tmp_path, item):
    state = codex.state(write(tmp_path, history(completed(item))))
    assert state.pending_tools == ()
    assert state.turn_open is False
    assert idle.check(state) is None


def test_failed_command_with_exit_code_is_terminal_metadata(tmp_path):
    item = dict(ITEMS[3], status='failed', exit_code=1)
    assert codex.state(write(tmp_path, history(completed(item)))).pending_tools == ()


@pytest.mark.parametrize('item', ITEMS, ids=lambda item: item['type'])
def test_native_completion_never_closes_response_call(tmp_path, item):
    call = row('response_item', {'type': 'function_call', 'call_id': 'call-example',
                                 'name': 'plain_echo', 'arguments': '{}'})
    rows = history(call, completed(item))
    path = write(tmp_path, rows)
    state = codex.state(path)
    assert state.pending_tools == ('plain_echo',)
    assert idle.check(state).dimension == 'tools-pending'
    # Only the matching response output can retire that call, including on cached append.
    with path.open('a') as stream:
        stream.write(json.dumps(row('response_item', {'type': 'function_call_output',
                                'call_id': 'call-example', 'output': 'example result'})) + '\n')
    assert codex.state(path).pending_tools == ()


def invalid_metadata():
    unknown = completed({'type': 'FutureExecution', 'id': 'native-example'})
    yield 'future-item', unknown
    for field, value in [('status', 'in_progress'), ('status', 'future'), ('exit_code', None),
                         ('exit_code', True), ('process_id', ''), ('duration', {'secs': 0}),
                         ('duration', {'secs': -1, 'nanos': 0}),
                         ('duration', {'secs': 0, 'nanos': 1_000_000_000}), ('command', [])]:
        yield 'command-' + field + '-' + str(value), completed(dict(ITEMS[3], **{field: value}))
    for field, value in [('item', []), ('thread_id', ''), ('turn_id', None),
                         ('started_at_ms', True), ('completed_at_ms', 9)]:
        entry = completed(ITEMS[0])
        entry['payload'][field] = value
        yield 'envelope-' + field, entry
    entry = completed(ITEMS[0])
    del entry['payload']['completed_at_ms']
    yield 'incomplete-envelope', entry
    entry = completed(ITEMS[0])
    entry['payload']['item']['content'][0]['type'] = 'future-content'
    yield 'unknown-user-content', entry
    yield 'malformed-agent-content', completed(dict(ITEMS[1], content=[None]))
    yield 'unmeasured-reasoning', completed(dict(ITEMS[2], summary_text=['example']))
    yield 'file-progress', completed(dict(ITEMS[4], status='in_progress'))
    entry = completed(ITEMS[3])
    entry['payload']['item']['future_execution'] = {}
    yield 'future-item-field', entry
    entry = completed(ITEMS[0])
    entry['timestamp'] = 'unreadable'
    yield 'unreadable-timestamp', entry


@pytest.mark.parametrize('label,entry', list(invalid_metadata()), ids=lambda value: value if isinstance(value, str) else None)
def test_unrecognized_or_progress_shapes_stay_unknown(tmp_path, label, entry):
    state = codex.state(write(tmp_path, history(entry)))
    assert state.pending_tools is None
    assert idle.check(state).dimension == 'tools-unknown'


def test_known_completion_does_not_repair_unknown_history(tmp_path):
    state = codex.state(write(tmp_path, history(row('event_msg', {'type': 'future-execution'}),
                                               completed(ITEMS[3]))))
    assert state.pending_tools is None


def test_paired_opaque_wrapper_and_native_completion_still_unknown(tmp_path):
    call = row('response_item', {'type': 'custom_tool_call', 'call_id': 'wrapper-example',
                                 'name': 'functions.exec', 'input': 'void 0'})
    output = row('response_item', {'type': 'custom_tool_call_output', 'call_id': 'wrapper-example',
                                  'output': [{'type': 'text', 'text': 'Script completed'}]})
    state = codex.state(write(tmp_path, history(call, completed(ITEMS[3]), output)))
    assert state.pending_tools is None
    assert idle.check(state).dimension == 'tools-unknown'


def test_truncated_row_stays_unknown_and_unreadable_record_raises(tmp_path):
    path = write(tmp_path, history(completed(ITEMS[3])))
    with path.open('a') as stream:
        stream.write('{"type":')
    assert codex.state(path).pending_tools is None
    path.unlink()
    with pytest.raises(OSError):
        codex.state(path)


@pytest.mark.parametrize('cached', [False, True], ids=['initial-read', 'append'])
def test_unterminated_completion_needs_physical_line_end(tmp_path, cached):
    path = write(tmp_path, history())
    if cached:
        assert codex.state(path).pending_tools == ()
    with path.open('a') as stream:
        stream.write(json.dumps(completed(ITEMS[3])))
    assert codex.state(path).pending_tools is None
    with path.open('a') as stream:
        stream.write('\n')
    assert codex.state(path).pending_tools == ()
