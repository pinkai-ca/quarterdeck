"""Ordinary local metadata/recovery orchestration; native programs never start."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
import os
import json

import pytest

from quarterdeck import auth_failure, watch
from quarterdeck.harness.types import AUTHENTICATION, State, TurnFailure


def environment(tmp_path, monkeypatch, *, human=False):
    now = datetime.now(timezone.utc)
    source = tmp_path / 'synthetic-grant'
    source.write_text('synthetic local metadata stand-in')
    pool = SimpleNamespace(name='synthetic-pool')
    launched = SimpleNamespace(pool=pool.name, at=(now - timedelta(hours=1)).isoformat())
    ship = SimpleNamespace(runtime=tmp_path / 'runtime', pools={pool.name: pool})
    failure = TurnFailure(AUTHENTICATION, now, kind=AUTHENTICATION)
    state = State(turn_open=False, failure=failure)
    seat = SimpleNamespace(name='synthetic-worker', ship=ship, session=SimpleNamespace(human=human),
                           launched=Mock(return_value=launched), is_up=True,
                           record=Mock(return_value=tmp_path / 'synthetic-record'),
                           harness=SimpleNamespace(state=Mock(return_value=state)))
    monkeypatch.setattr(auth_failure.credential, 'pool_file', Mock(return_value=source))
    monkeypatch.setattr(auth_failure.credential, 'freshness', Mock(return_value=(now.timestamp() + 3600, source.stat().st_mtime)))
    monkeypatch.setattr(auth_failure.credential, 'grant_expiry', Mock(return_value=now + timedelta(days=7)))
    service = SimpleNamespace(resolved_seats=lambda: [seat], relaunch=Mock(return_value='synthetic resumed proof'),
                              log=Mock(), needs_human=Mock())
    return now, source, seat, state, service


def clock(monkeypatch, now):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)
    monkeypatch.setattr(auth_failure, 'datetime', Clock)


def test_changed_generation_retries_after_spacing_across_service_restart(tmp_path, monkeypatch):
    now, source, seat, state, service = environment(tmp_path, monkeypatch)
    clock(monkeypatch, now)
    auth_failure.run(service)
    service.relaunch.assert_called_once_with(seat, True)
    assert 'relaunch with resume (1/3)' in service.log.call_args.kwargs['proof']
    first = auth_failure._attempt(seat)['attempts'][0]
    # A resume updates launch time. Later retries compare with the attempt, not that launch.
    seat.launched().at = (now + timedelta(seconds=100)).isoformat()
    source.write_text('synthetic second generation')
    other = SimpleNamespace(**vars(service))
    other.relaunch = Mock()
    clock(monkeypatch, now + timedelta(seconds=59))
    auth_failure.run(other)
    other.relaunch.assert_not_called()
    clock(monkeypatch, now + timedelta(seconds=60))
    auth_failure.run(other)
    other.relaunch.assert_called_once_with(seat, True)
    attempts = auth_failure._attempt(seat)['attempts']
    assert len(attempts) == 2 and attempts[0] == first
    assert attempts[0]['credential'] != attempts[1]['credential']
    other.needs_human.assert_not_called()
    monkeypatch.setattr(auth_failure.turn_failure, 'current', Mock(return_value=state.failure))
    assert auth_failure.waiting(auth_failure.status(seat))


@pytest.mark.parametrize('case', ['expired-access', 'expired-grant', 'unknown', 'older', 'same-time', 'missing-launch', 'nonfinite', 'changing'])
def test_ineligible_local_evidence_holds_without_consuming_attempt(tmp_path, monkeypatch, case):
    now, source, seat, state, service = environment(tmp_path, monkeypatch)
    if case == 'expired-access':
        auth_failure.credential.freshness.return_value = (now.timestamp() - 1, source.stat().st_mtime)
    elif case == 'expired-grant':
        auth_failure.credential.grant_expiry.return_value = now - timedelta(seconds=1)
    elif case == 'unknown':
        auth_failure.credential.freshness.return_value = None
    elif case in ('older', 'same-time'):
        at = datetime.fromisoformat(seat.launched().at).timestamp()
        os.utime(source, (at, at if case == 'same-time' else at - 1))
    elif case == 'missing-launch':
        seat.launched.return_value = None
    elif case == 'changing':
        def refresh_during_observation(pool, path):
            path.write_text('synthetic metadata changed during observation, a different size')
            return now.timestamp() + 3600, path.stat().st_mtime
        auth_failure.credential.freshness.side_effect = refresh_during_observation
    else:
        auth_failure.credential.freshness.return_value = (float('inf'), source.stat().st_mtime)
    auth_failure.run(service)
    service.relaunch.assert_not_called()
    assert not auth_failure._path(seat).exists()
    monkeypatch.setattr(auth_failure.turn_failure, 'current', Mock(return_value=state.failure))
    if case in ('older', 'same-time'):
        service.needs_human.assert_not_called()
        assert auth_failure.waiting(auth_failure.status(seat))
    else:
        service.needs_human.assert_called_once_with(seat)
        assert not auth_failure.waiting(auth_failure.status(seat))


def test_human_seat_is_only_named_and_has_no_recovery_mutation(tmp_path, monkeypatch):
    _, _, seat, state, service = environment(tmp_path, monkeypatch, human=True)
    seat.record.side_effect = AssertionError('automatic recovery must not read a human conversation')
    auth_failure.run(service)
    seat.record.assert_not_called()
    service.relaunch.assert_not_called()
    service.log.assert_not_called()
    assert not auth_failure._path(seat).exists()
    monkeypatch.setattr(auth_failure.turn_failure, 'current', Mock(return_value=state.failure))
    assert 'human session needs attention' in auth_failure.status(seat)


def test_held_lifecycle_check_is_not_retried(tmp_path, monkeypatch):
    _, _, seat, _, service = environment(tmp_path, monkeypatch)
    service.relaunch.return_value = None
    auth_failure.run(service)
    auth_failure.run(service)
    service.relaunch.assert_called_once_with(seat, True)
    assert 'held by an unnamed lifecycle check' in service.log.call_args.kwargs['proof']


def test_failed_resume_call_stays_consumed(tmp_path, monkeypatch):
    _, _, seat, _, service = environment(tmp_path, monkeypatch)
    from quarterdeck.ship import ShipError
    service.relaunch.side_effect = ShipError('synthetic lifecycle failure')
    auth_failure.run(service)
    auth_failure.run(service)
    service.relaunch.assert_called_once_with(seat, True)
    assert auth_failure._path(seat).exists()
    assert 'sign-in recovery held' in service.log.call_args.kwargs['proof']


def test_attempt_directory_and_complete_record_are_published_without_sync_before_resume(tmp_path, monkeypatch):
    _, _, seat, _, service = environment(tmp_path, monkeypatch)
    def forbidden_sync(_):
        pytest.fail('ordinary authentication record forced a sync')
    monkeypatch.setattr(os, 'fsync', forbidden_sync)
    monkeypatch.setattr(os, 'fdatasync', forbidden_sync)
    def resume(subject, resume):
        path = auth_failure._path(subject)
        assert resume is True
        assert path.is_file() and path.parent.is_dir()
        assert auth_failure._attempt(subject) is not None
        return 'synthetic durable resume proof'
    service.relaunch.side_effect = resume
    auth_failure.run(service)
    service.relaunch.assert_called_once_with(seat, True)


def test_opening_another_turn_does_not_clear_attempt_but_completed_success_does(tmp_path, monkeypatch):
    now, _, seat, state, service = environment(tmp_path, monkeypatch)
    auth_failure.run(service)
    state = replace(state, failure=None, turn_open=True)
    seat.harness.state.return_value = state
    auth_failure.run(service)
    assert auth_failure._path(seat).exists()
    completed = SimpleNamespace(timed=True, successful=None, failure=None, ended=now + timedelta(hours=1))
    seat.harness.state.return_value = replace(state, turn_open=False, completed_turns=(completed,))
    auth_failure.run(service)
    assert auth_failure._path(seat).exists()  # completion without successful-auth evidence is insufficient
    completed.successful = True
    auth_failure.run(service)
    assert not auth_failure._path(seat).exists()


@pytest.mark.parametrize('human', [True, False])
def test_status_line_names_authentication_with_no_open_order(tmp_path, monkeypatch, human):
    _, _, seat, state, _ = environment(tmp_path, monkeypatch, human=human)
    seat.harness.reads_queue_error = False
    monkeypatch.setattr(watch, 'Door', lambda s: SimpleNamespace(human_obstruction=lambda: (watch.Reading(False, AUTHENTICATION, 'needs a human'), AUTHENTICATION)))
    ship = seat.ship
    ship.workspace = tmp_path
    seat.spec = SimpleNamespace(pools=['synthetic-pool'])
    monkeypatch.setattr(auth_failure.turn_failure, 'current', Mock(return_value=state.failure))
    monkeypatch.setattr(watch, 'seats', lambda *a: [seat])
    monkeypatch.setattr(watch, 'limit_state', lambda *a: ([], [], []))
    monkeypatch.setattr(watch, 'service_state', lambda *a: 'ok')
    monkeypatch.setattr(watch, 'headline_readings', lambda *a: [])
    monkeypatch.setattr(watch, 'disks', lambda *a: [])
    monkeypatch.setattr(watch, 'ledger_walls', lambda *a: [])
    monkeypatch.setattr(watch.credential, 'login_needed', lambda *a: [])
    monkeypatch.setattr(watch.recovery, 'held', lambda *a: None)
    from quarterdeck import compaction, compaction_alert, fleet_post, model_mismatch, structure_outcome
    monkeypatch.setattr(fleet_post, 'status', lambda *a: [])
    monkeypatch.setattr(compaction, 'status', lambda *a, **k: None)
    monkeypatch.setattr(compaction_alert, 'unrouted', lambda *a: [])
    monkeypatch.setattr(model_mismatch, 'unrouted', lambda *args, **kwargs: [])
    monkeypatch.setattr(structure_outcome, 'waits', lambda *a: [])
    store = SimpleNamespace(open_for=lambda *a: [])
    attempts = SimpleNamespace(all=lambda: [], latest=lambda **k: {}, undelivered=lambda *a, **k: [])
    facts = watch.facts(ship, object(), store=store, attempts=attempts)
    assert (seat.name in facts.needs_human) is human
    assert AUTHENTICATION in facts.line()
    if not human:
        assert auth_failure.WAITING in facts.line()
        assert f'{seat.name} needs a human' not in facts.line()
    assert facts.waiting == 0


def test_failed_turn_after_attempt_names_failed_cure_and_alarms_systems_once(tmp_path, monkeypatch):
    now, _, seat, state, service = environment(tmp_path, monkeypatch)
    auth_failure.run(service)
    later = replace(state.failure, at=now + timedelta(minutes=1))
    monkeypatch.setattr(auth_failure.turn_failure, 'current', Mock(return_value=later))
    clock(monkeypatch, now + timedelta(minutes=15, seconds=1))
    expected = AUTHENTICATION + '; relaunch did not cure it; needs attention'
    assert auth_failure.status(seat) == expected
    from quarterdeck import service as service_module
    monkeypatch.setattr(service_module, 'Door', lambda s: SimpleNamespace(human_obstruction=lambda: (None, None)))
    systems = SimpleNamespace(name='synthetic-systems', role='systems')
    desk = SimpleNamespace(name='synthetic-desk', role='desk')
    seat.ship.seats = {systems.name: systems, desk.name: desk}
    alarm = service_module.Service.__new__(service_module.Service)
    alarm.ship = seat.ship
    alarm.told, alarm.alarmed = {}, {}
    alarm._up = lambda name: True
    alarm.store = SimpleNamespace(new=Mock())
    alarm.needs_human(seat)
    alarm.needs_human(seat)
    alarm.store.new.assert_any_call('report', 'ship', systems.name, f'{seat.name} obstructed, {expected}')
    alarm.store.new.assert_any_call('report', 'ship', desk.name, f'{seat.name} obstructed, {expected}')
    assert alarm.store.new.call_count == 2


@pytest.mark.parametrize('seconds', [60, 899, 900])
def test_unchanged_generation_waits_until_window_ends(tmp_path, monkeypatch, seconds):
    now, _, seat, state, service = environment(tmp_path, monkeypatch)
    clock(monkeypatch, now)
    auth_failure.run(service)
    before = auth_failure._path(seat).read_bytes()
    clock(monkeypatch, now + timedelta(seconds=seconds))
    auth_failure.run(service)
    service.relaunch.assert_called_once()
    assert auth_failure._path(seat).read_bytes() == before
    monkeypatch.setattr(auth_failure.turn_failure, 'current', lambda s: state.failure)
    if seconds < 900:
        service.needs_human.assert_not_called()
        assert auth_failure.waiting(auth_failure.status(seat))
    else:
        service.needs_human.assert_called_once_with(seat)
        assert 'needs attention' in auth_failure.status(seat)


def test_three_attempts_then_attention_even_with_another_generation(tmp_path, monkeypatch):
    now, source, seat, state, service = environment(tmp_path, monkeypatch)
    for i in range(4):
        clock(monkeypatch, now + timedelta(seconds=60 * i))
        source.write_text('synthetic generation ' + str(i))
        seat.harness.state.return_value = replace(state, failure=replace(state.failure, at=now + timedelta(seconds=60 * i - 1)))
        auth_failure.run(service)
    assert service.relaunch.call_count == 3
    assert len(auth_failure._attempt(seat)['attempts']) == 3
    service.needs_human.assert_called_once_with(seat)


def test_old_journal_reads_and_gains_history_on_retry(tmp_path, monkeypatch):
    now, source, seat, _, service = environment(tmp_path, monkeypatch)
    clock(monkeypatch, now)
    auth_failure.run(service)
    path = auth_failure._path(seat)
    data = json.loads(path.read_text())
    old = data.pop('attempts')[0]
    path.write_text(json.dumps(data))
    assert auth_failure._attempt(seat)['attempts'] == [old]
    source.write_text('synthetic renewed generation')
    clock(monkeypatch, now + timedelta(seconds=60))
    auth_failure.run(service)
    assert service.relaunch.call_count == 2
    assert auth_failure._attempt(seat)['attempts'][0] == old


def test_success_must_follow_the_latest_attempt(tmp_path, monkeypatch):
    now, source, seat, state, service = environment(tmp_path, monkeypatch)
    clock(monkeypatch, now)
    auth_failure.run(service)
    source.write_text('synthetic renewed generation')
    clock(monkeypatch, now + timedelta(seconds=60))
    auth_failure.run(service)
    completed = SimpleNamespace(timed=True, successful=True, failure=None, ended=now + timedelta(seconds=30))
    seat.harness.state.return_value = replace(state, failure=None, completed_turns=(completed,))
    auth_failure.run(service)
    assert auth_failure._path(seat).exists()
    completed.ended = now + timedelta(seconds=61)
    auth_failure.run(service)
    assert not auth_failure._path(seat).exists()


def test_newer_comparison_uses_aware_launch_time(tmp_path, monkeypatch):
    now, source, seat, _, _ = environment(tmp_path, monkeypatch)
    # Invented date, incident's wall-clock times and equivalent timezone spellings.
    seat.launched().at = '2025-01-02T09:43:57-07:00'
    renewed = datetime.fromisoformat('2025-01-02T10:05:22-07:00').timestamp()
    os.utime(source, (renewed, renewed))
    assert auth_failure.pool_observation(seat, now)[2] is True
    seat.launched().at = '2025-01-02T16:43:57+00:00'
    assert auth_failure.pool_observation(seat, now)[2] is True


def test_initial_wait_expires_without_attempting_an_unchanged_file(tmp_path, monkeypatch):
    now, source, seat, state, service = environment(tmp_path, monkeypatch)
    at = datetime.fromisoformat(seat.launched().at).timestamp()
    os.utime(source, (at, at))
    clock(monkeypatch, now + timedelta(seconds=899))
    auth_failure.run(service)
    service.needs_human.assert_not_called()
    clock(monkeypatch, now + timedelta(seconds=900))
    auth_failure.run(service)
    service.needs_human.assert_called_once_with(seat)
    service.relaunch.assert_not_called()


def test_renewal_wait_does_not_alarm_then_window_end_does(tmp_path, monkeypatch):
    now, _, seat, state, service = environment(tmp_path, monkeypatch)
    clock(monkeypatch, now)
    auth_failure.run(service)
    monkeypatch.setattr(auth_failure.turn_failure, 'current', lambda s: state.failure)
    from quarterdeck import service as service_module
    monkeypatch.setattr(service_module, 'Door', lambda s: SimpleNamespace(human_obstruction=lambda: (None, AUTHENTICATION)))
    alarm = service_module.Service.__new__(service_module.Service)
    alarm.told, alarm.alarmed = {}, {}
    alarm.alarm_recipients = Mock(return_value=['synthetic-systems'])
    alarm.store = SimpleNamespace(new=Mock())
    alarm.needs_human(seat)
    alarm.store.new.assert_not_called()
    clock(monkeypatch, now + timedelta(minutes=15))
    alarm.needs_human(seat)
    alarm.store.new.assert_called_once()


def test_last_resume_waits_for_next_turn_before_exhaustion_alarm(tmp_path, monkeypatch):
    now, source, seat, state, service = environment(tmp_path, monkeypatch)
    for i in range(3):
        clock(monkeypatch, now + timedelta(seconds=60 * i))
        source.write_text('synthetic generation ' + str(i))
        auth_failure.run(service)
    monkeypatch.setattr(auth_failure.turn_failure, 'current', lambda s: state.failure)
    assert auth_failure.waiting(auth_failure.status(seat))
    service.needs_human.assert_not_called()
    failed = replace(state.failure, at=now + timedelta(seconds=121))
    monkeypatch.setattr(auth_failure.turn_failure, 'current', lambda s: failed)
    assert 'needs attention' in auth_failure.status(seat)


@pytest.mark.parametrize('validity', ['expired', 'unknown'])
def test_changed_but_unproved_generation_needs_attention_without_retry(tmp_path, monkeypatch, validity):
    now, source, seat, state, service = environment(tmp_path, monkeypatch)
    clock(monkeypatch, now)
    auth_failure.run(service)
    source.write_text('synthetic changed generation')
    clock(monkeypatch, now + timedelta(seconds=60))
    auth_failure.credential.freshness.return_value = None if validity == 'unknown' else (now.timestamp() - 1, source.stat().st_mtime)
    auth_failure.run(service)
    service.relaunch.assert_called_once()
    service.needs_human.assert_called_once_with(seat)


def test_changed_generation_cannot_retry_at_window_end(tmp_path, monkeypatch):
    now, source, seat, _, service = environment(tmp_path, monkeypatch)
    clock(monkeypatch, now)
    auth_failure.run(service)
    source.write_text('synthetic changed generation')
    clock(monkeypatch, now + timedelta(minutes=15))
    auth_failure.run(service)
    service.relaunch.assert_called_once()
    service.needs_human.assert_called_once_with(seat)


def test_down_seat_keeps_attention_instead_of_waiting_forever(tmp_path, monkeypatch):
    _, _, seat, state, service = environment(tmp_path, monkeypatch)
    seat.is_up = False
    auth_failure.run(service)
    service.relaunch.assert_not_called()
    service.needs_human.assert_called_once_with(seat)
    monkeypatch.setattr(auth_failure.turn_failure, 'current', lambda s: state.failure)
    assert 'needs attention' in auth_failure.status(seat)


def test_expired_before_first_attempt_calls_person_immediately(tmp_path, monkeypatch):
    now, source, seat, state, service = environment(tmp_path, monkeypatch)
    clock(monkeypatch, now)
    auth_failure.credential.freshness.return_value = (now.timestamp() - 1, source.stat().st_mtime)
    auth_failure.run(service)
    service.needs_human.assert_called_once_with(seat)
    service.relaunch.assert_not_called()
    assert not auth_failure._path(seat).exists()
    monkeypatch.setattr(auth_failure.turn_failure, 'current', lambda _: state.failure)
    assert 'pool sign-in expired' in auth_failure.status(seat)
    assert not auth_failure.waiting(auth_failure.status(seat))


def test_unknown_between_attempts_calls_person_immediately(tmp_path, monkeypatch):
    now, _, seat, state, service = environment(tmp_path, monkeypatch)
    clock(monkeypatch, now)
    auth_failure.run(service)
    before = auth_failure._path(seat).read_bytes()
    auth_failure.credential.freshness.return_value = None
    clock(monkeypatch, now + timedelta(seconds=1))
    auth_failure.run(service)
    service.relaunch.assert_called_once_with(seat, True)
    service.needs_human.assert_called_once_with(seat)
    assert auth_failure._path(seat).read_bytes() == before
    monkeypatch.setattr(auth_failure.turn_failure, 'current', lambda _: state.failure)
    assert 'validity unknown' in auth_failure.status(seat)
    assert not auth_failure.waiting(auth_failure.status(seat))


def real_alarm(monkeypatch, state):
    """The service's own needs_human, its routing seams stubbed: what it would post is what it alarms."""
    from quarterdeck import service as service_module
    monkeypatch.setattr(auth_failure.turn_failure, 'current', lambda s: state.failure)
    monkeypatch.setattr(service_module, 'Door', lambda s: SimpleNamespace(human_obstruction=lambda: (None, AUTHENTICATION)))
    alarm = service_module.Service.__new__(service_module.Service)
    alarm.told, alarm.alarmed = {}, {}
    alarm.alarm_recipients = Mock(return_value=['synthetic-systems'])
    alarm.store = SimpleNamespace(new=Mock())
    return alarm


def held_relaunch(service):
    def relaunch(seat, resume):
        service.request_wait = ('recovery', 'a synthetic recovery hold')
        return None
    return relaunch


@pytest.mark.parametrize('how', ['held', 'raised'])
def test_a_relaunch_held_or_raised_alarms_at_once_with_its_reason(tmp_path, monkeypatch, how):
    """A consumed attempt whose relaunch did not start is a failure, never the wait for the renewed pass."""
    from quarterdeck.ship import ShipError
    now, _, seat, state, service = environment(tmp_path, monkeypatch)
    clock(monkeypatch, now)
    alarm = real_alarm(monkeypatch, state)
    service.needs_human = alarm.needs_human
    if how == 'held':
        service.relaunch = Mock(side_effect=held_relaunch(service))
        reason = 'held by the recovery hold: a synthetic recovery hold'
    else:
        service.relaunch = Mock(side_effect=ShipError('synthetic lifecycle failure'))
        reason = 'raised ShipError'
    auth_failure.run(service)
    service.relaunch.assert_called_once_with(seat, True)
    expected = f'{AUTHENTICATION}; sign-in resume {reason}; needs attention'
    assert auth_failure.status(seat) == expected and not auth_failure.waiting(auth_failure.status(seat))
    alarm.alarm_recipients.assert_called_once()
    alarm.store.new.assert_called_once_with('report', 'ship', 'synthetic-systems', f'{seat.name} obstructed, {expected}')
    clock(monkeypatch, now + timedelta(seconds=90))
    auth_failure.run(service)  # the failed attempt stays consumed, and the standing alarm is not sent twice
    service.relaunch.assert_called_once_with(seat, True)
    alarm.store.new.assert_called_once()
    assert 'synthetic lifecycle failure' not in json.dumps(auth_failure._attempt(seat))  # the error's kind only


def test_a_started_relaunch_still_waits_for_the_renewed_pass(tmp_path, monkeypatch):
    now, _, seat, state, service = environment(tmp_path, monkeypatch)
    clock(monkeypatch, now)
    alarm = real_alarm(monkeypatch, state)
    service.needs_human = Mock(side_effect=alarm.needs_human)
    auth_failure.run(service)
    service.relaunch.assert_called_once_with(seat, True)
    service.needs_human.assert_not_called()
    assert auth_failure.waiting(auth_failure.status(seat))
    alarm.needs_human(seat)  # a reading in the window: no alarm while the started relaunch waits
    alarm.store.new.assert_not_called()


def test_a_started_relaunch_whose_log_write_fails_is_not_a_failed_relaunch(tmp_path, monkeypatch):
    """The relaunch has returned a proof: a failure of the log line after it is that, never a failed relaunch."""
    now, _, seat, state, service = environment(tmp_path, monkeypatch)
    clock(monkeypatch, now)
    alarm = real_alarm(monkeypatch, state)
    service.needs_human = alarm.needs_human
    written = []

    def log(kind, **fields):
        if not written:
            written.append(fields)
            raise OSError('synthetic log failure')
        written.append(fields)
    service.log = Mock(side_effect=log)
    auth_failure.run(service)
    service.relaunch.assert_called_once_with(seat, True)
    assert auth_failure.failed(auth_failure._attempt(seat)) is None, 'the relaunch started: no failure recorded'
    assert 'held by the relaunch log (OSError)' in written[-1]['proof'], 'its own step, not the relaunch'
    assert auth_failure.waiting(auth_failure.status(seat)), 'it still waits for the renewed pass'
    alarm.store.new.assert_not_called()
