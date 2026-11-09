"""Unclassified native content cannot prove an exclusive wait or an empty execution set."""

import json

import pytest

from quarterdeck.harness.synthetic_record import ClaudeShape
from quarterdeck.service import Service
from .test_human_recovery import seat_for


CLAUDE_BAD = [
    ('assistant', {'content': [{'type': 'future_tool_call', 'id': 'other', 'name': 'Bash', 'input': {'command': 'synthetic'}}]}),
    ('assistant', {'content': [{'type': 'tool_use', 'id': 'other', 'name': 'Bash'}]}),
    ('assistant', {'role': 'future', 'content': [{'type': 'text', 'text': 'synthetic'}]}),
    ('assistant', {'content': 'unparsed'}),
    ('assistant', {'content': [{'type': 'text', 'text': {'unparsed': True}}]}),
    ('user', {'content': [{'type': 'future_tool_result', 'tool_use_id': 'ask'}]}),
    ('user', {'role': 'assistant', 'content': 'synthetic'}),
    ('user', {'content': [{'type': 'tool_result', 'tool_use_id': 'ask', 'content': [{'type': 'future_tool_call'}]}]}),
    ('user', {'content': [{'type': 'tool_result', 'tool_use_id': 'absent', 'content': 'synthetic'}]}),
    ('user', {'content': None}),
    ('assistant', {'content': [{'type': 'tool_use', 'id': 'duplicate', 'name': 'Bash', 'input': {}},
                               {'type': 'tool_use', 'id': 'duplicate', 'name': 'Bash', 'input': {}}]}),
]


@pytest.mark.parametrize('kind,message', CLAUDE_BAD)
@pytest.mark.parametrize('question', [True, False])
def test_claude_unclassified_payload_never_certifies_question_only_or_empty(tmp_path, kind, message, question):
    record = ClaudeShape(tmp_path/'home', tmp_path, 'synthetic', 'synthetic')
    record.begin('Synthetic work', 'synthetic', 'low')
    if question:
        blocks = [{'type': 'tool_use', 'id': 'ask', 'name': 'AskUserQuestion', 'input': {'questions': []}}]
        if kind == 'assistant' and 'role' not in message and isinstance(message.get('content'), list):
            record.assistant(blocks + message['content'], 'tool_use')
        else:
            record.assistant(blocks, 'tool_use')
            record.entry(kind, message)
    else:
        record.entry(kind, message)
        record.end('Finished')
    seat, calls = seat_for(record.path, 'claude')
    measured = seat.harness.turns(record.path)
    assert seat.harness.state(record.path).pending_tools is None
    assert Service.relaunch(None, seat, False) is None and calls == []
    assert seat.harness.turns(record.path) == measured


PI_BAD = [
    {'role': 'assistant', 'content': [{'type': 'future_tool_call', 'id': 'other', 'name': 'bash'}]},
    {'role': 'assistant', 'content': [{'type': 'toolCall', 'id': 'other', 'name': 'bash', 'arguments': 'unparsed'}]},
    {'role': 'assistant', 'content': 'unparsed'},
    {'role': 'assistant', 'content': [{'type': 'thinking', 'thinking': {'unparsed': True}}]},
    {'role': 'user', 'content': [{'type': 'toolCall', 'id': 'other', 'name': 'bash', 'arguments': {}}]},
    {'role': 'future', 'content': [{'type': 'text', 'text': 'synthetic'}]},
    {'role': 'toolResult', 'toolCallId': 'absent', 'toolName': 'bash', 'isError': False, 'content': [{'type': 'text', 'text': 'synthetic'}]},
    {'role': 'toolResult', 'toolCallId': 'call', 'toolName': 'bash', 'isError': False, 'content': [{'type': 'future_tool_call'}]},
    {'role': 'toolResult', 'toolCallId': 'call', 'toolName': 'other', 'isError': False, 'content': []},
    {'role': 'toolResult', 'toolCallId': 'call', 'toolName': 'bash', 'isError': 'unknown', 'content': []},
    {'role': 'assistant', 'content': [{'type': 'toolCall', 'id': 'duplicate', 'name': 'bash', 'arguments': {}},
                                     {'type': 'toolCall', 'id': 'duplicate', 'name': 'bash', 'arguments': {}}]},
]


def pi_rows(path, messages):
    rows = [{'type': 'session', 'id': 'synthetic', 'timestamp': '2030-01-01T00:00:00Z'}]
    for index, message in enumerate(messages):
        rows.append({'type': 'message', 'timestamp': f'2030-01-01T00:00:{index + 1:02d}Z', 'message': message})
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))


PI_USER = {'role': 'user', 'content': 'Synthetic work'}
PI_CALL = {'role': 'assistant', 'content': [{'type': 'toolCall', 'id': 'call', 'name': 'bash', 'arguments': {'command': 'synthetic'}}], 'stopReason': 'toolUse'}
PI_RESULT = {'role': 'toolResult', 'toolCallId': 'call', 'toolName': 'bash', 'isError': False, 'content': [{'type': 'text', 'text': 'finished'}]}
PI_END = {'role': 'assistant', 'content': [{'type': 'text', 'text': 'Finished'}], 'stopReason': 'stop'}


@pytest.mark.parametrize('message', PI_BAD)
def test_pi_unclassified_message_cannot_certify_ordinary_empty_recovery(tmp_path, message):
    path = tmp_path/'record'
    pi_rows(path, [PI_USER, PI_CALL, message, PI_RESULT, PI_END])
    seat, calls = seat_for(path, 'pi')
    measured = seat.harness.turns(path)
    assert seat.harness.state(path).pending_tools is None
    assert Service.relaunch(None, seat, False) is None and calls == []
    assert seat.harness.turns(path) == measured


@pytest.mark.parametrize('program', ['claude', 'pi'])
def test_known_text_thinking_and_joined_tools_preserve_recovery(tmp_path, program):
    if program == 'claude':
        record = ClaudeShape(tmp_path/'home', tmp_path, 'synthetic', 'synthetic')
        record.begin('Synthetic work', 'synthetic', 'low')
        record.assistant([{'type': 'thinking', 'thinking': 'Synthetic reasoning', 'signature': 'synthetic'},
                          {'type': 'redacted_thinking', 'data': 'synthetic'}], None)
        record.tool('Bash', 'synthetic', 'finished', False)
        record.end('Finished')
        path = record.path
    else:
        path = tmp_path/'record'
        pi_rows(path, [PI_USER, {'role': 'assistant', 'content': [{'type': 'thinking', 'thinking': 'Synthetic reasoning'}], 'stopReason': 'toolUse'}, PI_CALL, PI_RESULT, PI_END])
    seat, calls = seat_for(path, program)
    measured = seat.harness.turns(path)
    assert seat.harness.state(path).pending_tools == ()
    assert Service.relaunch(None, seat, False) == 'fresh' and calls == ['fresh']
    assert seat.harness.turns(path) == measured


def test_claude_known_nonexecution_result_content_keeps_joined_completion(tmp_path):
    record = ClaudeShape(tmp_path/'home', tmp_path, 'synthetic', 'synthetic')
    record.begin('Synthetic work', 'synthetic', 'low')
    record.tool_start('Bash', 'synthetic')
    record.entry('user', {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': record.call,
        'content': [{'type': 'image', 'source': {'type': 'base64', 'data': 'synthetic'}},
                    {'type': 'document', 'source': {'type': 'content', 'content': [{'type': 'text', 'text': 'synthetic'}]}},
                    {'type': 'search_result', 'title': 'Synthetic', 'source': 'synthetic', 'content': [{'type': 'text', 'text': 'synthetic'}]},
                    {'type': 'tool_reference', 'tool_name': 'Bash'}]}]})
    record.end('Finished')
    seat, calls = seat_for(record.path, 'claude')
    assert seat.harness.state(record.path).pending_tools == ()
    assert Service.relaunch(None, seat, False) == 'fresh' and calls == ['fresh']


@pytest.mark.parametrize('block', [{'type': 'tool_use', 'id': 'late', 'name': 'Bash', 'input': {}},
                                 {'type': 'future_tool_call', 'id': 'late', 'name': 'Bash'}])
def test_claude_call_after_terminal_accounting_cannot_disappear_from_execution_proof(tmp_path, block):
    record = ClaudeShape(tmp_path/'home', tmp_path, 'synthetic', 'synthetic')
    record.begin('Synthetic work', 'synthetic', 'low')
    record.end('Finished')
    record.assistant([block], 'tool_use')
    seat, calls = seat_for(record.path, 'claude')
    assert seat.harness.state(record.path).pending_tools is None
    assert Service.relaunch(None, seat, False) is None and calls == []


def test_pi_documented_display_and_finished_shell_roles_do_not_poison_completion(tmp_path):
    path = tmp_path/'record'
    pi_rows(path, [PI_USER, PI_CALL, PI_RESULT, PI_END,
                  {'role': 'custom', 'customType': 'synthetic', 'content': [{'type': 'text', 'text': 'Synthetic display'}], 'display': True},
                  {'role': 'branchSummary', 'summary': 'Synthetic summary', 'fromId': 'synthetic'},
                  {'role': 'compactionSummary', 'summary': 'Synthetic summary', 'tokensBefore': 1},
                  {'role': 'bashExecution', 'command': 'synthetic', 'output': 'finished', 'exitCode': 0, 'cancelled': False}])
    seat, calls = seat_for(path, 'pi')
    assert seat.harness.state(path).pending_tools == ()
    assert Service.relaunch(None, seat, False) == 'fresh' and calls == ['fresh']
