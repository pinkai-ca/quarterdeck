"""The real failover duty supplies status with evidence from each decision stage."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from quarterdeck import lifecycle, post, quota, service, trusted_files, watch
from quarterdeck.harness.limits import Usage, Window

from .test_claude_model_allowances import model, scoped, windows  # noqa: F401
from .test_cross_program_failover import running  # noqa: F401


def test_cached_room_does_not_turn_unreadable_source_into_a_move(scoped, monkeypatch):
    r = scoped
    model(r, 'claude-fable-5-2')
    at = post.now()
    r.svc.ledger.record_usage('second', Usage((
        Window('5h', .8, at + timedelta(hours=5)),
        Window('7d', 1, at + timedelta(days=7)),
        Window('fable / 7d', .73, at + timedelta(days=7), group='fable'),
    ), at))
    windows(r, 'third')
    before = r.seat.launched()
    assert quota.cached_open(r.seat.ship, r.svc.ledger, 'third', r.seat)

    def unreadable(*args):
        raise OSError('made record could not be read')

    def no_selection(*args):
        pytest.fail('record guard must run before destination eligibility')

    monkeypatch.setattr(r.seat.harness, 'state', unreadable)
    monkeypatch.setattr(r.svc, 'known_open', no_selection)
    r.svc.failover()
    monkeypatch.setattr(r.seat, 'record', lambda: pytest.fail('status must not publish a record pin'))
    held, unknown, decisions = watch.limit_state(r.seat.ship, [r.seat])
    assert (held, unknown) == ([], [])
    assert decisions == ['worker at limit (second 7d) · record unreadable: no move weighed']
    assert r.seat.launched() == before and r.seat.pool.name == 'second'


def test_selected_move_held_by_the_real_lifecycle_claim(scoped, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(service, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    r = scoped
    model(r, 'claude-fable-5-2')
    windows(r, 'second', fable=1)
    windows(r, 'third')
    before = r.seat.launched()
    pin = r.seat.trusted / trusted_files.RECORD
    assert not pin.exists()
    assert watch.limit_state(r.seat.ship, [r.seat]) == ([], [], ['worker: move proof unknown'])
    assert not pin.exists()
    with lifecycle.claim(r.svc.runtime, r.seat.name):
        r.svc.failover()
        r.svc.failover()
    held, unknown, decisions = watch.limit_state(r.seat.ship, [r.seat])
    assert (held, unknown) == ([], [])
    assert decisions == [
        'worker at limit (second fable / 7d) · move held for third: another lifecycle transition is in progress',
    ]
    assert r.seat.launched() == before and r.seat.pool.name == 'second'
    r.svc.failover()
    assert r.seat.launched() == before, 'releasing the claim does not bypass the retry delay'
    clock[0] += service.RELAUNCH_RETRY
    r.svc.failover()
    assert r.seat.pool.name == 'third'
    assert watch.service_holds(r.svc.runtime) == []
    assert watch.limit_state(r.seat.ship, [r.seat]) == ([], [], [])


def test_failed_move_delays_only_that_seat_and_clears_after_retry(running, monkeypatch):
    from copy import copy
    from unittest.mock import Mock

    r = running
    first = r.seat
    sibling = copy(first)
    sibling.spec = replace(sibling.spec, name='sibling')
    # Keep the sibling's idle reading independent of the failing launch.
    monkeypatch.setattr(r.svc, 'resolved_seats', lambda: [first, sibling])
    monkeypatch.setattr(r.svc, '_wall_idle_hold', lambda seat: None)
    clock = [100.0]
    monkeypatch.setattr(service, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    move = first.failover_to
    calls = Mock(side_effect=[OSError('synthetic launch refused before retirement'), 'second attempt'])
    sibling_calls = Mock(return_value=None)

    def first_move(target):
        outcome = calls(target)
        if outcome:
            return move(target)

    monkeypatch.setattr(first, 'failover_to', first_move)
    monkeypatch.setattr(sibling, 'failover_to', sibling_calls)
    for _ in range(5):
        r.svc.failover()
    assert calls.call_count == 1
    assert sibling_calls.call_count == 5
    assert watch.service_holds(r.svc.runtime) == ['worker: move to second held: could not finish (OSError); retry delayed']
    clock[0] += service.RELAUNCH_RETRY
    r.svc.failover()
    assert first.pool.name == 'second' and calls.call_count == 2
    assert watch.service_holds(r.svc.runtime) == []


def test_pool_move_leaves_a_failed_intent_authoritative(running, monkeypatch):
    from unittest.mock import Mock
    from quarterdeck import relaunch_intent, shutdown

    r = running
    controller = shutdown.Stop(r.svc.runtime)
    controller.owner = {'pid': 101, 'start': 'synthetic service start'}
    monkeypatch.setattr(shutdown, '_controller', controller)

    def attempted(target):
        relaunch_intent.record_destination(r.seat, True, pool=target)
        raise OSError('synthetic uncertain launch')

    move = Mock(side_effect=attempted)
    monkeypatch.setattr(r.seat, 'failover_to', move)
    r.svc.failover()
    path = relaunch_intent.path(r.svc.runtime, r.seat.name)
    intent = relaunch_intent.read(r.svc.runtime, path.name, {'worker'})
    assert intent['phase'] == relaunch_intent.FAILED
    assert intent['destination']['pool'] == 'second'
    assert 'automatic transition outcome unknown' in relaunch_intent.status(r.seat.ship)[0]
    before = path.read_bytes()
    monkeypatch.setattr(shutdown, '_controller', None)
    monkeypatch.setattr(relaunch_intent, '_gone', lambda owner: True)
    for _ in range(5):
        r.svc.relaunch_intents()
        r.svc.failover()
    assert move.call_count == 1
    assert path.read_bytes() == before, 'the surviving old launch does not resolve a failed intent'
    assert watch.service_holds(r.svc.runtime) == []
