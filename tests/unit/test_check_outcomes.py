"""Ordinary synthetic check outcomes, shared alarms and presentation."""

from types import SimpleNamespace as NS

import pytest

from quarterdeck import check, check_outcome, watch
from quarterdeck.output import value


def outcome(runtime, harness, result, reason):
    check_outcome.publish(runtime, harness, check_outcome.event(result, 'synthetic-version', reason))
    return check_outcome.read(runtime, harness)


def test_failed_check_records_meaningful_safe_reason_and_preserves_proven_mark(tmp_path, monkeypatch):
    mark = tmp_path / 'proven' / 'fake-synthetic-version'
    mark.parent.mkdir()
    mark.write_text('synthetic prior proof\n')
    monkeypatch.setattr(check, '_check', lambda *a: (
        [check.ProbeCheck('installed', False, 'synthetic private detail', 'installed')], 'synthetic-version'))
    found = check.check(NS(runtime=tmp_path), 'fake', None)
    data = check_outcome.read(tmp_path, 'fake')
    assert not found[0].passed
    assert data['definitive']['step'] == 'installed'
    assert data['definitive']['version'] == 'synthetic-version'
    assert data['definitive']['at'].endswith('+00:00')
    assert 'synthetic private detail' not in check_outcome.path(tmp_path, 'fake').read_text()
    assert mark.read_text() == 'synthetic prior proof\n'


def test_same_program_pass_clears_only_its_failure(tmp_path):
    outcome(tmp_path, 'fake', 'failed', 'door')
    outcome(tmp_path, 'codex', 'failed', 'paste')
    first = check_outcome.read(tmp_path, 'fake')['definitive']['sequence']
    data = outcome(tmp_path, 'fake', 'passed', 'passed')
    assert data['definitive']['sequence'] > first
    found, errors = check_outcome.collect(tmp_path)
    alarms = check_outcome.alarms(found)
    assert errors == []
    assert len(alarms) == 1 and alarms[0].startswith('codex failed its check at ')


def test_unknown_completion_does_not_clear_a_definitive_failure(tmp_path):
    data = outcome(tmp_path, 'fake', 'failed', 'file')
    failed = data['definitive']
    data = outcome(tmp_path, 'fake', 'unknown', 'incomplete')
    assert data['definitive'] == failed
    assert data['latest_attempt']['at'] is None
    assert data['latest_attempt']['step'] is None
    assert data['latest_attempt']['sequence'] > failed['sequence']
    assert len(check_outcome.alarms([data])) == 1


def test_absent_outcome_is_unknown_without_new_files(tmp_path):
    assert check_outcome.collect(tmp_path) == ([], [])
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('program', ['fake', 'codex'])
def test_alarm_shared_by_every_session_and_json(tmp_path, program):
    outcome(tmp_path, program, 'failed', 'report')
    data, _ = check_outcome.collect(tmp_path)
    alarm = check_outcome.alarms(data)[0]
    sessions = [watch.Facts('ok', 0, 0, 0, check_failed=[alarm]) for _ in ('studio', 'crew')]
    assert all(alarm in facts.line() for facts in sessions)
    assert all(value(facts)['check_failed'] == [alarm] for facts in sessions)


def test_overflow_keeps_program_time_and_reason(tmp_path):
    data = outcome(tmp_path, 'fake', 'failed', 'record')
    alarm = check_outcome.alarms([data])[0]
    facts = watch.Facts('ok', 0, 0, 0, pending=['synthetic pending ' * 30], check_failed=[alarm])
    assert alarm in facts.line()
    assert 'details: qd status' in facts.line()


def test_account_holder_question_is_named(tmp_path, monkeypatch):
    door = NS(screen=lambda: NS(interruption=NS(name='login')))
    reason = check.account_question(door)
    monkeypatch.setattr(check, '_check', lambda *a: (
        [check.ProbeCheck('door open', False, '', 'door'),
         check.ProbeCheck('account-holder question', False, '', reason)], 'synthetic-version'))
    check.check(NS(runtime=tmp_path), 'fake', None)
    found, _ = check_outcome.collect(tmp_path)
    assert 'account-holder question: login' in check_outcome.alarms(found)[0]


def test_plain_status_and_json_include_the_same_failed_outcome(tmp_path):
    data = outcome(tmp_path, 'fake', 'failed', 'launch')
    status = dict(service='ok', seats=[], open_orders=[], programs=[], usage=[],
                  check_outcomes=[data], check_outcome_errors=[])
    assert check_outcome.alarms([data])[0] in watch.render_table(status)
    assert value(status)['check_outcomes'][0]['definitive']['reason'] == 'launch'
