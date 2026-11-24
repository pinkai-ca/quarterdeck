"""Synthetic Claude records with source-backed metadata and intact native IDs."""

import pytest

from quarterdeck.harness import claude, claude_record
from tests.compaction_shapes import Native


def metadata(native, kind, **fields):
    return dict(type=kind, v=1, sessionId=native.conversation, **fields)


def test_metadata_preserves_mixed_resume_and_manual_compaction(tmp_path):
    native = Native(tmp_path / 'record.jsonl', 'claude')
    native.finish()
    for row in native.rows:
        row['version'] = '2.1.317'
    native.flush()
    before = claude.compactions(native.path)
    assert before.reason is None and before.complete and before.count == 0
    old_inputs = claude.inputs(native.path)

    native.rows.extend([
        metadata(native, 'derived-from', sessions=[
            dict(type='claude_code_session', id=native.conversation, via='resume'),
        ]),
        metadata(native, 'data-flow-incomplete', codes=[9, 4]),
        metadata(native, 'data-flow-records', records=[], snapshot=True),
        metadata(native, 'data-flow-records', records=[
            dict(response_id='msg_SyntheticNew', mark_sent_by='synthetic-session', listed=['msg_SyntheticOld']),
        ]),
    ])
    native.flush()
    after = claude.compactions(native.path)
    assert after.conversation == before.conversation
    assert after.complete and after.frontier == before.frontier and after.turn == before.turn
    assert after.matches_prefix(before.extent, before.digest)
    assert claude.inputs(native.path) == old_inputs

    native.begin('New prompt after resume')
    native.finish()
    native.compact()
    native.rows[-1]['compactMetadata'] = dict(trigger='manual', preTokens=100, messagesSummarized=4)
    native.add('user', isCompactSummary=True, message=dict(role='user', content='Synthetic summary'))
    native.rows.extend([
        metadata(native, 'derived-from', sessions=[
            dict(type='claude_code_session', id=native.conversation, via='resume'),
            dict(type='claude_code_session', id=native.conversation, via='compact'),
        ]),
        metadata(native, 'data-flow-records', snapshot=True, records=[
            dict(tool_name='Bash', tool_use_id='toolu_Synthetic1'),
            dict(tool_name='Agent', tool_use_id='toolu_Synthetic2'),
            dict(tool_name='TaskStop', tool_use_id='toolu_Synthetic3'),
            dict(response_id='msg_SyntheticNew', mark_sent_by=native.conversation),
        ]),
    ])
    for row in native.rows:
        if 'uuid' in row:
            row.setdefault('version', '2.1.331')
    native.flush()

    history = claude.compactions(native.path)
    found = claude.inputs(native.path)
    assert history.reason is None and history.count == 1 and history.complete
    assert history.conversation == before.conversation
    assert history.matches_prefix(before.extent, before.digest)
    assert history.turn != before.turn and history.frontier != before.frontier
    assert history.state.turn_open is False and history.state.pending_tools == ()
    assert len(history.state.completed_turns) == 2
    assert [item.text for item in found] == ['Synthetic task', 'New prompt after resume']
    assert not found.skipped
    assert len(claude.person_inputs(native.path, ())) == 2


@pytest.mark.parametrize(('kind', 'fields'), [
    ('data-flow-records', {'records': [{'response_id': 'msg_Synthetic', 'listed': [None]}]}),
    ('data-flow-records', {'records': [{'tool_name': 'Bash', 'tool_use_id': 'toolu_Synthetic'}]}),
    ('data-flow-records', {'snapshot': True, 'records': [{'tool_name': 'Bash', 'tool_use_id': 'other-native-form'}]}),
    ('data-flow-records', {'snapshot': True, 'records': [{'tool_name': 'Other', 'tool_use_id': 'toolu_Synthetic'}]}),
    ('data-flow-records', {'records': [], 'snapshot': False}),
    ('data-flow-records', {'records': [], 'agentId': 'child-form'}),
    ('data-flow-incomplete', {'codes': [True]}),
    ('data-flow-incomplete', {'codes': [256]}),
    ('data-flow-incomplete', {'codes': [9], 'uuid': 'unexpected-field'}),
    ('derived-from', {'sessions': [{'type': 'claude_code_session', 'id': 'foreign', 'via': 'fork'}]}),
    ('derived-from', {'sessions': [{'type': 'claude_code_session', 'id': 'foreign', 'via': 'compact'}]}),
    ('derived-from', {'sessions': [{'type': 'claude_code_session', 'id': 'prior', 'via': 'resume', 'started': 'other'}]}),
])
def test_unmeasured_metadata_keeps_execution_and_compaction_unknown(tmp_path, kind, fields):
    native = Native(tmp_path / 'record.jsonl', 'claude')
    native.finish()
    assert not claude.inputs(native.path).skipped
    assert claude.compactions(native.path).complete
    native.rows.append(metadata(native, kind, **fields))
    native.flush()

    state = claude.state(native.path)
    found = claude.inputs(native.path)
    assert state.turn_open is False and state.pending_tools is None
    assert [item.text for item in found] == ['Synthetic task']
    assert found.skipped == [dict(line=4, kind=kind, reason='unknown native data-flow metadata')]
    assert claude.compactions(native.path).count is None
    with pytest.raises(ValueError, match='unknown native data-flow metadata'):
        claude.person_inputs(native.path, ())


@pytest.mark.parametrize('position', [0, 3])
def test_metadata_cannot_establish_a_transcript_owner(tmp_path, position):
    native = Native(tmp_path / 'record.jsonl', 'claude')
    native.finish()
    row = metadata(native, 'data-flow-records', records=[])
    native.rows.insert(position, row)
    native.flush()
    assert claude.state(native.path).pending_tools == ()
    assert claude.compactions(native.path).complete

    row['sessionId'] = 'other-conversation'
    native.flush()
    assert claude.state(native.path).pending_tools is None
    assert claude.compactions(native.path).count is None
    assert [item.text for item in claude.inputs(native.path)] == ['Synthetic task']


def test_metadata_still_uses_the_bounded_full_reader_for_a_large_row(tmp_path, monkeypatch):
    native = Native(tmp_path / 'record.jsonl', 'claude')
    native.finish()
    before = claude.compactions(native.path)
    items = [dict(response_id=f'msg_Synthetic{i}') for i in range(128)]
    native.rows.append(metadata(native, 'data-flow-records', records=items))
    native.flush()
    monkeypatch.setattr(claude_record, 'ROW_BYTES', 1024)
    state = claude.state(native.path)
    assert state.projection_usage.compatibility
    assert state.pending_tools == ()
    history = claude.compactions(native.path)
    assert history.extent == native.path.stat().st_size
    assert history.matches_prefix(before.extent, before.digest)


def test_metadata_does_not_close_an_open_turn_or_identify_a_record(tmp_path):
    native = Native(tmp_path / 'record.jsonl', 'claude')
    note = metadata(native, 'data-flow-incomplete', codes=[9])
    native.rows.append(note)
    native.flush()
    state = claude.state(native.path)
    assert state.turn_open is True and not state.completed_turns
    assert not claude.compactions(native.path).complete

    native.rows[:] = [note]
    native.flush()
    with pytest.raises(claude_record.record_file.RecordError, match='no Claude Code entries'):
        claude.state(native.path)
    assert claude.compactions(native.path).conversation is None


@pytest.mark.parametrize('version', [True, 2, '1'])
def test_unmeasured_metadata_schema_versions_stay_unknown(tmp_path, version):
    native = Native(tmp_path / 'record.jsonl', 'claude')
    native.finish()
    row = metadata(native, 'data-flow-records', records=[])
    row['v'] = version
    native.rows.append(row)
    native.flush()
    assert claude.state(native.path).pending_tools is None
    assert claude.compactions(native.path).count is None
    assert claude.inputs(native.path).skipped
