"""Native environment and execution evidence must agree with the recovery gate."""

from dataclasses import replace
import json

import pytest

from quarterdeck import endpoint
from quarterdeck.harness.synthetic_record import CodexShape, ClaudeShape
from quarterdeck.service import Service
from .test_human_tools import resolved
from .test_human_recovery import seat_for


@pytest.mark.parametrize('human', [False, True])
def test_opencode_launch_binds_guarded_home_only_outside_human_sessions(tmp_path, monkeypatch, human):
    monkeypatch.setattr(endpoint, 'choose', lambda pool, model: endpoint.Choice(pool.name, 'synthetic', 8192, 1024))
    seat = resolved(tmp_path, 'opencode', human)
    spec = replace(seat.spec, sandbox=False)
    seat.harness.build_home(seat.home, spec, seat.pool, None)
    launch = seat.harness.launch(spec, seat.pool, seat.home, tmp_path/'brief', None, None)
    if human:
        assert 'HOME' not in launch.env
    else:
        assert launch.env.get('HOME') == str(seat.home)


def codex_record(tmp_path):
    record = CodexShape(tmp_path/'home', tmp_path, 'synthetic', 'synthetic')
    record.begin('Synthetic work', 'synthetic', 'low')
    return record


def ask(record):
    record.line('response_item', {'type': 'function_call', 'name': 'request_user_input', 'call_id': 'ask',
                                 'arguments': json.dumps({'questions': [{'id': 'choice', 'header': 'Choice',
                                     'question': 'Which synthetic option?', 'options': [
                                         {'label': 'First', 'description': 'First option'},
                                         {'label': 'Second', 'description': 'Second option'}]}]})})


@pytest.mark.parametrize('tool', [None, 'exec_command'])
@pytest.mark.parametrize('metadata', [None, 'token_usage_record', 'world_state', 'reasoning'])
def test_completed_codex_foreground_work_and_metadata_keep_ordinary_relaunch(tmp_path, tool, metadata):
    record = codex_record(tmp_path)
    if tool:
        record.tool(tool, 'synthetic command', 'finished', False)
    record.end('Finished')
    seat, calls = seat_for(record.path, 'codex')
    measured = seat.harness.turns(record.path)
    if metadata == 'reasoning':
        record.line('response_item', {'type': 'reasoning', 'summary': [], 'encrypted_content': 'synthetic'})
    elif metadata:
        record.line(metadata, {'synthetic': True})
    state = seat.harness.state(record.path)
    assert state.turn_open is False and len(state.completed_turns) == 1
    assert state.pending_tools == ()
    assert Service.relaunch(None, seat, False) == 'fresh' and calls == ['fresh']
    assert seat.harness.turns(record.path) == measured, 'proof metadata does not change measured rows'


def test_completed_foreground_call_does_not_poison_a_later_question_wait(tmp_path):
    record = codex_record(tmp_path)
    record.tool('exec_command', 'synthetic command', 'finished', False)
    record.end('Finished')
    record.begin('Next synthetic work', 'synthetic', 'low')
    ask(record)
    seat, calls = seat_for(record.path, 'codex')
    assert seat.harness.state(record.path).pending_tools == ('request_user_input',)
    assert Service.relaunch(None, seat, False) == 'fresh' and calls == ['fresh']
    assert seat.harness.state(record.path).turn_open is True


@pytest.mark.parametrize('payload', ['unparsed', {'type': 'future_tool_call', 'name': 'shell', 'call_id': 'other'},
                                   {'type': 'function_call', 'name': 'shell', 'call_id': 'other', 'arguments': '{'},
                                   {'type': 'message', 'role': 'assistant', 'content': [{'type': 'future_tool_call', 'name': 'shell'}]},
                                   {'type': 'function_call', 'name': 'request_user_input', 'call_id': 'streaming',
                                    'arguments': '{}', 'status': 'in_progress'}])
def test_unparsed_codex_execution_payload_is_not_a_question_only_proof(tmp_path, payload):
    record = codex_record(tmp_path)
    ask(record)
    record.line('response_item', payload)
    seat, calls = seat_for(record.path, 'codex')
    assert seat.harness.state(record.path).pending_tools is None
    assert Service.relaunch(None, seat, False) is None and calls == []
    assert seat.harness.state(record.path).turn_open is True


@pytest.mark.parametrize('output', [json.dumps({'output': 'still running', 'session_id': 17}),
                                  json.dumps({'output': 'unproved'}),
                                  json.dumps({'exit_code': None, 'session_id': 17}),
                                  json.dumps({'output': 'Exit code: 0', 'session_id': 17}),
                                  'Wall time: 0.1000 seconds\nProcess running with session ID 17\nOutput:\nProcess exited with code 0',
                                  'Wall time: 0.1000 seconds\nOutput:\nProcess exited with code 0',
                                  'Wall time: ... seconds\nProcess exited with code 0\nOutput:\nfinished'])
def test_returned_or_unproved_execution_stays_held_after_root_completion(tmp_path, output):
    record = codex_record(tmp_path)
    record.tool_start('exec_command', 'synthetic command')
    record.line('response_item', {'type': 'function_call_output', 'call_id': record.call, 'output': output})
    record.end('Root finished')
    seat, calls = seat_for(record.path, 'codex')
    assert seat.harness.state(record.path).pending_tools != ()
    assert Service.relaunch(None, seat, False) is None and calls == []


def test_completed_claude_foreground_tool_and_known_metadata_remain_recoverable(tmp_path):
    record = ClaudeShape(tmp_path/'home', tmp_path, 'synthetic', 'synthetic')
    record.begin('Synthetic work', 'synthetic', 'low')
    record.tool('Bash', 'synthetic command', 'finished', False)
    record.end('Finished')
    record.write({'type': 'file-history-snapshot', 'snapshot': {}})
    seat, calls = seat_for(record.path, 'claude')
    assert seat.harness.state(record.path).pending_tools == ()
    assert Service.relaunch(None, seat, False) == 'fresh' and calls == ['fresh']


@pytest.mark.parametrize('output', [json.dumps({'exit_code': 0, 'output': 'finished'}),
                                  'Chunk ID: synthetic\nWall time: 0.1000 seconds\nProcess exited with code 0\nOutput:\nfinished',
                                  [{'type': 'input_text', 'text': 'Wall time: 0.1000 seconds\nProcess exited with code 0\nOutput:\nfinished'}]])
def test_native_foreground_result_encodings_release_after_recorded_turn_end_only(tmp_path, output):
    record = codex_record(tmp_path)
    record.tool_start('exec_command', 'synthetic command')
    record.line('response_item', {'type': 'function_call_output', 'call_id': record.call, 'output': output})
    seat, calls = seat_for(record.path, 'codex')
    assert Service.relaunch(None, seat, False) is None and calls == []
    record.end('Root finished')
    assert Service.relaunch(None, seat, False) == 'fresh' and calls == ['fresh']


@pytest.mark.parametrize('remaining', [True, False])
def test_process_poll_completion_clears_only_its_matching_returned_handle(tmp_path, remaining):
    record = codex_record(tmp_path)
    for session in (17, 18):
        record.tool_start('exec_command', 'synthetic command')
        record.line('response_item', {'type': 'function_call_output', 'call_id': record.call,
                                     'output': json.dumps({'session_id': session, 'output': 'running'})})
    for session in ((17,) if remaining else (17, 18)):
        call = f'poll-{session}'
        record.line('response_item', {'type': 'function_call', 'name': 'write_stdin', 'call_id': call,
                                     'arguments': json.dumps({'session_id': session, 'chars': ''})})
        record.line('response_item', {'type': 'function_call_output', 'call_id': call,
                                     'output': json.dumps({'exit_code': 0, 'output': 'finished'})})
    record.end('Root finished')
    seat, calls = seat_for(record.path, 'codex')
    assert bool(Service.relaunch(None, seat, False)) is (not remaining)
    assert calls == ([] if remaining else ['fresh'])
