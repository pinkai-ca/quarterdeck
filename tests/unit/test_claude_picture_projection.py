"""Made payloads exercise the bounded reader and its native consumers."""

from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
import json

import pytest

from quarterdeck import input_receipt
from quarterdeck.harness import claude, claude_record, get, json_record
from quarterdeck.harness.compactions import identity
from tests.compaction_shapes import AT, Native


PAYLOAD_BYTES = 32 * 1024


@pytest.fixture(autouse=True)
def small_projection_bounds(monkeypatch):
    monkeypatch.setattr(claude_record, 'CHUNK', 1024)
    monkeypatch.setattr(claude_record, 'ROW_BYTES', 8 * 1024)


def image(size):
    return {'source': {'data': 'QUJD' * (size // 4), 'media_type': 'image/png', 'type': 'base64'},
            'type': 'image'}


def text(value):
    return {'type': 'text', 'text': value}


def result(value, *, error=False):
    return {'content': value, 'tool_use_id': 'toolu_synthetic', 'is_error': error, 'type': 'tool_result'}


def begin_tool(record, name='Read', args=None, *, version='2.1.325', call='toolu_synthetic'):
    record.add('assistant', version=version, message=dict(
        role='assistant', id='msg_' + call, model='synthetic', stop_reason='tool_use',
        usage=dict(input_tokens=11, output_tokens=3, cache_read_input_tokens=7, cache_creation_input_tokens=5),
        content=[dict(type='tool_use', id=call, name=name, input=args if args is not None else {'file_path': 'synthetic.png'})]))


def read_mirror(picture):
    data = picture['source']['data']
    return {'type': 'image', 'file': {'base64': data, 'type': 'image/png', 'originalSize': len(data) // 4 * 3}}


@pytest.mark.parametrize('use', ['typed', 'image-result', 'string-result', 'text-block-result'])
@pytest.mark.parametrize('splitlines', [False, True], ids=['newline', 'splitlines'])
def test_payload_projection_matches_the_full_native_readers(tmp_path, monkeypatch, use, splitlines):
    record = Native(tmp_path / 'synthetic.jsonl', 'claude')
    size = PAYLOAD_BYTES
    body = 'escaped "quotes", \\ and plain text\n' * (size // 25)
    receipt = 'Synthetic receipt Ω\n\nSecond block'
    if use == 'typed':
        row = record.add('user', version='2.1.325', message={
            'content': [text('Synthetic receipt Ω'), image(size), text('\nSecond block')], 'role': 'user'},
            snapshot={'separate': 'ordinary metadata'})
        index = 1
    else:
        begin_tool(record)
        value = {'image-result': [text('Picture read'), image(size)],
                 'string-result': body, 'text-block-result': [text('Small heading'), text(body)]}[use]
        row = record.add('user', version='2.1.325', message={
            'content': [result(value, error=use == 'string-result')], 'role': 'user'},
            toolUseResult={'type': 'ordinary', 'size': size})
        index = 2
    record.finish()
    source = record.path.read_bytes()
    if splitlines:
        source = source.replace(b'\n', '\u2028'.encode(), 1)
        record.path.write_bytes(source)
    adapter = get('claude')

    def consumers():
        return (adapter.read(record.path), adapter.inputs(record.path), adapter.attribution_inputs(record.path),
                adapter.person_inputs(record.path, ()), adapter.compactions(record.path))

    with monkeypatch.context() as full:
        full.setattr(claude_record, 'PROJECTED_BYTES', 1)
        ordinary = consumers()
    json_record.release(record.path)
    streamed = consumers()
    assert streamed == ordinary
    state, turns = streamed[0]
    assert state.turn_open is False and state.pending_tools == ()
    snapshot = claude_record.parse(record.path)
    assert snapshot.raw_digest == sha256(source).hexdigest()
    assert snapshot.projected_bytes < 4096
    assert not snapshot.skipped
    projected = snapshot.rows[index][1]
    if use == 'typed':
        assert [item.text for item in streamed[1]][-1] == receipt
        assert turns[-1].input == receipt
        assert streamed[-1].turn == identity(row)
        assert projected['message']['content'][1]['source']['data'].size == size
    else:
        assert turns[0].tokens.read_cached == 7
        assert turns[0].tools[-1].name == 'Read'
        assert projected['toolUseResult'] == row['toolUseResult']
        block = projected['message']['content'][0]
        assert (block['type'], block['tool_use_id'], block['is_error']) == (
            'tool_result', 'toolu_synthetic', use == 'string-result')
        content = block['content']
        payload = (content[1]['source']['data'] if use == 'image-result'
                   else content if use == 'string-result' else content[1]['text'])
        assert payload.size == (size if use == 'image-result' else len(body.encode()))


@pytest.mark.parametrize('layout', ['newline', 'splitlines'])
@pytest.mark.parametrize('carrier', [None, 'tool_result', 'mcp_tool_result', 'web_fetch_tool_result'])
def test_assistant_images_preserve_native_evidence(tmp_path, monkeypatch, layout, carrier):
    path = tmp_path / 'synthetic.jsonl'
    record = Native(path, 'claude', path.stem)
    size = PAYLOAD_BYTES
    long_text = 'Ω "quote" and \\ with a newline\n' * 32
    reply = 'Synthetic assistant reply Ω'
    bash = dict(type='tool_use', id='toolu_bash', name='Bash',
                input=dict(command='synthetic command', run_in_background=True))
    agent = dict(type='tool_use', id='toolu_agent', name='Agent',
                 input=dict(description='synthetic agent', prompt='synthetic prompt'))
    picture = image(size)
    block = picture if carrier is None else dict(result([text(long_text), picture]), type=carrier)
    content = [text(reply), block, bash, agent]
    if carrier is None:
        retained = image(2 * 1024)
        retained['source']['media_type'] = 'synthetic-other-format'
        content.append(retained)
    row = record.add('assistant', version='2.1.325', context={'value': 'exact'},
                     toolUseResult={'note': long_text},
                     message=dict(content=content, role='assistant', model='synthetic',
                                  id='msg_synthetic', stop_reason='tool_use',
                                  usage=dict(input_tokens=11, output_tokens=3, cache_read_input_tokens=7,
                                             cache_creation_input_tokens=5)))
    for call, mirror in (('toolu_bash', dict(backgroundTaskId='synthetic-bash')),
                         ('toolu_agent', dict(agentId='synthetic-agent', isAsync=True, status='async_launched'))):
        record.add('user', version='2.1.325', toolUseResult=mirror,
                   message=dict(role='user', content=[dict(result('Synthetic start'), tool_use_id=call)]))
    begin_tool(record, 'Bash', {'command': 'qd inbox --wait'}, call='toolu_watch')
    watch = 'qd > synthetic watch envelope'
    record.add('user', version='2.1.325', message=dict(
        role='user', content=[dict(result(watch), tool_use_id='toolu_watch')]))
    record.finish()
    record.compact()
    child = path.with_suffix('') / 'subagents' / 'agent-synthetic-agent.jsonl'
    child.parent.mkdir(parents=True)
    child_row = dict(type='assistant', version='2.1.325', timestamp=(AT + timedelta(seconds=20)).isoformat(),
                     sessionId=path.stem, agentId='synthetic-agent', isSidechain=True,
                     message=dict(role='assistant', stop_reason='tool_use', content=[
                         block, dict(type='tool_use', name='Read', id='toolu_child', input={'path': 'synthetic'})]))
    child.write_text(json.dumps({'type': 'file-history-snapshot'}) + '\n' + json.dumps(child_row) + '\n')
    if layout == 'splitlines':
        for source in (path, child):
            source.write_bytes(source.read_bytes().replace(b'\n', '\u2028'.encode(), 1))

    adapter = get('claude')

    def observations():
        return (adapter.read(path), adapter.inputs(path), adapter.attribution_inputs(path),
                adapter.person_inputs(path, ()), adapter.compactions(path),
                input_receipt.confirmed(adapter, path, 'Synthetic task', AT),
                input_receipt.confirmed(adapter, path, 'Synthetic TASK', AT))

    with monkeypatch.context() as full:
        full.setattr(claude_record, 'PROJECTED_BYTES', 1)
        ordinary = observations()
    for source in (path, child):
        json_record.release(source)
    streamed = observations()
    assert streamed == ordinary
    (state, turns), _, attribution, _, compaction, receipt, edited = streamed
    assert not state.turn_open
    assert {tool.name for tool in state.active_tools} == {'Agent', 'Bash', 'Read'}
    assert turns[0].reply.startswith(reply) and turns[0].tokens.read_cached == 14
    assert [tool.name for tool in turns[0].tools] == ['Bash', 'Agent', 'Bash']
    assert any(item.kind == 'watch' and item.text == watch for item in attribution)
    assert (receipt, edited) == ('delivered', None)
    assert (compaction.count, compaction.last_call, compaction.reason) == (1, 'toolu_watch', None)
    snapshot = claude_record.parse(path)
    assert snapshot.raw_digest == sha256(path.read_bytes()).hexdigest()
    kept = snapshot.rows[1][1]
    expected = deepcopy(row)
    for value, raw in ((kept, expected), (claude_record.parse(child).rows[1][1], deepcopy(child_row))):
        index = 1 if value is kept else 0
        original = raw['message']['content'][index]
        projected = value['message']['content'][index]
        if carrier is not None:
            original, projected = original['content'][1], projected['content'][1]
        payload = projected['source']['data']
        assert (payload.size, payload.image_bytes) == (size, size // 4 * 3)
        original['source']['data'] = ''
        assert value == raw


@pytest.mark.parametrize('case,reason,path_tail,shape', [
    ('row', 'not-a-user-row', ('source', 'data'), ('user', 'assistant', 'image', True)),
    ('role', 'not-a-user-row', ('source', 'data'), ('assistant', '?', 'image', True)),
    ('scalar', 'not-a-user-row', ('toolUseResult',), ('assistant', 'assistant', '?', False)),
    ('unknown-block', 'unknown-block-type', ('source', 'data'), ('assistant', 'assistant', '?', True)),
    ('unknown-result', 'unknown-block-type', ('source', 'data'), ('assistant', 'assistant', '?', True)),
    ('result-envelope', 'unknown-block-type', ('source', 'data'), ('assistant', 'assistant', 'tool_result', True)),
    ('result-text', 'unknown-block-type', ('content', 'text'), ('assistant', 'assistant', 'tool_result', True)),
    ('text', 'budget', ('content', 'text'), (None,) * 4),
    ('mirror', 'unknown-block-type', ('toolUseResult',), ('assistant', 'assistant', '?', True)),
    ('media', 'image-media-type', ('source', 'data'), (None,) * 4),
    ('base64', 'image-not-base64', ('source', 'data'), (None,) * 4),
    ('padding', 'image-padding', ('source', 'data'), (None,) * 4),
])
def test_assistant_projection_refuses_unverified_oversized_values(tmp_path, monkeypatch, case, reason, path_tail, shape):
    record = Native(tmp_path / 'synthetic.jsonl', 'claude')
    monkeypatch.setattr(claude_record, 'COMPATIBILITY_BOUND', 1)
    size = PAYLOAD_BYTES
    picture = image(size)
    row = {'type': 'assistant', 'message': {'role': 'assistant', 'content': [picture]}}
    if case == 'row':
        row['type'] = 'user'
    elif case == 'role':
        row['message']['role'] = 'private-message-role'
    elif case in ('scalar', 'mirror'):
        row['toolUseResult'] = 'x' * size
        if case == 'scalar':
            row['message']['content'] = 'Synthetic scalar content'
    elif case == 'unknown-block':
        picture['type'] = 'private-block'
    elif case == 'unknown-result':
        row['message']['content'] = [dict(result([picture]), type='private_tool_result')]
    elif case == 'result-envelope':
        row['message']['content'] = [dict(result([picture]), tool_use_id='')]
    elif case == 'result-text':
        row['message']['content'] = [result([text('x' * size), picture])]
    elif case == 'text':
        row['message']['content'].append(text('x' * size))
    elif case == 'media':
        picture['source']['media_type'] = 'private-image-compression'
    elif case == 'base64':
        picture['source']['data'] = '!' + picture['source']['data'][1:]
    elif case == 'padding':
        picture['source']['data'] += '='
    with record.path.open('a') as stream:
        stream.write(json.dumps(row) + '\n')
    with pytest.raises(claude_record.ProjectionLimit) as rejected:
        claude_record.parse(record.path)
    detail = rejected.value.row_refusal
    assert detail.reason == reason
    assert detail.path[-len(path_tail):] == path_tail
    assert detail.size >= size
    assert (detail.row_type, detail.message_role, detail.block_type, detail.content_is_list) == shape


@pytest.mark.parametrize('layout', ['newline', 'splitlines'])
@pytest.mark.parametrize('row_type,role', [(None, 'user'), ('future_result', 'user'), ('future_result', 'assistant')])
def test_non_native_row_projects_only_verified_message_image(tmp_path, monkeypatch, layout, row_type, role):
    native = Native(tmp_path / 'synthetic.jsonl', 'claude')
    begin_tool(native, 'Bash', {'command': 'qd inbox --wait'}, call='toolu_watch')
    watch = 'qd > synthetic watch envelope\n' + 'Exact Ω text with a backslash \\ and newline\n' * 32
    picture = image(PAYLOAD_BYTES)
    carrier = dict(result([text('Exact sibling'), picture]),
                   type='tool_result' if role == 'user' else 'mcp_tool_result')
    content = [carrier, dict(result(watch), tool_use_id='toolu_watch')]
    # A mirror string is exact evidence outside native user rows, even when it looks like base64.
    row = native.add(row_type, message=dict(role=role, content=content),
                     toolUseResult=dict(data='QUJD' * 512, status='synthetic'))
    if row_type is None:
        del row['type']
    native.finish()
    path = native.path
    if layout == 'splitlines':
        path.write_bytes(path.read_bytes().replace(b'\n', '\u2028'.encode(), 1))
    adapter = get('claude')

    def observations():
        return (adapter.read(path), adapter.inputs(path), adapter.attribution_inputs(path),
                adapter.person_inputs(path, ()), adapter.compactions(path),
                input_receipt.confirmed(adapter, path, 'Synthetic task', AT))

    with monkeypatch.context() as full:
        full.setattr(claude_record, 'PROJECTED_BYTES', 1)
        expected = observations()
        assert claude_record.projection_usage(claude_record.parse(path)).compatibility
    json_record.release(path)
    monkeypatch.setattr(claude_record, 'COMPATIBILITY_BOUND', 1)
    assert observations() == expected
    assert any(item.kind == 'watch' and item.text == watch.strip() for item in expected[2]) == (role == 'user')
    assert expected[4].reason == 'native compaction history unreadable or incomplete'
    assert expected[5] == 'delivered'
    snapshot = claude_record.parse(path)
    kept = snapshot.rows[2][1]
    payload = kept['message']['content'][0]['content'][1]['source']['data']
    assert (payload.size, payload.image_bytes) == (PAYLOAD_BYTES, PAYLOAD_BYTES // 4 * 3)
    assert not isinstance(kept, claude_record.ProjectedRow)
    expected_row = deepcopy(row)
    expected_row['message']['content'][0]['content'][1]['source']['data'] = ''
    assert kept == expected_row
    assert snapshot.raw_digest == sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize('row_type', ['system', 'queue-operation'])
def test_non_native_work_rows_keep_the_exact_compaction_frontier(tmp_path, monkeypatch, row_type):
    native = Native(tmp_path / 'synthetic.jsonl', 'claude')
    native.finish()
    fields = (dict(subtype='compact_boundary', compactMetadata={'trigger': 'auto'}) if row_type == 'system'
              else dict(operation='enqueue', content='Exact queued input'))
    row = native.add(row_type, message=dict(role='user', content=[image(PAYLOAD_BYTES)]), **fields)
    with monkeypatch.context() as full:
        full.setattr(claude_record, 'PROJECTED_BYTES', 1)
        expected = claude.compactions(native.path)
    assert expected.reason is None
    json_record.release(native.path)
    monkeypatch.setattr(claude_record, 'COMPATIBILITY_BOUND', 1)
    assert claude.compactions(native.path) == expected
    assert claude_record.parse(native.path).rows[-1][1].identity == identity(row)


@pytest.mark.parametrize('field,row_type,label', [
    ('absent', None, 'missing'), ('present', None, '?'), ('present', 'future_result', 'future_result'),
    ('present', 'a' + '_' * 31, 'a' + '_' * 31), ('present', 'a' * 33, '?'),
    ('present', 'Mixed-case', '?'), ('present', 'future_result\n', '?'), ('present', 'type2', '?'),
])
def test_non_native_row_refusal_bounds_its_type_label(tmp_path, monkeypatch, field, row_type, label):
    monkeypatch.setattr(claude_record, 'COMPATIBILITY_BOUND', 1)
    monkeypatch.setattr(claude_record, 'ROW_BYTES', 4096)
    monkeypatch.setattr(claude_record, 'CHUNK', 512)
    native = Native(tmp_path / 'synthetic.jsonl', 'claude')
    row = native.add(row_type, message=dict(role='user', content=[result([image(8192), text('x' * 8192)])]))
    if field == 'absent':
        del row['type']
        native.flush()
    with pytest.raises(claude_record.ProjectionLimit) as rejected:
        claude_record.parse(native.path)
    assert rejected.value.row_refusal == claude_record.RowRefusal(
        ('message', 'content', 'content', 'text'), 8192, 'newline', 'unknown-block-type', 'tool_result',
        label, 'user', 'tool_result', True)


@pytest.mark.parametrize('tool', ['Read', 'Bash error', 'Agent'])
def test_duplicate_mirror_is_compatible_with_native_evidence(tmp_path, monkeypatch, tool):
    path = tmp_path / 'synthetic-conversation.jsonl'
    record = Native(path, 'claude')
    size = PAYLOAD_BYTES
    large = 'line with quotes " and a backslash \\ and newline\n' * (size // 40)
    version = '2.1.317' if tool == 'Bash error' else '2.1.325'
    if tool == 'Read':
        begin_tool(record)
        picture = image(size)
        value, mirror = [picture], read_mirror(picture)
    elif tool == 'Bash error':
        begin_tool(record, 'Bash', {'command': 'generated', 'run_in_background': True}, version=version)
        value = mirror = large
    else:
        begin_tool(record, 'Agent', {'description': 'synthetic', 'prompt': 'synthetic'})
        value = 'Synthetic agent launched'
        mirror = {'agentId': 'synthetic-agent', 'isAsync': True, 'status': 'async_launched',
                  'result': [{'arbitrary-mirror-key': large, 'size': 17}], 'prompt': 'small inline string'}
        child = path.with_suffix('') / 'subagents' / 'agent-synthetic-agent.jsonl'
        child.parent.mkdir(parents=True)
        child.write_text(json.dumps({
            'type': 'user', 'version': version, 'sessionId': path.stem, 'agentId': 'synthetic-agent',
            'isSidechain': True, 'timestamp': AT.isoformat(),
            'message': {'role': 'user', 'content': 'Synthetic child input'},
        }) + '\n')
    record.add('user', version=version, message={'role': 'user', 'content': [result(value, error=tool == 'Bash error')]},
               toolUseResult=mirror)
    begin_tool(record, 'Bash', {'command': 'qd inbox --wait'}, call='toolu_watch')
    watch = result('qd > synthetic watch envelope')
    watch['tool_use_id'] = 'toolu_watch'
    record.add('user', version='2.1.325', message={'role': 'user', 'content': [watch]})
    record.finish()
    record.compact()
    adapter = get('claude')

    def observations():
        return (adapter.read(path), adapter.inputs(path), adapter.attribution_inputs(path),
                adapter.person_inputs(path, ()), adapter.compactions(path),
                input_receipt.confirmed(adapter, path, 'Synthetic task', AT),
                input_receipt.confirmed(adapter, path, 'Synthetic TASK', AT))

    with monkeypatch.context() as baseline:
        baseline.setattr(claude_record, 'PROJECTED_BYTES', 1)
        ordinary = observations()
    json_record.release(path)
    streamed = observations()
    assert streamed == ordinary
    (state, turns), _, attributed, _, compaction, receipt, edited = streamed
    assert turns[0].tokens.read_cached == 14
    assert state.pending_tools == (('Agent',) if tool == 'Agent' else ())
    assert any(item.kind == 'watch' and item.text == 'qd > synthetic watch envelope' for item in attributed)
    assert compaction.count == 1 and compaction.reason is None
    assert (receipt, edited) == ('delivered', None)
    snapshot = claude_record.parse(path)
    assert snapshot.projected_bytes < 8192
    projected = snapshot.rows[2][1]['toolUseResult']
    if tool == 'Read':
        payload = projected['file']['base64']
        assert projected['file']['originalSize'] == payload.image_bytes == size // 4 * 3
        assert payload.size == size
    elif tool == 'Bash error':
        assert isinstance(projected, str) and projected.size == len(large.encode())
    else:
        assert projected['result'][0]['arbitrary-mirror-key'].size == len(large.encode())
        assert projected['result'][0]['size'] == 17
        assert all(projected[key] == mirror[key] for key in ('agentId', 'isAsync', 'status', 'prompt'))


@pytest.mark.parametrize('key', sorted(claude_record.MIRROR_EVIDENCE))
def test_mirror_evidence_keeps_wrong_types_or_refuses_at_the_original_field(tmp_path, monkeypatch, key):
    path = tmp_path / 'synthetic.jsonl'
    record = Native(path, 'claude')
    begin_tool(record)
    large = 'x' * (claude_record.ROW_BYTES + 10)
    mirror = {key: {'synthetic-key': 'small but wrong type'}, 'stdout': large}
    row = record.add('user', message={'role': 'user', 'content': [result('Synthetic result')]}, toolUseResult=mirror)
    monkeypatch.setattr(claude_record, 'COMPATIBILITY_BOUND', 1)
    assert claude_record.parse(path).rows[2][1]['toolUseResult'][key] == mirror[key]
    row['toolUseResult'][key]['synthetic-key'] = large
    record.flush()
    with pytest.raises(claude_record.ProjectionLimit) as caught:
        claude_record.parse(path)
    assert caught.value.row_refusal == claude_record.RowRefusal(('toolUseResult', key, '?'), len(large), 'newline', 'budget', '?')


@pytest.mark.parametrize('unsafe', ['qd > synthetic marker', '\\u0064', 'Command running in background with ID: '])
def test_mirror_text_with_watch_markers_or_unicode_escapes_refuses(tmp_path, monkeypatch, unsafe):
    record = Native(tmp_path / 'synthetic.jsonl', 'claude')
    begin_tool(record)
    value = 'x' * claude_record.ROW_BYTES + unsafe
    row = record.add('user', message={'role': 'user', 'content': [result('Small native result')]},
                     toolUseResult={'unrecognized-name': [{'text': value}]})
    # Use a real JSON Unicode escape in the specimen in that variant.
    if unsafe == '\\u0064':
        record.path.write_bytes(record.path.read_bytes().replace(b'\\\\u0064', b'\\u0064'))
    monkeypatch.setattr(claude_record, 'COMPATIBILITY_BOUND', 1)
    with pytest.raises(claude_record.ProjectionLimit) as caught:
        claude_record.parse(record.path)
    reason = 'text-escape' if unsafe == '\\u0064' else 'text-watch-marker'
    assert caught.value.row_refusal == claude_record.RowRefusal(
        ('toolUseResult', '?', 'text'), len(value), 'newline', reason, '?')
    assert row['toolUseResult']['unrecognized-name'][0]['text'] == value


def test_mirror_image_requires_valid_base64_even_with_a_valid_message_copy(tmp_path, monkeypatch):
    record = Native(tmp_path / 'synthetic.jsonl', 'claude')
    begin_tool(record)
    picture = image(claude_record.ROW_BYTES)
    mirror = read_mirror(picture)
    mirror['file']['base64'] = mirror['file']['base64'][:-1] + '%'
    record.add('user', message={'role': 'user', 'content': [result([picture])]}, toolUseResult=mirror)
    monkeypatch.setattr(claude_record, 'COMPATIBILITY_BOUND', 1)
    with pytest.raises(claude_record.ProjectionLimit) as caught:
        claude_record.parse(record.path)
    assert caught.value.row_refusal == claude_record.RowRefusal(
        ('toolUseResult', 'file', 'base64'), claude_record.ROW_BYTES, 'newline', 'image-not-base64', 'image')


def test_mirrored_bash_output_preserves_the_background_handle_and_exact_stop_message(tmp_path, monkeypatch):
    record = Native(tmp_path / 'synthetic.jsonl', 'claude')
    begin_tool(record, 'Bash', {'command': 'synthetic', 'run_in_background': True})
    large = 'x' * (claude_record.ROW_BYTES + 10)
    command = result('Synthetic running command')
    record.add('user', version='2.1.325', message={'role': 'user', 'content': [command]},
               toolUseResult={'backgroundTaskId': 'synthetic-bash', 'stdout': large})
    assert claude.state(record.path).pending_tools == ('Bash',)
    begin_tool(record, 'TaskStop', {'task_id': 'synthetic-bash'}, call='toolu_stop')
    stopped = result('Synthetic stop result')
    stopped['tool_use_id'] = 'toolu_stop'
    stop = {'message': 'Successfully stopped synthetic-bash.', 'task_type': 'local_bash',
            'task_id': 'synthetic-bash', 'details': [{'output': large}]}
    record.add('user', version='2.1.325', message={'role': 'user', 'content': [stopped]}, toolUseResult=stop)
    record.finish()
    streamed = get('claude').read(record.path)
    json_record.release(record.path)
    with monkeypatch.context() as ordinary:
        ordinary.setattr(claude_record, 'PROJECTED_BYTES', 1)
        assert get('claude').read(record.path) == streamed
    assert streamed[0].pending_tools == ()
    json_record.release(record.path)
    snapshot = claude_record.parse(record.path)
    projected = snapshot.rows[4][1]['toolUseResult']
    assert all(projected[key] == stop[key] for key in ('message', 'task_type', 'task_id'))
    assert projected['details'][0]['output'].size == len(large)


@pytest.mark.parametrize('layout', ['newline', 'splitlines'])
@pytest.mark.parametrize('case,reason,value_type', [
    ('watch', 'text-watch-marker', 'tool_result'),
    ('escaped-watch', 'text-escape', 'tool_result'),
    ('watch-sibling', 'text-watch-marker', 'text'),
    ('background', 'text-watch-marker', 'tool_result'),
    ('role', 'not-a-user-row', '?'),
    ('source', 'image-not-base64', 'image'),
    ('media', 'image-media-type', '?'),
    ('media-short', 'image-media-type', 'png'),
    ('data', 'image-not-base64', 'image/png'),
    ('padding', 'image-padding', 'image/png'),
    ('duplicate', 'duplicate-key', '?'),
    ('partial', None, None),
    ('unknown-block', 'unknown-block-type', '?'),
    ('other-block', 'unknown-block-type', 'tool_use'),
    ('unknown-mirror', 'unknown-block-type', '?'),
    ('typed-text', 'budget', '?'),
    ('many', 'too-many-payloads', '?'),
])
def test_giant_unproved_or_required_values_remain_unknown(tmp_path, monkeypatch, layout, case, reason, value_type):
    record = Native(tmp_path / 'synthetic.jsonl', 'claude')
    monkeypatch.setattr(claude_record, 'COMPATIBILITY_BOUND', 1)
    big = claude_record.ROW_BYTES + 4
    picture = image(big)
    padding = 'x' * big
    watched = 'qd > synthetic marker\n' + padding
    content = [text('Synthetic typed input'), picture]
    message = {'role': 'user', 'content': content}
    row = {'type': 'user', 'message': message}
    if case in ('watch', 'escaped-watch'):
        message['content'] = [result(watched)]
    elif case == 'watch-sibling':
        message['content'] = [result([text(padding), text('qd > synthetic marker')])]
    elif case == 'background':
        message['content'] = [result('Command running in background with ID: synthetic. ' + padding)]
    elif case == 'role':
        message['role'] = 'assistant'
    elif case == 'source':
        picture['source']['type'] = 'made-up-source'
    elif case in ('media', 'media-short'):
        picture['source']['media_type'] = 'made-up-media' if case == 'media' else 'png'
    elif case == 'data':
        picture['source']['data'] += '%'
    elif case == 'padding':
        picture['source']['data'] = 'QU=D' + picture['source']['data'][4:]
    elif case in ('unknown-block', 'other-block'):
        content.append({'type': 'made-up-block' if case == 'unknown-block' else 'tool_use'})
    elif case == 'unknown-mirror':
        row['toolUseResult'] = {'unproved_field': padding}
    elif case == 'typed-text':
        content[0]['text'] = padding
    elif case == 'many':
        monkeypatch.setattr(claude_record, 'MAX_PAYLOADS', 1)
        content.append(image(big))
    raw = json.dumps(row).encode()
    if case == 'escaped-watch':
        raw = raw.replace(b'qd >', b'q\\u0064 >', 1)
    elif case == 'duplicate':
        raw = b'{"message": {}, ' + raw[1:]
    elif case == 'partial':
        raw = raw[:raw.index(b'QUJD') + big - 8]
    prefix = record.path.read_bytes()
    if layout == 'splitlines':
        prefix = prefix.replace(b'\n', '\u2028'.encode(), 1)
    record.path.write_bytes(prefix + raw + (b'' if case == 'partial' else b'\n'))
    with pytest.raises(claude_record.ProjectionLimit) as refused:
        claude_record.parse(record.path)
    assert refused.value.resource == 'retained bytes per row'
    detail = refused.value.row_refusal
    if reason is None:
        assert detail is None
    else:
        assert (detail.layout, detail.reason, detail.value_type) == (layout, reason, value_type)
        assert detail.size >= big
        assert all(part == '?' or part in claude_record.SCHEMA_KEYS for part in detail.path)
    assert record.path not in json_record._snapshots


@pytest.mark.parametrize('layout', ['newline', 'splitlines'])
def test_typed_identity_uses_the_same_guard_after_reading_image_bytes(tmp_path, monkeypatch, layout):
    record = Native(tmp_path / 'synthetic.jsonl', 'claude')
    row = record.add('user', message=dict(role='user', content=[text('Synthetic Ω input'), image(PAYLOAD_BYTES)]))
    record.finish()
    raw = b'\n'.join(json.dumps(item, ensure_ascii=False).encode() for item in record.rows) + b'\n'
    if layout == 'splitlines':
        first, rest = raw.split(b'\n', 1)
        chunk = claude_record.CHUNK
        # Put a UTF-8 separator, CRLF, and a non-separator UTF-8 character across successive reads.
        prefix = first.ljust(chunk - 1) + '\u2028'.encode()
        prefix = prefix.ljust(2 * chunk - 1) + b'\r\n'
        omega = rest.index('Ω'.encode())
        raw = prefix + b' ' * (3 * chunk - 1 - len(prefix) - omega) + rest
    record.path.write_bytes(raw)
    image_start = raw.index(b'QUJD')
    pread = claude_record.os.pread
    reread_offsets = []

    def growing(descriptor, count, offset):
        reread_offsets.append(offset)
        if len(reread_offsets) == 1:
            with record.path.open('ab') as stream:
                stream.write(b'\n')
        return pread(descriptor, count, offset)

    monkeypatch.setattr(claude_record.os, 'pread', growing)
    observation = claude.compactions(record.path)
    assert reread_offsets[0] == image_start and reread_offsets.count(image_start) == 2
    assert observation.turn == identity(row)
    assert observation.digest == sha256(raw + b'\n').hexdigest()
    assert observation.extent == len(raw) + 1
