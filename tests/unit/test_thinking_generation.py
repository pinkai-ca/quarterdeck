"""Native terminal-generation evidence, independent of prior completed tool steps."""

import json
from datetime import timedelta

import pytest

from quarterdeck.harness.opencode import state, read
from tests.unit.test_opencode_record import AT, MS, database, session, message, part


def capped(con, *, reasoning=8192, output=0, finish='length'):
    session(con)
    message(con, 'input', 'root', 10, {'role': 'user', 'agent': 'build', 'time': {'created': MS + 10},
        'model': {'providerID': 'synthetic', 'modelID': 'qwen3.8-27b'}})
    part(con, 'input-text', 'input', 'root', 10, {'type': 'text', 'text': 'Synthetic accepted work'})
    # The earlier completed executable call remains context and accounting.
    message(con, 'earlier', 'root', 20, {'role': 'assistant', 'parentID': 'input', 'agent': 'build',
        'modelID': 'qwen3.8-27b', 'providerID': 'synthetic', 'time': {'created': MS + 20, 'completed': MS + 40},
        'finish': 'tool-calls', 'tokens': {'total': 15, 'input': 10, 'output': 5, 'reasoning': 0}})
    part(con, 'earlier-tool', 'earlier', 'root', 30, {'type': 'tool', 'tool': 'bash', 'callID': 'call',
        'state': {'status': 'completed', 'time': {'start': MS + 25, 'end': MS + 35}}})
    tokens = {'total': 100 + reasoning + output, 'input': 100, 'output': output, 'reasoning': reasoning,
              'cache': {'read': 0, 'write': 0}}
    message(con, 'capped', 'root', 50, {'role': 'assistant', 'parentID': 'input', 'agent': 'build', 'mode': 'build',
        'modelID': 'qwen3.8-27b', 'providerID': 'synthetic', 'time': {'created': MS + 50, 'completed': MS + 100},
        'finish': finish, 'tokens': tokens})
    part(con, 'thinking', 'capped', 'root', 60, {'type': 'reasoning', 'text': 'Synthetic thinking',
        'time': {'start': MS + 60, 'end': MS + 90}})
    part(con, 'end', 'capped', 'root', 100, {'type': 'step-finish', 'reason': finish, 'tokens': tokens, 'cost': 0})
    con.commit()


@pytest.mark.parametrize('reasoning,output', [(8192, 0), (8191, 1)])
def test_real_reader_retains_the_capped_generation_and_the_prior_completed_work(tmp_path, reasoning, output):
    record = tmp_path / 'synthetic.db'
    con = database(record)
    capped(con, reasoning=reasoning, output=output)
    found, turns = read(record)
    evidence = getattr(found, 'capped', None)
    assert evidence is not None, 'the native length/reasoning-only generation needs explicit evidence'
    assert (evidence.conversation, evidence.user, evidence.generation) == ('root', 'input', 'capped')
    assert (evidence.reasoning, evidence.written) == (reasoning, output)
    assert not found.turn_open and found.pending_tools == () and found.queued == ()
    assert len(turns) == len(found.completed_turns) == 1
    assert [tool.name for tool in turns[0].tools] == ['bash']
    assert turns[0].tokens.written == 5 + reasoning + output
    # A snapshot patch is known client metadata; it is not a terminal tool call.
    signature = evidence.signature
    part(con, 'patch', 'capped', 'root', 101, {'type': 'patch', 'hash': 'synthetic', 'files': []})
    con.commit()
    after = getattr(state(record), 'capped', None)
    assert after is not None and after.signature != signature, 'changed source needs fresh binding'
    con.close()


def pi_rows():
    def row(kind, ident, parent, offset, **fields):
        return dict(type=kind, id=ident, parentId=parent, timestamp=(AT + timedelta(milliseconds=offset)).isoformat(), **fields)
    return [dict(type='session', id='root', timestamp=AT.isoformat(), version=3, cwd='/synthetic'),
        row('model_change', 'model', None, 1, provider='synthetic', modelId='qwen3.8-27b'),
        row('thinking_level_change', 'level', 'model', 2, thinkingLevel='high'),
        row('message', 'input', 'level', 10, message={'role': 'user', 'content': [{'type': 'text', 'text': 'Synthetic work'}]}),
        row('message', 'earlier', 'input', 20, message={'role': 'assistant', 'model': 'qwen3.8-27b', 'provider': 'synthetic',
            'content': [{'type': 'toolCall', 'id': 'call', 'name': 'bash', 'arguments': {'command': 'synthetic'}}],
            'usage': {'input': 10, 'output': 5, 'totalTokens': 15}, 'stopReason': 'toolUse'}),
        row('message', 'result', 'earlier', 30, message={'role': 'toolResult', 'toolCallId': 'call', 'toolName': 'bash',
            'isError': False, 'content': [{'type': 'text', 'text': 'Synthetic completed result'}]}),
        row('message', 'capped', 'result', 100, message={'role': 'assistant', 'model': 'qwen3.8-27b', 'provider': 'synthetic',
            'content': [{'type': 'thinking', 'thinking': 'Synthetic reasoning'}],
            'usage': {'input': 100, 'output': 8192, 'totalTokens': 8292}, 'stopReason': 'length'})]


def append_compaction(con):
    model = {'providerID': 'synthetic', 'modelID': 'qwen3.8-27b'}
    message(con, 'compact-input', 'root', 20000, {'role': 'user', 'agent': 'build',
        'time': {'created': MS + 20000}, 'model': model})
    part(con, 'compact-part', 'compact-input', 'root', 20000, {'type': 'compaction', 'auto': True})
    message(con, 'summary', 'root', 20010, {'role': 'assistant', 'agent': 'compaction', 'parentID': 'compact-input',
        'summary': True, 'time': {'created': MS + 20010, 'completed': MS + 20020}, 'finish': 'stop',
        'modelID': 'qwen3.8-27b', 'providerID': 'synthetic', 'tokens': {'input': 10, 'output': 2, 'total': 12}})
    part(con, 'summary-text', 'summary', 'root', 20015, {'type': 'text', 'text': 'Synthetic summary'})
    con.commit()


def append_auto_cap(con, *, marked=True):
    model = {'providerID': 'synthetic', 'modelID': 'qwen3.8-27b'}
    message(con, 'auto-input', 'root', 20030, {'role': 'user', 'agent': 'build',
        'time': {'created': MS + 20030}, 'model': model})
    data = {'type': 'text', 'text': 'Synthetic native continuation'}
    if marked:
        data.update(synthetic=True, metadata={'compaction_continue': True})
    part(con, 'auto-text', 'auto-input', 'root', 20030, data)
    tokens = {'input': 100, 'output': 0, 'reasoning': 8192, 'total': 8292, 'cache': {'read': 0, 'write': 0}}
    message(con, 'auto-cap', 'root', 20040, {'role': 'assistant', 'agent': 'build', 'parentID': 'auto-input',
        'time': {'created': MS + 20040, 'completed': MS + 20050}, 'finish': 'length',
        'modelID': 'qwen3.8-27b', 'providerID': 'synthetic', 'tokens': tokens})
    part(con, 'auto-thinking', 'auto-cap', 'root', 20045, {'type': 'reasoning', 'text': 'Synthetic thinking'})
    part(con, 'auto-end', 'auto-cap', 'root', 20050, {'type': 'step-finish', 'reason': 'length', 'tokens': tokens})
    con.commit()


@pytest.mark.parametrize('marked', [True, False])
def test_compaction_summary_never_creates_a_new_recovery_work_identity(tmp_path, marked):
    record = tmp_path / 'synthetic.db'
    con = database(record)
    capped(con)
    append_compaction(con)
    summary = state(record)
    assert summary.capped is None and summary.work_id is None, 'an ended summary is not eligible idle work'
    append_auto_cap(con, marked=marked)
    found = state(record)
    if marked:
        assert found.capped is not None and found.capped.work == 'input' and found.capped.user == 'auto-input'
    else:
        assert found.capped is None and found.work_id is None, 'unmarked replay has no proved work lineage'
    con.close()


def test_pi_real_reader_preserves_unseparated_budget_and_native_control_is_not_new_work(tmp_path):
    from quarterdeck.harness import pi
    record = tmp_path / 'pi.jsonl'
    rows = pi_rows()
    record.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    state, turns = pi.read(record)
    assert state.capped is not None and state.capped.reasoning is None and state.capped.written == 8192
    assert state.capped.work == state.work_id == state.input_id == 'input'
    assert state.capped.generation == 'capped' and state.conversation == 'root'
    assert len(turns) == 1 and turns[0].tools[0].name == 'bash' and turns[0].tokens.written == 8197
    with record.open('a') as stream:
        stream.write(json.dumps(dict(type='thinking_level_change', id='off', parentId='capped',
            timestamp=(AT + timedelta(milliseconds=110)).isoformat(), thinkingLevel='off')) + '\n')
    after = pi.state(record)
    assert after.effort == 'off' and after.capped == state.capped and after.work_id == state.work_id


@pytest.mark.parametrize('fault', ['text', 'tool', 'unknown', 'malformed-thinking', 'stop', 'error', 'branch', 'missing-id', 'compaction'])
def test_pi_unparsed_mixed_or_nonterminal_generation_cannot_establish_recovery(tmp_path, fault):
    from quarterdeck.harness import pi
    rows = pi_rows()
    message = rows[-1]['message']
    if fault == 'text':
        message['content'].append({'type': 'text', 'text': 'A visible answer'})
    elif fault == 'tool':
        message['content'].append({'type': 'toolCall', 'id': 'future-call', 'name': 'bash', 'arguments': {}})
    elif fault == 'unknown':
        message['content'].append({'type': 'future_tool_call', 'name': 'bash'})
    elif fault == 'malformed-thinking':
        message['content'][0]['thinking'] = {}
    elif fault == 'stop':
        message['stopReason'] = 'stop'
    elif fault == 'error':
        message['errorMessage'] = 'Synthetic refusal'
    elif fault == 'branch':
        rows[-1]['parentId'] = 'unseen-branch'
    elif fault == 'missing-id':
        rows[-1].pop('id')
    else:
        rows.insert(-1, dict(type='compaction', id='summary', parentId='result', summary='Synthetic summary'))
    record = tmp_path / 'pi.jsonl'
    record.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    assert pi.state(record).capped is None


@pytest.mark.parametrize('fault', ['text', 'tool', 'unknown-part', 'malformed-thinking', 'unfinished',
                                  'compaction', 'new-input', 'later-assistant', 'child', 'unknown-role', 'error'])
def test_terminal_or_current_unknown_activity_cannot_become_recovery_evidence(tmp_path, fault):
    record = tmp_path / 'synthetic.db'
    con = database(record)
    capped(con)
    if fault == 'text':
        part(con, 'answer', 'capped', 'root', 80, {'type': 'text', 'text': 'An answer exists'})
    elif fault == 'tool':
        part(con, 'terminal-tool', 'capped', 'root', 80, {'type': 'tool', 'tool': 'bash',
            'state': {'status': 'completed', 'time': {'start': MS + 70, 'end': MS + 80}}})
    elif fault == 'unknown-part':
        part(con, 'unparsed', 'capped', 'root', 80, {'type': 'future_tool_call', 'name': 'bash'})
    elif fault == 'malformed-thinking':
        con.execute("UPDATE part SET data=? WHERE id='thinking'", (json.dumps({'type': 'reasoning', 'text': {}}),))
    elif fault == 'unfinished':
        con.execute("UPDATE message SET data=json_remove(data,'$.time.completed') WHERE id='capped'")
    elif fault == 'compaction':
        con.execute("UPDATE message SET data=json_set(data,'$.summary',json('true'),'$.agent','compaction') WHERE id='capped'")
    elif fault == 'new-input':
        message(con, 'next', 'root', 110, {'role': 'user', 'time': {'created': MS + 110}})
        part(con, 'next-text', 'next', 'root', 110, {'type': 'text', 'text': 'Superseding input'})
    elif fault == 'later-assistant':
        message(con, 'automatic', 'root', 110, {'role': 'assistant', 'parentID': 'input',
            'time': {'created': MS + 110}, 'agent': 'build'})
    elif fault == 'child':
        session(con, 'child', 110, 'root')
        message(con, 'child-input', 'child', 120, {'role': 'user', 'time': {'created': MS + 120}})
    elif fault == 'unknown-role':
        message(con, 'future', 'root', 110, {'role': 'future_executor'})
    else:
        con.execute("UPDATE message SET data=json_set(data,'$.error',json('{\"name\":\"SyntheticError\"}')) WHERE id='capped'")
    con.commit()
    assert getattr(state(record), 'capped', None) is None
    con.close()
