"""Made payloads exercise the bounded reader and its native consumers."""

from hashlib import sha256
import json

import pytest

from quarterdeck import input_receipt
from quarterdeck.harness import claude, claude_record, get, json_record
from quarterdeck.harness.compactions import identity
from tests.compaction_shapes import AT, Native


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
def test_payload_projection_matches_the_full_native_readers(tmp_path, monkeypatch, use):
    record = Native(tmp_path / 'synthetic.jsonl', 'claude')
    size = 1024 * 1024
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


@pytest.mark.parametrize('tool', ['Read', 'Bash error', 'Agent'])
def test_duplicate_mirror_is_compatible_with_native_evidence(tmp_path, monkeypatch, tool):
    path = tmp_path / 'synthetic-conversation.jsonl'
    record = Native(path, 'claude')
    size = 1024 * 1024
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
    assert caught.value.row_refusal == claude_record.RowRefusal(('toolUseResult', key, '?'), len(large))


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
    assert caught.value.row_refusal == claude_record.RowRefusal(('toolUseResult', '?', 'text'), len(value))
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
    assert caught.value.row_refusal == claude_record.RowRefusal(('toolUseResult', 'file', 'base64'), claude_record.ROW_BYTES)


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


@pytest.mark.parametrize('reason', ['watch', 'escaped-watch', 'watch-sibling', 'background',
                                     'role', 'type', 'source', 'data', 'duplicate', 'partial',
                                     'unknown-block', 'unknown-mirror', 'typed-text'])
def test_giant_unproved_or_required_values_remain_unknown(tmp_path, monkeypatch, reason):
    record = Native(tmp_path / 'synthetic.jsonl', 'claude')
    monkeypatch.setattr(claude_record, 'COMPATIBILITY_BOUND', 1)
    big = claude_record.ROW_BYTES + 4
    picture = image(big)
    padding = 'x' * big
    watched = 'qd > synthetic marker\n' + padding
    content = [text('Synthetic typed input'), picture]
    message = {'role': 'user', 'content': content}
    row = {'type': 'user', 'message': message}
    if reason in ('watch', 'escaped-watch'):
        message['content'] = [result(watched)]
    elif reason == 'watch-sibling':
        message['content'] = [result([text(padding), text('qd > synthetic marker')])]
    elif reason == 'background':
        message['content'] = [result('Command running in background with ID: synthetic. ' + padding)]
    elif reason == 'role':
        message['role'] = 'assistant'
    elif reason == 'type':
        row['type'] = 'system'
    elif reason == 'source':
        picture['source']['type'] = 'unknown'
    elif reason == 'data':
        picture['source']['data'] += '%'
    elif reason == 'unknown-block':
        content.append({'type': 'unrecognized'})
    elif reason == 'unknown-mirror':
        row['toolUseResult'] = {'unproved_field': padding}
    elif reason == 'typed-text':
        content[0]['text'] = padding
    raw = json.dumps(row).encode()
    if reason == 'escaped-watch':
        raw = raw.replace(b'qd >', b'q\\u0064 >', 1)
    elif reason == 'duplicate':
        raw = b'{"message": {}, ' + raw[1:]
    elif reason == 'partial':
        raw = raw[:raw.index(b'QUJD') + big - 8]
    with record.path.open('ab') as stream:
        stream.write(raw + (b'' if reason == 'partial' else b'\n'))
    with pytest.raises(claude_record.ProjectionLimit) as refused:
        claude_record.parse(record.path)
    assert refused.value.resource == 'retained bytes per row'
    assert record.path not in json_record._snapshots


def test_typed_identity_uses_the_same_guard_after_reading_image_bytes(tmp_path, monkeypatch):
    record = Native(tmp_path / 'synthetic.jsonl', 'claude')
    row = record.add('user', message=dict(role='user', content=[image(1024 * 1024), text('Synthetic input')]))
    record.finish()
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
    assert reread_offsets.count(reread_offsets[0]) == 2
    assert observation.turn == identity(row)
    assert observation.digest == sha256(record.path.read_bytes()).hexdigest()
