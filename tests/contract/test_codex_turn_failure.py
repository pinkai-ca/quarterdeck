"""A Codex turn that ended in a recorded error reads as refused or failed, never idle; synthetic rows only.

Codex 0.166.0 ends such a turn with ``task_complete`` carrying ``error`` and a null
``last_agent_message``. Only the native refusal classification reads as a refusal; any other error is a
provider error, and model output is proved absent only by complete known rows.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from quarterdeck.harness import codex, pi

START = datetime(2031, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
MESSAGE = 'Synthetic refusal stand-in: the provider declined this invented request.'
TEXT = 'qd > order from tester · ord-a1b2 · Invented task\nDo the invented thing.\nqd > reply: qd post report --re ord-a1b2'


def row(kind, payload, seconds):
    return {'type': kind, 'timestamp': (START + timedelta(seconds=seconds)).isoformat(), 'payload': payload}


def user(text, seconds):
    return row('response_item', {'type': 'message', 'role': 'user',
                                 'content': [{'type': 'input_text', 'text': text}]}, seconds)


def tokens(output, reasoning, seconds):
    return row('event_msg', {'type': 'token_count', 'info': {'last_token_usage': {
        'input_tokens': 10, 'cached_input_tokens': 0, 'output_tokens': output,
        'reasoning_output_tokens': reasoning, 'total_tokens': 10 + output}}}, seconds)


def complete(seconds, error=None, last='absent'):
    payload = {'type': 'task_complete'}
    if last != 'absent':
        payload['last_agent_message'] = last
    if error is not None:
        payload['error'] = error
    return row('event_msg', payload, seconds)


REFUSED = {'message': MESSAGE, 'codex_error_info': 'cyber_policy'}


def write(tmp_path: Path, rows) -> Path:
    record = tmp_path / 'sessions' / 'rollout-synthetic.jsonl'
    record.parent.mkdir(parents=True, exist_ok=True)
    head = [row('session_meta', {'id': 'synthetic-session'}, 0)]
    record.write_text(''.join(json.dumps(r) + '\n' for r in head + rows))
    return record


def refused_turn(*middle, error=REFUSED, last=None, at=1):
    return [row('event_msg', {'type': 'task_started'}, at), user(TEXT, at + 1), *middle,
            complete(at + 5, error, last)]


def test_a_refusal_with_no_model_output_reads_refused_with_its_first_words_never_idle(tmp_path):  # law: record-first
    record = write(tmp_path, refused_turn(row('event_msg', {'type': 'token_count', 'info': None}, 2)))
    state, turns = codex.read(record)
    failure = state.failure
    assert state.turn_open is False and failure is not None
    assert failure.kind == 'provider refusal' and failure.code == 'cyber_policy'
    assert failure.model_output is False, 'complete known rows, null final message and no output: proved none'
    assert failure.message == ' '.join(MESSAGE.split()[:16])
    assert [i.text for i in failure.inputs] == [TEXT]
    assert turns[-1].failure == failure, 'the Ledger keeps the turn; the failure rides beside it'


@pytest.mark.parametrize('middle', [
    [tokens(12, 0, 2)],
    [tokens(0, 7, 2)],
    [row('response_item', {'type': 'reasoning', 'summary': []}, 2)],
    [row('response_item', {'type': 'message', 'role': 'assistant',
                           'content': [{'type': 'output_text', 'text': 'Partial invented answer'}]}, 2)],
    [row('event_msg', {'type': 'agent_reasoning', 'text': 'invented'}, 2)],
], ids=['output-tokens', 'reasoning-tokens', 'reasoning-item', 'assistant-text', 'agent-reasoning'])
def test_any_model_output_before_the_error_disqualifies_unacted(tmp_path, middle):
    failure = codex.state(write(tmp_path, refused_turn(*middle))).failure
    assert failure.kind == 'provider refusal' and failure.model_output is True


@pytest.mark.parametrize('turn', [
    refused_turn(last='absent'),
    refused_turn(row('event_msg', {'type': 'token_count', 'info': {'last_token_usage': {'output_tokens': 'x'}}}, 2)),
    refused_turn(row('response_item', {'type': 'message', 'role': 'assistant', 'content': []}, 2)),
    refused_turn(row('response_item', {'type': 'future_execution_item'}, 2)),
], ids=['final-message-field-missing', 'unreadable-count', 'empty-assistant-row', 'unknown-item'])
def test_missing_or_unknown_evidence_is_unknown_output_never_zero(tmp_path, turn):
    failure = codex.state(write(tmp_path, turn)).failure
    assert failure is not None and failure.model_output is None


@pytest.mark.parametrize('error,kind,code', [
    ({'message': 'Synthetic server trouble', 'codex_error_info': 'internal_server_error'}, 'provider error', 'internal_server_error'),
    ({'message': 'Synthetic error with no classification'}, 'provider error', None),
    ({'message': '', 'codex_error_info': 'cyber_policy'}, 'provider error', 'cyber_policy'),
    ('a bare string error', 'provider error', None),
], ids=['other-code', 'no-code', 'refusal-code-without-message', 'malformed'])
def test_only_the_known_refusal_form_is_a_refusal_and_never_a_wall(tmp_path, error, kind, code):
    state = codex.state(write(tmp_path, refused_turn(error=error)))
    assert state.failure.kind == kind and state.failure.code == code
    assert state.walls == (), 'a turn error is never read as an allowance wall'
    if not isinstance(error, dict) or not error.get('message'):
        assert state.failure.model_output is None and state.failure.message == 'error details unavailable'


def test_a_later_normal_turn_clears_the_failure_and_the_history_keeps_it(tmp_path):
    rows = refused_turn() + [row('event_msg', {'type': 'task_started'}, 10), user('Another invented message', 11),
                             row('response_item', {'type': 'message', 'role': 'assistant',
                                                   'content': [{'type': 'output_text', 'text': 'done'}]}, 12),
                             complete(13, last='done')]
    state, turns = codex.read(write(tmp_path, rows))
    assert state.failure is None and state.turn_open is False
    assert turns[0].failure is not None and turns[1].failure is None


def test_an_error_with_no_open_turn_is_shown_with_unknown_output(tmp_path):
    rows = [user(TEXT, 1), complete(2, REFUSED, None)]
    failure = codex.state(write(tmp_path, rows)).failure
    assert failure.kind == 'provider refusal' and failure.model_output is None
    assert [i.text for i in failure.inputs] == [TEXT]


def test_control_characters_and_long_errors_are_cut_to_first_words(tmp_path):
    long = '\x1b[31mInvented\x1b[0m\x07 ' + ' '.join(f'word{n}' for n in range(40))
    failure = codex.state(write(tmp_path, refused_turn(error={'message': long, 'codex_error_info': 'cyber_policy'}))).failure
    assert '\x1b' not in failure.message and '\x07' not in failure.message
    assert len(failure.message.split()) == 16 and len(failure.message) <= 160


def test_the_input_receipt_is_unchanged_by_the_reader(tmp_path):
    """Settlement is the service's, from the failure; the receipt matcher still sees the native acceptance."""
    from quarterdeck import input_receipt
    record = write(tmp_path, refused_turn())
    assert input_receipt.confirmed('codex', record, TEXT, START) == 'delivered'


def test_a_pi_error_stop_is_a_program_error_with_output_never_proved_absent(tmp_path):  # law: record-first
    record = tmp_path / 'sessions' / 'synthetic.jsonl'
    record.parent.mkdir(parents=True)
    at = lambda s: (START + timedelta(seconds=s)).isoformat().replace('+00:00', 'Z')
    rows = [{'type': 'session', 'version': 3, 'id': 'synthetic', 'timestamp': at(0), 'cwd': '/synthetic'},
            {'type': 'message', 'id': 'u1', 'timestamp': at(1),
             'message': {'role': 'user', 'content': [{'type': 'text', 'text': TEXT}]}},
            {'type': 'message', 'id': 'a1', 'timestamp': at(2),
             'message': {'role': 'assistant', 'content': [], 'stopReason': 'error',
                         'errorMessage': 'Synthetic local server unavailable'}}]
    record.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    failure = pi.state(record).failure
    assert failure.kind == 'program error' and failure.model_output is None
    assert failure.message == 'Synthetic local server unavailable'
