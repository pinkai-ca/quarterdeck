"""Ordinary synthetic check outcomes, shared alarms and presentation."""

from dataclasses import replace
from datetime import datetime, timezone
import fcntl
import json
from types import SimpleNamespace as NS

import pytest

from quarterdeck import check, check_outcome, harness, service_status, ship, ship_locks, watch
from quarterdeck.harness import antigravity, claude, codex, grok_screen
from quarterdeck.harness.types import Screen
from quarterdeck.output import value


def outcome(runtime, harness, result, reason):
    check_outcome.publish(runtime, harness, check_outcome.event(result, 'synthetic-version', reason))
    return check_outcome.read(runtime, harness)


@pytest.fixture
def copied_ship(tmp_path):
    (tmp_path / 'ship.toml').write_text('[ship]\nname="synthetic"\ncode="checkout"\n')
    return ship.load(tmp_path)


@pytest.fixture
def check_time(monkeypatch):
    now = datetime(2026, 11, 17, 23, 30, tzinfo=timezone.utc)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    monkeypatch.setattr(check_outcome, 'datetime', Clock)
    return now.isoformat()


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


def test_old_untimed_unknown_is_read_without_inventing_time(tmp_path):
    data = outcome(tmp_path, 'fake', 'failed', 'file')
    old = dict(data['latest_attempt'], sequence=2, outcome='unknown', at=None, step=None, reason='interrupted')
    data['latest_attempt'] = old
    source = check_outcome.path(tmp_path, 'fake')
    stored = json.dumps(data)
    source.write_text(stored)
    rows, errors = check_outcome.collect(tmp_path)
    assert rows == [data] and errors == []
    assert check_outcome.unknown_alarms(rows) == ['fake check unknown: interrupted']
    assert source.read_text() == stored
    next_attempt = outcome(tmp_path, 'fake', 'unknown', 'incomplete')
    assert next_attempt['latest_attempt']['sequence'] == 3
    assert next_attempt['definitive'] == data['definitive']


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


@pytest.mark.parametrize(('program', 'interruption', 'reason'), [
    ('grok', grok_screen.TRAINING_QUESTION, 'account-training'),
    ('grok', grok_screen.TRAINING_ALTERED, 'account-training'),
    ('codex', codex.LOGIN_QUESTION, 'account-login'),
    ('codex', codex.APPROVAL_QUESTION, 'account-permission'),
    ('claude', claude.LOGIN_QUESTION, 'account-login'),
    ('claude', claude.PERMISSION_QUESTION, 'account-permission'),
    ('claude', claude.TRUST_QUESTION, 'account-trust'),
    ('antigravity', antigravity.LOGIN_QUESTION, 'account-login'),
    ('antigravity', antigravity.TRUST_QUESTION, 'account-trust'),
])
def test_account_holder_question_is_named_by_its_real_descriptor(tmp_path, monkeypatch, program, interruption, reason):
    door = NS(screen=lambda: Screen('unrelated synthetic display', box=None, interruption=interruption),
              reading=lambda: NS(open=False), harness=harness.get(program))
    monkeypatch.setattr(check, 'Door', lambda seat: door)
    monkeypatch.setattr(check, 'wait_for', lambda predicate, timeout: False)

    def stopped_probe(ship, name, tmux, harness, model):
        if name != 'grok':
            assert any(descriptor is interruption for descriptor in harness.interruptions())
        seat = NS(name=f'check-{name}-synthetic', harness=harness, up=lambda resume: 'synthetic launch',
                  is_up=True, launched=lambda: None)
        return check.probe(ship, seat), 'synthetic-version'

    monkeypatch.setattr(check, '_check', stopped_probe)
    check.check(NS(runtime=tmp_path), program, None)
    found, _ = check_outcome.collect(tmp_path)
    assert found[0]['definitive']['reason'] == reason
    assert check_outcome.REASONS[reason][1] in check_outcome.alarms(found)[0]
    stored = check_outcome.path(tmp_path, program).read_text()
    assert 'unrelated synthetic display' not in stored
    assert json.dumps(interruption.name) not in stored


def test_equal_looking_descriptor_cannot_name_an_account_question():
    duplicate = replace(grok_screen.TRAINING_ALTERED)
    assert duplicate == grok_screen.TRAINING_ALTERED and duplicate is not grok_screen.TRAINING_ALTERED
    door = NS(harness=harness.get('grok'), screen=lambda: Screen('synthetic', box=None, interruption=duplicate))
    assert check.account_question(door) is None
    door.harness = harness.get('fake')
    door.screen = lambda: Screen('synthetic', box=None, interruption=grok_screen.TRAINING_ALTERED)
    assert check.account_question(door) is None


@pytest.mark.parametrize(('error', 'legacy_reason', 'reason'), [
    (RuntimeError('synthetic private exception detail'), 'unclassified', 'check raised an exception'),
    (KeyboardInterrupt('synthetic private exception detail'), 'interrupted', 'interrupted'),
])
def test_thrown_check_is_named_once_and_clears_only_after_its_program_passes(
        copied_ship, monkeypatch, check_time, error, legacy_reason, reason):
    previous = outcome(copied_ship.runtime, 'fake', 'failed', 'file')['definitive']

    def fail(*args):
        raise error

    monkeypatch.setattr(check, '_check', fail)
    with pytest.raises(type(error)):
        check.check(copied_ship, 'fake', None)
    stamp = f'fake check unknown since {check_time}: {reason}'
    source = check_outcome.path(copied_ship.runtime, 'fake')
    assert 'synthetic private exception detail' not in source.read_text()
    assert 'synthetic private exception detail' not in check_outcome._observation_path(source).read_text()
    stored = check_outcome.read(copied_ship.runtime, 'fake')
    assert set(stored) == {'format', 'harness', 'latest_attempt', 'definitive'}
    assert stored['definitive'] == previous
    assert stored['latest_attempt'] == dict(sequence=3, outcome='unknown', at=None, version=None,
                                            step=None, reason=legacy_reason)
    facts = watch.facts(copied_ship, NS())
    assert facts.check_unknown == [stamp]
    assert facts.line().count(stamp) == 1 and 'fake failed its check' in facts.line()
    detail = watch.status_reading(copied_ship, NS(), collected=facts)
    assert watch.render_table(detail).count(stamp) == 1
    overflow = replace(facts, pending=['synthetic pending ' * 30])
    assert overflow.line().count(stamp) == 1 and 'details: qd status' in overflow.line()

    monkeypatch.setattr(check, '_check', lambda *args: ([check.ProbeCheck('complete', True, '')], 'synthetic-version'))
    check.check(copied_ship, 'codex', None)
    assert watch.facts(copied_ship, NS()).check_unknown == [stamp]
    check.check(copied_ship, 'fake', None)
    cleared = watch.facts(copied_ship, NS())
    assert cleared.check_unknown == [] and cleared.check_failed == []
    assert not check_outcome._observation_path(source).exists()


@pytest.mark.parametrize('mismatch', ['missing', 'same bytes replaced', 'invalid time', 'invalid classification'])
def test_unknown_observation_requires_exact_outcome_and_valid_metadata(copied_ship, check_time, mismatch):
    runtime = copied_ship.runtime
    observed = check_outcome.event('unknown', reason='unclassified')
    check_outcome.publish(runtime, 'fake', observed, classification='exception')
    source = check_outcome.path(runtime, 'fake')
    sidecar = check_outcome._observation_path(source)
    latest = check_outcome.read(runtime, 'fake')
    before = service_status.inputs(copied_ship)
    with check_outcome._lock(runtime, 'fake'):
        if mismatch == 'missing':
            sidecar.unlink()
        elif mismatch == 'same bytes replaced':
            check_outcome.durable.replace_file(source, source.read_text())
        else:
            metadata = json.loads(sidecar.read_text())
            if mismatch == 'invalid time':
                metadata['at'] = '2026-11-17T15:30:00-08:00'
            else:
                metadata['classification'] = 'synthetic unrecognized'
            check_outcome.durable.replace_file(sidecar, json.dumps(metadata))
    assert service_status.inputs(copied_ship) != before
    found, errors = check_outcome.collect(runtime)
    assert found == [latest] and errors == []
    assert check_outcome.unknown_alarms(found) == ['fake check unknown: completion could not be classified']


@pytest.mark.parametrize('malformed', [
    b'{not json',
    b'{"format": 1, "harness": "fake", "latest_attempt": [], "definitive": null}',
    b'x' * (check_outcome.MAX_BYTES + 1),
])
def test_malformed_program_outcome_is_visible_isolated_and_replaced_whole(
        copied_ship, monkeypatch, check_time, malformed):
    runtime = copied_ship.runtime
    outcome(runtime, 'codex', 'failed', 'door')
    source = check_outcome.path(runtime, 'fake')
    source.write_bytes(malformed)
    unknown = 'fake check unknown: evidence unavailable'
    facts = watch.facts(copied_ship, NS())
    assert facts.check_unknown == [unknown] and facts.line().count(unknown) == 1
    assert watch.render_table(watch.status_reading(copied_ship, NS(), collected=facts)).count(unknown) == 1
    monkeypatch.setattr(check, '_check', lambda *args: ([check.ProbeCheck('complete', True, '')], 'synthetic-version'))
    check.check(copied_ship, 'codex', None)
    peer = check_outcome.path(runtime, 'codex').read_bytes()
    assert source.read_bytes() == malformed
    assert watch.facts(copied_ship, NS()).check_unknown == [unknown]

    locked = ship_locks.path(runtime, 'check-outcomes', 'fake.lock')
    sidecar = check_outcome._observation_path(source)
    replace_file, changes, observed = check_outcome.durable.replace_file, [], []

    def under_publish_lock(path, text, **options):
        if path in (source, sidecar):
            with locked.open('r') as lock:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if path == source:
                changes.append((source.read_bytes(), text.encode()))
            else:
                observed.append(json.loads(text))
        return replace_file(path, text, **options)

    monkeypatch.setattr(check_outcome.durable, 'replace_file', under_publish_lock)
    check.check(copied_ship, 'fake', None)
    latest = dict(sequence=2, outcome='passed', at=check_time,
                  version='synthetic-version', step='complete', reason='passed')
    expected = (json.dumps(dict(format=1, harness='fake', latest_attempt=latest, definitive=latest),
                           sort_keys=True) + '\n').encode()
    assert len(changes) == 2 and changes[0][0] == malformed and changes[1] == (changes[0][1], expected)
    assert source.read_bytes() == expected and check_outcome.path(runtime, 'codex').read_bytes() == peer
    assert len(observed) == 1 and observed[0]['at'] == check_time and not sidecar.exists()
    assert watch.facts(copied_ship, NS()).check_unknown == []


def test_plain_status_and_json_include_the_same_failed_outcome(tmp_path):
    data = outcome(tmp_path, 'fake', 'failed', 'launch')
    status = dict(service='ok', seats=[], open_orders=[], programs=[], usage=[],
                  check_outcomes=[data], check_outcome_errors=[])
    assert check_outcome.alarms([data])[0] in watch.render_table(status)
    assert value(status)['check_outcomes'][0]['definitive']['reason'] == 'launch'
