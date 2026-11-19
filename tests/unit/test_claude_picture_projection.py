"""Made payloads exercise the bounded reader and its native consumers."""

from hashlib import sha256
import json

import pytest

from quarterdeck.harness import claude, claude_record, get, json_record
from quarterdeck.harness.compactions import identity
from tests.compaction_shapes import Native


def image(size):
    return {'source': {'data': 'QUJD' * (size // 4), 'media_type': 'image/png', 'type': 'base64'},
            'type': 'image'}


def text(value):
    return {'type': 'text', 'text': value}


def result(value, *, error=False):
    return {'content': value, 'tool_use_id': 'toolu_synthetic', 'is_error': error, 'type': 'tool_result'}


def begin_read(record):
    record.add('assistant', version='2.1.325', message=dict(
        role='assistant', id='msg_synthetic', model='synthetic', stop_reason='tool_use',
        usage=dict(input_tokens=11, output_tokens=3, cache_read_input_tokens=7, cache_creation_input_tokens=5),
        content=[dict(type='tool_use', id='toolu_synthetic', name='Read', input=dict(file_path='synthetic.png'))]))


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
        begin_read(record)
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
