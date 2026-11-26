"""The compact status can explain a pool-return hold while the key is working."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

from quarterdeck import headlines, service, watch
from quarterdeck.harness import resume_target
from tests.unit.test_cross_program_failover import running  # noqa: F401


def test_human_return_holds_its_launch_and_reports_a_safe_reason_in_compact_status(running, monkeypatch):
    r = running
    r.svc.failover()  # synthetic launch on the fallback
    r.seat.ship.sessions['crew'] = replace(r.seat.session, human=True)
    paid = SimpleNamespace(name='second', paid_by_key=True, kind='key')
    r.seat.ship.pools['second'] = paid
    before = r.seat.launched()
    clock = [100.0]
    monkeypatch.setattr(service, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(r.svc, 'known_open', lambda seat, pool: pool == 'first')
    monkeypatch.setattr(r.svc, 'resumes_on', lambda seat, pool: True)
    raw_reason = 'synthetic adapter detail that should stay out of status'
    move = Mock(side_effect=resume_target.Unproved(r.seat.name, raw_reason, launch=before))
    monkeypatch.setattr(r.seat, 'failover_to', move)
    monkeypatch.setattr(r.svc, '_fresh_unproved_move', Mock(side_effect=AssertionError('automatic fresh forbidden')))
    for _ in range(4):
        r.svc._back_from_key(r.seat)
        clock[0] += 2 * service.RELAUNCH_RETRY
    move.assert_called_once_with('first')
    r.svc._fresh_unproved_move.assert_not_called()
    assert r.seat.launched() == before and r.seat.is_up
    facts = watch.Facts(service='ok', waiting=0, oldest=0, blind=0,
                        seat_holds=watch.service_holds(r.svc.runtime),
                        routes=['synthetic route ' * 30])
    line = headlines.render(headlines.cache_document(facts), compact=True)
    assert 'worker: return to first held: conversation resume unproved; needs the desk' in line
    assert raw_reason not in line

    monkeypatch.setattr(r.svc, 'known_open', lambda *args: False)
    r.svc._back_from_key(r.seat)
    assert watch.service_holds(r.svc.runtime) == []


def test_crew_return_with_a_held_handoff_delays_further_attempts(running, monkeypatch):
    from quarterdeck import fresh_handoff, keyfile, post
    from quarterdeck.ship import Pool

    class StandInKeyPool(Pool):
        kind = 'key'
        paid_by_key = True

    r = running
    r.svc.failover()
    r.seat.ship.pools['second'] = StandInKeyPool('second', 'anthropic')
    monkeypatch.setattr(keyfile, 'usable', lambda pool: True)
    before = r.seat.launched()
    clock = [100.0]
    monkeypatch.setattr(service, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(r.svc, 'known_open', lambda seat, pool: pool == 'first')
    monkeypatch.setattr(r.svc, 'resumes_on', lambda seat, pool: True)
    reason = 'synthetic adapter detail that should stay out of status'
    move = Mock(side_effect=resume_target.Unproved(r.seat.name, reason, launch=before, pool='first'))
    monkeypatch.setattr(r.seat, 'failover_to', move)
    handoff = Mock(return_value=fresh_handoff.FreshHandoffResult('held', 'synthetic handoff failure detail'))
    monkeypatch.setattr(fresh_handoff, 'request', handoff)

    for _ in range(5):
        r.svc.failover()
    move.assert_called_once_with('first')
    handoff.assert_called_once_with(r.seat, reason=reason, expected_launch=before, service=r.svc,
                                    pool='first', immediate=False, return_from='second')
    facts = watch.Facts(service='ok', waiting=0, oldest=0, blind=0,
                        seat_holds=watch.service_holds(r.svc.runtime), routes=['synthetic route ' * 30])
    line = headlines.render(headlines.cache_document(facts), compact=True)
    assert 'worker: return to first held: conversation handoff held; retry delayed' in line
    assert 'synthetic handoff failure detail' not in line and reason not in line
    assert r.seat.is_up and r.seat.launched() == before
    assert not any('moved back' in m.body for m in r.svc.store.for_seat('worker') if m.sender == post.SHIP)

    clock[0] += service.RELAUNCH_RETRY
    for _ in range(5):
        r.svc.failover()
    assert move.call_count == handoff.call_count == 2
    monkeypatch.setattr(r.svc, 'known_open', lambda *args: False)
    r.svc.failover()
    assert watch.service_holds(r.svc.runtime) == []
