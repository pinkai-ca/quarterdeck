"""Synthetic Claude records with source-backed metadata and intact native IDs."""

import json

import pytest

from quarterdeck.harness import claude, claude_record, json_record
from tests.compaction_shapes import Native
from .test_compactions_incremental import candidate_boundary


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


def remote_control_startup(session):
    url = 'https://example.invalid/synthetic-bridge'
    leaf = 'synthetic-bridge-status'
    event = dict(tool_name='RemoteControlLinked', event_id='event_00000000-0000-4000-8000-000000000001')
    return [
        dict(type='custom-title', sessionId=session, customTitle='Synthetic seat'),
        dict(type='agent-name', sessionId=session, agentName='Synthetic seat'),
        dict(type='mode', sessionId=session, mode='normal'),
        dict(type='permission-mode', sessionId=session, permissionMode='bypassPermissions'),
        dict(type='session-owner', sessionId=session, accountUuid='synthetic-account',
             organizationUuid='synthetic-organization'),
        dict(type='bridge-session', sessionId=session, bridgeSessionId='synthetic-bridge', lastSequenceNum=0,
             ownerAccountUuid='synthetic-account', ownerOrganizationUuid='synthetic-organization'),
        dict(type='system', subtype='bridge_status', sessionId=session, uuid=leaf, parentUuid=None,
             isSidechain=False, version='2.1.331', dataFlowOn=True, userType='external', entrypoint='cli',
             cwd='/synthetic/workspace', gitBranch='HEAD', isMeta=False, url=url, timestamp='2030-01-01T00:00:00Z',
             content=f'/remote-control is active · Continue here, on your phone, or at {url}'),
        dict(type='data-flow-records', v=1, sessionId=session, records=[event], snapshot=True),
    ]


def test_remote_control_startup_keeps_the_root_idle_across_appends(tmp_path):
    record = tmp_path / 'synthetic-session.jsonl'
    rows = remote_control_startup(record.stem)
    record.write_text(''.join(json.dumps(row) + '\n' for row in rows[:7]))
    for row in [rows[7], dict(type='file-history-snapshot', isSnapshotUpdate=False),
                dict(type='atis-latch', sessionId=record.stem, atis='')]:
        assert claude.state(record).pending_tools == ()
        assert not claude.inputs(record).skipped
        with record.open('a') as stream:
            stream.write(json.dumps(row) + '\n')

    state = claude.state(record)
    assert state.turn_open is False and state.pending_tools == () and state.queue_error is None
    assert not state.completed_turns and not state.active_tools
    found = claude.inputs(record)
    assert not found and not found.skipped
    assert claude.person_inputs(record, ()) == []

    # Ordinary tool evidence still opens a turn after the startup and stays pending.
    native = Native(tmp_path / 'ordinary.jsonl', 'claude', conversation=record.stem)
    native.call()
    with record.open('a') as stream:
        for row in native.rows:
            stream.write(json.dumps(row) + '\n')
    state = claude.state(record)
    assert state.turn_open is True and state.pending_tools == ('Bash',)


@pytest.mark.parametrize('stripped', [False, True])
def test_remote_control_checkpoint_uses_ordinary_metadata_and_continuation_stays_unknown(tmp_path, stripped):
    record = tmp_path / 'synthetic-session.jsonl'
    rows = remote_control_startup(record.stem)
    checkpoint = dict(type='last-prompt', leafUuid=rows[6]['uuid'], sessionId=record.stem)
    record.write_text(''.join(json.dumps(row) + '\n' for row in [*rows, checkpoint]))

    assert claude.state(record).pending_tools == ()
    assert not claude.inputs(record).skipped
    assert claude.person_inputs(record, ()) == []

    continuation = rows[7]['records'][0]
    if stripped:
        continuation = dict(tool_name='<str 19>', event_id='<str 42>')
    with record.open('a') as stream:
        stream.write(json.dumps(dict(type='data-flow-records', v=1, sessionId=record.stem,
                                     records=[dict(continuation, no_message=True)])) + '\n')

    assert claude.state(record).pending_tools is None
    assert claude.inputs(record).skipped == [
        dict(line=10, kind='data-flow-records', reason='unknown native data-flow metadata'),
    ]
    with pytest.raises(ValueError, match='unknown native data-flow metadata'):
        claude.person_inputs(record, ())


@pytest.mark.parametrize(('index', 'changes', 'unknown_line'), [
    (5, {'lastSequenceNum': 1}, 8),
    (6, {'version': '2.1.317'}, 8),
    (6, {'entrypoint': 'sdk'}, 8),
    (6, {'parentUuid': 'prior-turn'}, 8),
    (6, {'isSidechain': True}, 8),
    (6, {'sessionId': 'other-session'}, 8),
    (7, {'snapshot': False}, 8),
    (7, {'agentId': 'child'}, 8),
    (7, {'sessionId': 'other-session'}, 8),
    (7, {'records': [{'tool_name': 'RemoteControlInput', 'event_id':
                      'event_00000000-0000-4000-8000-000000000001'}]}, 8),
    (7, {'records': [{'tool_name': 'RemoteControlLinked', 'event_id': 'event_other'}]}, 8),
])
def test_unmatched_remote_control_startup_keeps_metadata_unknown(tmp_path, index, changes, unknown_line):
    record = tmp_path / 'synthetic-session.jsonl'
    rows = remote_control_startup(record.stem)
    rows[index].update(changes)
    record.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    assert claude.state(record).pending_tools is None
    assert claude.inputs(record).skipped[0] == dict(line=unknown_line, kind='data-flow-records',
                                                   reason='unknown native data-flow metadata')
    with pytest.raises(ValueError, match='unknown native data-flow metadata'):
        claude.person_inputs(record, ())


@pytest.mark.parametrize('other_context', ['different-file', 'after-a-turn'])
def test_remote_control_startup_requires_the_current_root_prefix(tmp_path, other_context):
    record = tmp_path / 'synthetic-session.jsonl'
    rows = remote_control_startup(record.stem)
    if other_context == 'different-file':
        record = tmp_path / 'other-session.jsonl'
    else:
        native = Native(record, 'claude', conversation=record.stem)
        native.finish()
        rows = [*native.rows, *rows]
    record.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    assert claude.state(record).pending_tools is None
    assert claude.inputs(record).skipped
    with pytest.raises(ValueError, match='unknown native data-flow metadata'):
        claude.person_inputs(record, ())


EVENT = 'event_00000000-0000-4000-8000-0000000000{:02d}'
MEMORY = 'synthetic-memory-' + 'a' * 38  # the measured class: 55 of '-', digits, '_' and lower case


@pytest.mark.parametrize('fields', [
    # D: the program's own memory write beside an ordinary response entry
    {'records': [{'event_id': EVENT.format(1), 'touched': {'action': 'write', 'id': MEMORY, 'kind': 'memory'}},
                 {'listed': ['msg_SyntheticOld'], 'mark_sent_by': 'synthetic-session', 'response_id': 'msg_SyntheticNew'}]},
    # C: a tool event restated in a compaction snapshot
    {'snapshot': True, 'records': [{'event_id': EVENT.format(2), 'tool_name': 'Read'}]},
    # The 2.1.331 snapshot tool-use names
    {'snapshot': True, 'records': [{'tool_name': name, 'tool_use_id': f'toolu_Synthetic{i}'}
                                   for i, name in enumerate(('Read', 'SendUserFile', 'Write'))]},
])
def test_measured_331_metadata_carries_no_input(tmp_path, fields):
    native = Native(tmp_path / 'record.jsonl', 'claude')
    native.finish()
    native.rows.append(metadata(native, 'data-flow-records', **fields))
    native.flush()
    found = claude.inputs(native.path)
    assert [item.text for item in found] == ['Synthetic task'] and not found.skipped
    assert claude.state(native.path).pending_tools == ()
    assert claude.compactions(native.path).count == 0
    assert len(claude.person_inputs(native.path, ())) == 1


REMOTE_INPUT = {'event_id': EVENT.format(3), 'tool_name': 'RemoteControlInput'}
RESPONSE = {'mark_sent_by': 'synthetic-session', 'response_id': 'msg_SyntheticNew'}


def memory(event=EVENT.format(5), **touched):
    return {'event_id': event, 'touched': {'action': 'write', 'id': MEMORY, 'kind': 'memory', **touched}}


SHORT_MEMORY = memory(id=MEMORY[:42])
SOURCE_SESSION = dict(type='claude_code_session', id=EVENT.format(8).removeprefix('event_'))
SESSION_EVENT = dict(event_id=EVENT.format(6), source=SOURCE_SESSION)
UNKNOWN_EVENT = dict(event_id=EVENT.format(7), source=dict(type='unknown', id=None))
LISTED_RESPONSE = dict(listed=['msg_SyntheticOld'], response_id='msg_SyntheticNew')
READ_EVENT = dict(event_id=EVENT.format(4), tool_name='Read')


@pytest.mark.parametrize(('records', 'snapshot'), [
    ([dict(tool_name='Edit', tool_use_id='toolu_SyntheticEdit'),
      dict(tool_name='ToolSearch', tool_use_id='toolu_SyntheticSearch')], True),
    ([dict(tool_name='SyntheticUnseenTool', tool_use_id='toolu_SyntheticUnseen'),
      dict(event_id=EVENT.format(9), tool_name='SyntheticUnseenTool')], True),
    ([READ_EVENT, SESSION_EVENT, UNKNOWN_EVENT, SHORT_MEMORY, LISTED_RESPONSE], True),
    ([READ_EVENT, LISTED_RESPONSE], False),
    ([LISTED_RESPONSE, READ_EVENT], False),
    ([UNKNOWN_EVENT], False),
], ids=['edit-and-toolsearch', 'unseen-tool', 'snapshot-updates', 'read-and-response',
        'response-and-read', 'standalone-unknown-source'])
def test_331_snapshot_and_update_metadata_preserves_the_compaction_count(tmp_path, records, snapshot):
    native = Native(tmp_path / 'record.jsonl', 'claude')
    native.finish()
    native.compact()
    before = claude.compactions(native.path)
    assert before.count == 1
    fields = dict(records=records, snapshot=True) if snapshot else dict(records=records)
    row = metadata(native, 'data-flow-records', **fields)
    with native.path.open('a') as stream:
        stream.write(json.dumps(row) + '\n')

    after = claude.compactions(native.path)
    assert after.reason is None and after.count == 1 and after.complete
    assert after.frontier == before.frontier and after.turn == before.turn
    assert after.matches_prefix(before.extent, before.digest)
    assert claude.inputs(native.path).skipped == [
        dict(line=5, kind='data-flow-records', reason='unknown native data-flow metadata'),
    ]
    json_record._snapshots.clear()
    assert claude.compactions(native.path) == after


@pytest.mark.parametrize('with_response', [False, True], ids=['alone', 'paired'])
@pytest.mark.parametrize('damage', [
    dict(sessionId='foreign-session'),
    dict(records=[dict(SESSION_EVENT, source=dict(SOURCE_SESSION, type='unmeasured'))]),
    dict(extra=True),
], ids=['foreign-session', 'unknown-source-type', 'extra-key'])
def test_two_automatic_boundaries_across_measured_data_flow_updates(tmp_path, monkeypatch, with_response, damage):
    monkeypatch.setattr(json_record, '_KEEP_ROWS', False)
    native = Native(tmp_path / 'record.jsonl', 'claude')
    native.finish()

    def summary(anchor):
        row = native.add('user', isCompactSummary=True, message=dict(role='user', content='Synthetic summary'))
        row['uuid'] = native.previous = anchor
        native.flush()

    native.compact()
    native.rows[-1]['version'] = '2.1.317'
    summary('synthetic-first-summary')
    before = claude.compactions(native.path)
    assert before.count == 1
    reader = claude._Reader
    interpreter = claude_record.parse(native.path).interpreters[reader][0]

    response = [LISTED_RESPONSE] if with_response else []
    notes = [metadata(native, 'data-flow-records', records=items) for items in (
        [SHORT_MEMORY, *response], [SESSION_EVENT, *response], [UNKNOWN_EVENT, LISTED_RESPONSE],
    )]
    first_note_line = len(native.rows) + 1
    native.rows.extend(notes)
    with native.path.open('a') as stream:
        for note in notes:
            stream.write(json.dumps(note) + '\n')
    after = claude.compactions(native.path)
    assert claude_record.parse(native.path).interpreters[reader][0] is interpreter
    assert after.count == 1 and after.frontier == before.frontier
    assert after.state.pending_tools is None
    assert claude.inputs(native.path).skipped == [
        dict(line=line, kind='data-flow-records', reason='unknown native data-flow metadata')
        for line in range(first_note_line, first_note_line + len(notes))
    ]

    native.begin('Synthetic work after the first compaction')
    native.finish()
    candidate_boundary(native, 'auto', anchor='synthetic-second-summary', form='precomputed')
    summary('synthetic-second-summary')
    warm = claude.compactions(native.path)
    assert warm.count == 2 and warm.matches_prefix(before.extent, before.digest)
    assert claude_record.parse(native.path).interpreters[reader][0] is interpreter
    json_record._snapshots.clear()
    assert claude.compactions(native.path) == warm

    notes[1].update(damage)
    native.flush()
    assert claude.compactions(native.path).count is None
    json_record._snapshots.clear()
    assert claude.compactions(native.path).count is None


@pytest.mark.parametrize('raw', ['{"type":"unmeasured-native-row"}\n', '{broken}\n', '[]\n', '{"type":'])
def test_damaged_or_unknown_append_leaves_cold_and_carried_counts_unknown(tmp_path, raw):
    native = Native(tmp_path / 'record.jsonl', 'claude')
    native.finish()
    native.compact()
    assert claude.compactions(native.path).count == 1
    with native.path.open('a') as stream:
        stream.write(raw)
    assert claude.compactions(native.path).count is None
    json_record._snapshots.clear()
    assert claude.compactions(native.path).count is None


@pytest.mark.parametrize(('records', 'snapshot'), [
    # E stays with its joins until measured: a person's Remote Control input is never plain metadata.
    ([REMOTE_INPUT, RESPONSE, {'tool_name': 'Write', 'tool_use_id': 'toolu_Synthetic'}], True),
    ([{'event_id': EVENT.format(1), 'tool_name': 'RemoteControlLinked', 'no_message': True}], False),
    ([{'event_id': EVENT.format(1), 'tool_name': 'RemoteControlLinked'}], False),
    ([{'event_id': EVENT.format(1), 'tool_name': 'RemoteControlLinked'}], True),
    ([REMOTE_INPUT, REMOTE_INPUT, RESPONSE], False),  # not the measured mixed form: two inputs
    ([REMOTE_INPUT, RESPONSE, {'tool_name': 'Write', 'tool_use_id': 'toolu_Synthetic'}], False),
    ([READ_EVENT], False),
    ([READ_EVENT, RESPONSE], False),
    ([READ_EVENT, dict(LISTED_RESPONSE, listed=[None])], False),
    ([READ_EVENT, dict(LISTED_RESPONSE, response_id='invalid')], False),
    ([READ_EVENT, dict(LISTED_RESPONSE, mark_sent_by='synthetic-session')], False),
    ([READ_EVENT, LISTED_RESPONSE, LISTED_RESPONSE], False),
    ([dict(READ_EVENT, tool_name='Edit'), LISTED_RESPONSE], False),
    ([{'tool_name': 'Write', 'tool_use_id': 'toolu_Synthetic'}], False),  # a tool-use entry outside a snapshot
    ([{'tool_name': '', 'tool_use_id': 'toolu_Synthetic'}], True),
    ([{'tool_name': 1, 'tool_use_id': 'toolu_Synthetic'}], True),
    ([{'tool_name': 'Edit', 'tool_use_id': 'toolu_Synthetic', 'extra': True}], True),
    ([dict(READ_EVENT, tool_name='')], True),
    ([dict(READ_EVENT, tool_name=None)], True),
    ([dict(READ_EVENT, event_id='event_other')], True),
    ([dict(READ_EVENT, extra=True)], True),
    ([memory(action='read')], False),
    ([memory(id=MEMORY + 'b')], True),
    ([memory(id=MEMORY.upper())], False),
    ([memory(kind='other')], False),
    ([memory(event='event_other')], False),
    ([memory(id=MEMORY[:41])], True),
    ([memory(id=MEMORY[:42], action='read')], True),
    ([memory(id=MEMORY[:41])], False),
    ([dict(SESSION_EVENT, source=dict(SOURCE_SESSION, extra=True))], False),
    ([dict(SESSION_EVENT, source=dict(SOURCE_SESSION, id='not-a-uuid'))], False),
    ([dict(SESSION_EVENT, event_id='event_other')], False),
    ([dict(SESSION_EVENT, source=dict(SOURCE_SESSION, id='not-a-uuid'))], True),
    ([dict(SESSION_EVENT, source=dict(SOURCE_SESSION, extra=True))], True),
    ([dict(UNKNOWN_EVENT, source=dict(type='unknown', id='unexpected'))], True),
    ([dict(UNKNOWN_EVENT, source=dict(type='unknown', id='unexpected')), LISTED_RESPONSE], False),
    ([SESSION_EVENT, RESPONSE], False),
    ([SESSION_EVENT, LISTED_RESPONSE, LISTED_RESPONSE], False),
    ([SHORT_MEMORY, SESSION_EVENT], False),
])
def test_331_remote_and_unmeasured_entries_stay_unknown(tmp_path, records, snapshot):
    native = Native(tmp_path / 'record.jsonl', 'claude')
    native.finish()
    fields = dict(records=records, snapshot=True) if snapshot else dict(records=records)
    native.rows.append(metadata(native, 'data-flow-records', **fields))
    native.flush()
    found = claude.inputs(native.path)
    assert found.skipped == [dict(line=4, kind='data-flow-records', reason='unknown native data-flow metadata')]
    assert claude.compactions(native.path).count is None
    with pytest.raises(ValueError, match='unknown native data-flow metadata'):
        claude.person_inputs(native.path, ())


def test_the_measured_mixed_remote_input_holds_quiet_time_under_its_own_name(tmp_path):
    from types import SimpleNamespace
    from quarterdeck import person_quiet

    native = Native(tmp_path / 'record.jsonl', 'claude')
    native.finish()
    native.rows.append(metadata(native, 'data-flow-records', records=[REMOTE_INPUT, RESPONSE]))
    native.flush()
    found = claude.inputs(native.path)
    # The typed root keeps its own proved time; the unbound event is neither dated nor dismissed.
    assert [item.text for item in found] == ['Synthetic task']
    assert found.skipped == [dict(line=4, kind='data-flow-records', reason=claude.MIXED_REMOTE_INPUT)]
    assert claude.compactions(native.path).count is None
    with pytest.raises(ValueError, match='Remote Control input of unknown time'):
        claude.person_inputs(native.path, ())
    from datetime import datetime, timezone
    launch = SimpleNamespace(pane='%1', pid=1, at='2026-11-24T00:00:00+00:00', harness='claude', version='2.1.331')
    seat = SimpleNamespace(launched=lambda: launch, record=lambda: native.path, harness=claude.ClaudeHarness())
    fact = person_quiet.read(seat, datetime.now(timezone.utc))
    assert fact['at'] is None and fact['reason'] == claude.MIXED_REMOTE_INPUT


def test_the_mixed_remote_input_is_a_named_move_hold_in_the_record_reading(tmp_path):
    from quarterdeck import idle

    native = Native(tmp_path / 'synthetic-session.jsonl', 'claude', conversation='synthetic-session')
    native.finish()
    native.rows.append(metadata(native, 'data-flow-records', records=[REMOTE_INPUT, RESPONSE]))
    native.flush()
    state = claude.state(native.path)
    assert state.input_hold == claude.MIXED_REMOTE_INPUT
    assert idle.check(state) == idle.Hold('input-unknown', claude.MIXED_REMOTE_INPUT)
    native.rows[-1] = metadata(native, 'data-flow-records', records=[REMOTE_INPUT, REMOTE_INPUT, RESPONSE])
    native.flush()
    assert getattr(claude.state(native.path), 'input_hold', None) is None  # other unknown rows: the generic reason
