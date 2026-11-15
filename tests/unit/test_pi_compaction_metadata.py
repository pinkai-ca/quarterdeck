"""Invented pi histories around the measured native compaction envelope."""

import copy
from dataclasses import asdict
import json

import pytest

from quarterdeck.harness import pi


AT = '2030-01-01T00:00:00+00:00'


def session():
    return dict(type='session', id='session', timestamp=AT, version=3, cwd='/synthetic')


def checkpoint():
    return dict(type='compaction', id='checkpoint', parentId='answer', timestamp=AT,
                summary='invented checkpoint summary', firstKeptEntryId='question',
                tokensBefore=100, fromHook=False,
                details=dict(readFiles=['/synthetic/example.py'], modifiedFiles=[]))


def user(identifier='question', parent=None, text='first prompt'):
    return dict(type='message', id=identifier, parentId=parent, timestamp=AT,
                message=dict(role='user', content=[dict(type='text', text=text)]))


def assistant(identifier='answer', parent='question', *, tool=False):
    return dict(type='message', id=identifier, parentId=parent, timestamp=AT,
                message=dict(role='assistant', model='synthetic', provider='synthetic',
                             content=[dict(type='toolCall', id='call', name='example', arguments={})]
                             if tool else [dict(type='text', text='answer')],
                             stopReason='toolUse' if tool else 'stop',
                             usage=dict(input=3, output=2, cacheRead=0, cacheWrite=0, totalTokens=5)))


def write(tmp_path, rows, *, newline=True, name='record.jsonl'):
    path = tmp_path / name
    path.write_text('\n'.join(json.dumps(row) for row in rows) + ('\n' if newline else ''))
    return path


def history():
    return [session(), user(), assistant()]


def test_valid_checkpoint_preserves_idle_and_complete_input_evidence(tmp_path):
    record = write(tmp_path, [*history(), checkpoint()])
    state = pi.state(record)
    assert state.turn_open is False and state.pending_tools == ()
    assert state.last_input == 'first prompt'
    inputs = pi.inputs(record)
    assert not inputs.skip_counts
    assert [item.text for item in inputs] == ['first prompt']


def test_compaction_does_not_end_a_turn_or_a_pending_tool(tmp_path):
    rows = [session(), user(), assistant(tool=True), checkpoint()]
    record = write(tmp_path, rows)
    state = pi.state(record)
    assert state.turn_open is True and state.pending_tools == ('example',)
    assert state.open_tool == 'example'
    assert not pi.turns(record)
    rows.append(user('steered', 'checkpoint', 'follow up'))
    write(tmp_path, rows)
    inputs = pi.inputs(record)
    assert not inputs.skip_counts
    assert [(item.text, item.kind) for item in inputs] == [
        ('first prompt', 'accepted'), ('follow up', 'consumed')]


def test_compaction_cannot_clear_previously_unknown_execution(tmp_path):
    unknown = dict(type='future_entry', id='future', parentId='answer', timestamp=AT)
    compacted = checkpoint()
    compacted['parentId'] = 'future'
    record = write(tmp_path, [*history(), unknown, compacted])
    assert pi.state(record).pending_tools is None
    assert sum(pi.inputs(record).skip_counts.values()) == 1


def test_partial_checkpoint_stays_unknown_until_its_newline(tmp_path):
    record = write(tmp_path, [*history(), checkpoint()], newline=False)
    assert pi.state(record).pending_tools is None
    assert pi.inputs(record).skip_counts
    with record.open('a') as stream:
        stream.write('\n')
    assert pi.state(record).pending_tools == ()
    assert not pi.inputs(record).skip_counts


def test_checkpoint_keeps_original_accounting_and_next_native_parent_chain(tmp_path):
    before = [*history(), user('next', 'answer', 'second prompt'), assistant('next-answer', 'next')]
    after = [*history(), checkpoint(), user('next', 'checkpoint', 'second prompt'), assistant('next-answer', 'next')]
    original = write(tmp_path, before, name='without.jsonl')
    compacted = write(tmp_path, after, name='with.jsonl')
    serialize = lambda turns: json.dumps([asdict(turn) for turn in turns], sort_keys=True,
                                         default=lambda value: value.isoformat()).encode()
    assert serialize(pi.turns(original)) == serialize(pi.turns(compacted))
    state = pi.state(compacted)
    assert state.conversation == 'session' and state.input_id == state.work_id == 'next'
    assert state.turns == 2 and state.pending_tools == ()
    assert [item.text for item in pi.inputs(compacted)] == ['first prompt', 'second prompt']


MUTATIONS = [
    ('extra', True), ('tokensBefore', -1), ('tokensBefore', True), ('fromHook', True),
    ('timestamp', 'unknown'), ('timestamp', '2030-01-01T00:00:00'),
    ('id', ''), ('parentId', None), ('firstKeptEntryId', ''), ('summary', ''),
    ('details', {}), ('readFiles', []), ('readFiles', [1]),
    ('modifiedFiles', ['invented.py']), ('modifiedFiles', {}), ('details-extra', True),
]


@pytest.mark.parametrize('field,value', MUTATIONS)
def test_unmeasured_checkpoint_variants_are_unknown_in_state_and_inputs(tmp_path, field, value):
    compacted = checkpoint()
    if field in ('readFiles', 'modifiedFiles'):
        compacted['details'][field] = value
    elif field == 'details-extra':
        compacted['details']['extra'] = value
    else:
        compacted[field] = value
    record = write(tmp_path, [*history(), compacted])
    assert pi.state(record).pending_tools is None
    assert sum(pi.inputs(record).skip_counts.values()) == 1


@pytest.mark.parametrize('field', list(checkpoint()))
def test_missing_checkpoint_fields_are_unknown(tmp_path, field):
    compacted = checkpoint()
    del compacted[field]
    record = write(tmp_path, [*history(), compacted])
    assert pi.state(record).pending_tools is None
    assert pi.inputs(record).skip_counts


def test_valid_append_preserves_inputs_and_a_future_append_is_not_cached_away(tmp_path):
    record = write(tmp_path, history())
    first = pi.inputs(record)
    with record.open('a') as stream:
        stream.write(json.dumps(checkpoint()) + '\n')
    assert pi.inputs(record) == first and not pi.inputs(record).skip_counts
    unknown = copy.deepcopy(checkpoint())
    unknown['details']['modifiedFiles'] = ['unmeasured.py']
    with record.open('a') as stream:
        stream.write(json.dumps(unknown) + '\n')
    assert pi.inputs(record).skip_counts
    assert pi.state(record).pending_tools is None
