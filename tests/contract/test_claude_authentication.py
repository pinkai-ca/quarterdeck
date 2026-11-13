"""Ordinary adapter authentication-state cases, with synthetic records only."""
import pytest

from quarterdeck.harness import claude
from quarterdeck.harness.types import AUTHENTICATION
from tests.authentication_shapes import error_row, rows, write_record


def test_measured_error_ends_as_authentication_failure_not_idle_success(tmp_path):
    state, turns = claude.read(write_record(tmp_path / 'synthetic.jsonl'))
    assert state.turn_open is False
    assert state.failure is turns[-1].failure
    assert state.failure.kind == AUTHENTICATION
    assert state.failure.message == AUTHENTICATION
    assert state.failure.code == 'authentication_failed'
    assert state.pending_tools == ()
    assert len(state.completed_turns) == 1
    assert state.failure.model_output is None  # no inference about earlier output in the failed turn


@pytest.mark.parametrize('change', ['code', 'flag', 'flag-int', 'role', 'stop', 'block', 'sidechain',
                                  'sidechain-missing', 'sidechain-int'])
def test_other_envelopes_do_not_gain_auth_classification(tmp_path, change):
    entries = rows()
    row = entries[1]
    if change == 'code':
        row['error'] = 'synthetic_other_error'
    elif change == 'flag':
        row['isApiErrorMessage'] = False
    elif change == 'flag-int':
        row['isApiErrorMessage'] = 1
    elif change == 'role':
        row['message']['role'] = 'synthetic-other-role'
    elif change == 'stop':
        row['message']['stop_reason'] = 'synthetic-other-stop'
    elif change == 'block':
        row['message']['content'] = [dict(type='thinking', thinking='Synthetic thought.')]
    elif change == 'sidechain-missing':
        row.pop('isSidechain')
    elif change == 'sidechain-int':
        row['isSidechain'] = 0
    else:
        row['isSidechain'] = True
    assert claude.state(write_record(tmp_path / 'synthetic.jsonl', entries)).failure is None


def test_login_words_in_user_input_are_not_native_authentication(tmp_path):
    entries = rows()
    entries[0]['message']['content'] = 'Synthetic quotation: Please run /login; authentication_failed.'
    entries.pop(1)
    assert claude.state(write_record(tmp_path / 'synthetic.jsonl', entries)).failure is None


def test_new_turn_clears_previous_ended_auth_reading(tmp_path):
    entries = rows()
    entries.append(dict(type='user', timestamp='2001-02-03T04:06:00+00:00',
                        message=dict(role='user', content='Synthetic next task.')))
    state = claude.state(write_record(tmp_path / 'synthetic.jsonl', entries))
    assert state.turn_open is True
    assert state.failure is None
    assert state.completed_turns[-1].failure.kind == AUTHENTICATION


def test_error_without_terminal_duration_keeps_turn_open(tmp_path):
    state = claude.state(write_record(tmp_path / 'synthetic.jsonl', rows()[:-1]))
    assert state.turn_open is True
    assert state.failure is None
    assert state.completed_turns == ()


def test_standalone_error_is_visible_without_invented_completion(tmp_path):
    state = claude.state(write_record(tmp_path / 'synthetic.jsonl', [error_row()]))
    assert state.failure.kind == AUTHENTICATION
    assert state.completed_turns == ()
    assert state.last_input is None


@pytest.mark.parametrize('ending', ['duration', 'interrupted', 'inferred', 'other-api-error', 'no-output'])
def test_only_native_completed_normal_response_proves_success(tmp_path, ending):
    entries = rows()
    response = entries[1]
    response['isApiErrorMessage'] = False
    response.pop('error')
    response['message']['stop_reason'] = 'end_turn'
    response['message']['content'][0]['text'] = 'Synthetic completed response.'
    if ending in ('interrupted', 'inferred'):
        entries[-1] = dict(type='user', timestamp='2001-02-03T04:05:02+00:00', message=dict(
            role='user', content='[Request interrupted by user]' if ending == 'interrupted' else 'Synthetic next input.'))
    elif ending == 'other-api-error':
        response.update(isApiErrorMessage=True, error='synthetic_other_error')
    elif ending == 'no-output':
        entries.pop(1)
    state, turns = claude.read(write_record(tmp_path / 'synthetic.jsonl', entries))
    assert turns[0].successful is (True if ending == 'duration' else False if ending == 'other-api-error' else None)
    assert turns[0].failure is None
    if ending == 'inferred':
        assert state.completed_turns == ()
