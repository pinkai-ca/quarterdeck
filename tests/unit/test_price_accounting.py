"""Real accounting uses saved endpoint launches, not the seat's latest pool settings."""

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

from quarterdeck import service
from quarterdeck.pricing import Price
from quarterdeck.provenance import remember_launch
from quarterdeck.seat import Launched, Seat
from quarterdeck.service import Service
from quarterdeck.ship import Pool, SeatSpec, Session, Ship

AT = datetime(2032, 1, 1, tzinfo=timezone.utc)
RATES = dict(currency='USD', unit='million_tokens', input=2, output=4, cache_read=1, cache_write=3)


def append_turn(path, minutes):
    rows = [{'type': 'message', 'timestamp': (AT + timedelta(minutes=minutes)).isoformat(),
             'message': {'role': 'user', 'content': 'synthetic input'}},
            {'type': 'message', 'timestamp': (AT + timedelta(minutes=minutes, seconds=1)).isoformat(),
             'message': {'role': 'assistant', 'model': 'm', 'content': [], 'stopReason': 'stop',
                         'usage': {'input': 1_000_000, 'cost': {'total': 0}}}}]
    with path.open('a') as stream:
        stream.write(''.join(json.dumps(row) + '\n' for row in rows))


def test_account_prices_resume_fresh_pool_edit_and_restart_from_launch_evidence(tmp_path, monkeypatch):
    pool = Pool('a', 'local', endpoint='http://127.0.0.1:1/v1', price=RATES)
    spec = SeatSpec('worker', 'crew', 'worker', 'pi', ('a', 'b'), cwd=tmp_path)
    ship = Ship(tmp_path, 'synthetic', 'admiral', tmp_path, {'a': pool, 'b': replace(pool, name='b')},
                {'crew': Session('crew', 'crew')}, {'worker': spec})
    seat = Seat(ship, spec, SimpleNamespace(pane=lambda ident: None))
    folder = seat.home / 'sessions'
    folder.mkdir(parents=True)
    record = folder / 'first.jsonl'
    record.touch()
    launched = Launched('%1', 2, AT.isoformat(), 'a', None, ['pi'], 'synthetic', 'pi')

    def pin(path, launch):
        (seat.folder / 'record').write_text(str(path))
        (seat.folder / 'launch.json').write_text(json.dumps(asdict(launch)))
        remember_launch(seat.folder, path, launch)

    pin(record, launched)  # old launch had no stated price
    append_turn(record, 1)
    monkeypatch.setattr(service, 'seats', lambda *args: [seat])
    monkeypatch.setattr(service.machine, 'processes', lambda: {})
    svc = Service(ship, seat.tmux)
    svc.account()
    assert svc.ledger.rollup('pool')[0].unpriced == 1
    # Editing a pool does not retroactively price this launch, even after restart.
    ship.pools['a'] = replace(pool, price={**RATES, 'input': 99})
    svc.ledger.close()
    svc = Service(ship, seat.tmux)
    svc.account()
    assert svc.ledger.db.execute('SELECT cost,recorded_cost FROM turns').fetchall() == [(None, 0)]
    resumed = replace(launched, at=(AT + timedelta(minutes=2)).isoformat(), resume='synthetic',
                      price=Price.parse(RATES).snapshot('m'))
    pin(record, resumed)
    append_turn(record, 3)
    svc.account()
    zero = replace(resumed, at=(AT + timedelta(minutes=4)).isoformat(), pool='b',
                   price=Price.parse({**RATES, 'input': 0}).snapshot('m'))
    pin(record, zero)
    append_turn(record, 5)
    svc.account()
    assert svc.ledger.db.execute('SELECT turn,pool,cost FROM turns ORDER BY turn').fetchall() == [(0,'a',None),(1,'a',2),(2,'b',0)]
    fresh = folder / 'fresh.jsonl'
    pin(fresh, replace(zero, at=(AT + timedelta(minutes=6)).isoformat(), resume=None, price=None))
    append_turn(fresh, 7)
    svc.account()
    svc.ledger.close()
    svc = Service(ship, seat.tmux)
    svc.account()
    assert svc.ledger.db.execute('SELECT pool,cost,recorded_cost FROM turns ORDER BY record,turn').fetchall() == [
        ('a',None,0),('a',2,0),('b',0,0),('b',None,0)]
    before = svc.ledger.db.total_changes
    svc.account()
    assert svc.ledger.db.total_changes == before
    svc.ledger.close()
