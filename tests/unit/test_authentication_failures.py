"""Ordinary local metadata/recovery orchestration; native programs never start."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
import os

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


def test_valid_newer_file_consumes_one_resume_even_across_service_restart(tmp_path, monkeypatch):
    _, source, seat, state, service = environment(tmp_path, monkeypatch)
    auth_failure.run(service)
    service.relaunch.assert_called_once_with(seat, True)
    assert 'one relaunch with resume' in service.log.call_args.kwargs['proof']
    # Recreated service and a rewritten pool file still cannot retry this failed episode.
    source.write_text('synthetic second generation')
    other = SimpleNamespace(**vars(service))
    other.relaunch = Mock()
    auth_failure.run(other)
    other.relaunch.assert_not_called()
    other.needs_human.assert_called_with(seat)
    monkeypatch.setattr(auth_failure.turn_failure, 'current', Mock(return_value=state.failure))
    assert 'resume already attempted' in auth_failure.status(seat)


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
    service.needs_human.assert_called_once_with(seat)
    assert not auth_failure._path(seat).exists()
    monkeypatch.setattr(auth_failure.turn_failure, 'current', Mock(return_value=state.failure))
    if case.startswith('expired'):
        assert 'login needed' in auth_failure.status(seat)


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
    assert 'held by existing lifecycle checks' in service.log.call_args.kwargs['proof']


def test_failed_resume_call_stays_consumed(tmp_path, monkeypatch):
    _, _, seat, _, service = environment(tmp_path, monkeypatch)
    from quarterdeck.ship import ShipError
    service.relaunch.side_effect = ShipError('synthetic lifecycle failure')
    auth_failure.run(service)
    auth_failure.run(service)
    service.relaunch.assert_called_once_with(seat, True)
    assert auth_failure._path(seat).exists()
    assert 'sign-in recovery held' in service.log.call_args.kwargs['proof']


def test_opening_another_turn_does_not_clear_attempt_but_completed_success_does(tmp_path, monkeypatch):
    now, _, seat, state, service = environment(tmp_path, monkeypatch)
    auth_failure.run(service)
    state = replace(state, failure=None, turn_open=True)
    seat.harness.state.return_value = state
    auth_failure.run(service)
    assert auth_failure._path(seat).exists()
    seat.harness.state.return_value = replace(state, turn_open=False, completed_turns=(
        SimpleNamespace(timed=True, failure=None, ended=now + timedelta(hours=1)),))
    auth_failure.run(service)
    assert not auth_failure._path(seat).exists()


def test_status_line_names_authentication_with_no_open_order(tmp_path, monkeypatch):
    _, _, seat, state, _ = environment(tmp_path, monkeypatch, human=True)
    ship = seat.ship
    ship.workspace = tmp_path
    seat.spec = SimpleNamespace(pools=['synthetic-pool'])
    monkeypatch.setattr(auth_failure.turn_failure, 'current', Mock(return_value=state.failure))
    monkeypatch.setattr(watch, 'seats', lambda *a: [seat])
    monkeypatch.setattr(watch, 'limit_holds', lambda *a: ([], []))
    monkeypatch.setattr(watch, 'service_state', lambda *a: 'ok')
    monkeypatch.setattr(watch, 'headline_readings', lambda *a: [])
    monkeypatch.setattr(watch, 'disks', lambda *a: [])
    monkeypatch.setattr(watch, 'ledger_walls', lambda *a: [])
    monkeypatch.setattr(watch.credential, 'login_needed', lambda *a: [])
    monkeypatch.setattr(watch.recovery, 'held', lambda *a: None)
    from quarterdeck import compaction, compaction_alert, structure_outcome
    monkeypatch.setattr(compaction, 'status', lambda *a, **k: None)
    monkeypatch.setattr(compaction_alert, 'unrouted', lambda *a: [])
    monkeypatch.setattr(structure_outcome, 'waits', lambda *a: [])
    store = SimpleNamespace(open_for=lambda *a: [])
    attempts = SimpleNamespace(all=lambda: [], latest=lambda **k: {}, undelivered=lambda *a, **k: [])
    facts = watch.facts(ship, object(), store=store, attempts=attempts)
    assert seat.name in facts.needs_human
    assert AUTHENTICATION in facts.line()
    assert facts.waiting == 0


def test_failed_turn_after_attempt_names_failed_cure_and_alarms_systems_once(tmp_path, monkeypatch):
    now, _, seat, state, service = environment(tmp_path, monkeypatch)
    auth_failure.run(service)
    later = replace(state.failure, at=now + timedelta(minutes=1))
    monkeypatch.setattr(auth_failure.turn_failure, 'current', Mock(return_value=later))
    expected = AUTHENTICATION + '; relaunch did not cure it'
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
