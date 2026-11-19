"""The real failover duty supplies status with evidence from each decision stage."""

from datetime import timedelta

import pytest

from quarterdeck import lifecycle, post, quota, trusted_files, watch
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


def test_selected_move_held_by_the_real_lifecycle_claim(scoped):
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
        with pytest.raises(lifecycle.Busy):
            r.svc.failover()
    held, unknown, decisions = watch.limit_state(r.seat.ship, [r.seat])
    assert (held, unknown) == ([], [])
    assert decisions == [
        'worker at limit (second fable / 7d) · move held for third: another lifecycle transition is in progress',
    ]
    assert r.seat.launched() == before and r.seat.pool.name == 'second'
    r.svc.failover()
    assert r.seat.pool.name == 'third'
    assert watch.limit_state(r.seat.ship, [r.seat]) == ([], [], [])
