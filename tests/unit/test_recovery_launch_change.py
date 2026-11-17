"""A seat's launch changing during its thinking recovery: no open attempt, nothing typed, the next seat recovered.

Two synthetic OpenCode seats in one tick, each with a capped turn on the real reader, as the recovery tests build
them. The first seat's launch changes between the recovery's claim and its journal; the second recovers all the
same. No program runs.
"""
from datetime import timedelta
from types import SimpleNamespace

from quarterdeck import endpoint, pending, post, record_owner, recovery
from quarterdeck.seat import Launched, Seat
from quarterdeck.ship import Pool, SeatSpec, Session, Ship
from tests.unit.test_opencode_record import AT, database
from tests.unit.test_thinking_generation import capped
from tests.unit.test_thinking_recovery import Native


def two_seats(tmp_path, monkeypatch):
    monkeypatch.setattr(endpoint, 'models', lambda *args, **kw: [endpoint.Served('qwen3.8-27b', 98304)])
    monkeypatch.setattr(post, 'now', lambda: AT + timedelta(seconds=1))
    pool = Pool('synthetic', 'local', endpoint='http://example.invalid/v1')
    settings = {'provider': {'synthetic': {'models': {'qwen3.8-27b': {
        'options': {'chat_template_kwargs': {'enable_thinking': True, 'preserve_thinking': True}}}}}}}
    specs = {name: SeatSpec(name, 'crew', 'worker', 'opencode', ('synthetic',), model='qwen3.8-27b', settings=settings)
             for name in ('first', 'second')}
    ship = Ship(tmp_path, 'synthetic', 'operator', tmp_path, {'synthetic': pool}, {'crew': Session('crew', 'Crew')}, specs)
    seats, natives = [], {}
    for number, name in enumerate(specs, 1):
        seat = Seat(ship, specs[name], None)
        seat.harness.build_home(seat.home, seat.spec, pool, None)
        launch = seat.harness.launch(seat.spec, pool, seat.home, tmp_path / 'brief', None, None)
        Launched(f'%{number}', 12340 + number, AT.isoformat(), pool.name, None, list(launch.argv), '1.19.5', 'opencode',
                 recovery=launch.recovery).write(seat.trusted / 'launch.json')
        record = seat.home / 'data-001/opencode/opencode.db'
        con = database(record)
        capped(con)
        con.close()
        (seat.trusted / 'record').write_text(str(record))
        record_owner.register(seat.trusted, record, 'opencode')
        native = Native(seat, record)
        native.live = SimpleNamespace(id=f'%{number}', pid=12340 + number, width=120, dead=False, in_mode=False)
        seat.tmux = native
        seats.append(seat)
        natives[name] = native
    events = []
    service = SimpleNamespace(store=post.Store(ship.runtime), attempts=post.Attempts(ship.runtime), ship=ship,
                              resolved_seats=lambda: list(seats), log=lambda event, **fields: events.append((event, fields)))
    return service, seats, natives, events


def test_a_launch_changing_between_the_claim_and_the_journal_leaves_no_open_attempt_and_stops_no_other_seat(
        tmp_path, monkeypatch):
    service, (first, second), natives, events = two_seats(tmp_path, monkeypatch)
    opened = service.attempts.open

    def open_then_relaunch(message, seat, *args):
        attempt = opened(message, seat, *args)
        if seat == 'first':  # the seat's launch changes right after the claim, before its journal
            launched = first.launched()
            Launched('%9', 99999, (AT + timedelta(seconds=30)).isoformat(), launched.pool, None, launched.argv,
                     launched.version, launched.harness, recovery=launched.recovery).write(first.trusted / 'launch.json')
        return attempt
    service.attempts.open = open_then_relaunch
    recovery.run(service)
    attempts = service.attempts.all()
    assert not [a for a in attempts if a.result is None], 'no attempt is left open'
    [withheld] = [a for a in attempts if a.seat == 'first']
    assert withheld.result == 'withheld' and 'no input typed' in withheld.detail
    assert pending.journals(first) == ([], []), 'no journal for the first seat'
    assert natives['first'].calls == [] and natives['first'].inputs == [], 'nothing typed into the first seat'
    [proof] = pending.journals(second)[0]
    assert proof.data['phase'] == 'delivered' and proof.data['recovery']['mode'] == 'done'
    assert len(natives['second'].inputs) == 1, 'the second seat recovered in the same tick'
    logged = [fields for event, fields in events if event == 'thinking-recovery' and fields.get('result') == 'withheld']
    assert logged == [dict(seat='first', result='withheld', detail=logged[0]['detail'])]
    assert 'no delivery journal opened' in logged[0]['detail'], 'the reader of the launch named it (NoCurrentLaunch)'

    service.attempts.open = opened
    recovery.run(service)  # the next tick: the first seat's launch is still unproved for its recovery
    assert [a for a in service.attempts.all() if a.seat == 'first'] == [withheld], 'no new attempt is opened'
    assert len([e for e in events if e[0] == 'thinking-recovery' and e[1].get('result') == 'withheld']) == 1, \
        'logged once while it stands'
    assert natives['first'].calls == []
