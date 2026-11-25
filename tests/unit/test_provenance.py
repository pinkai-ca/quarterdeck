"""Pool provenance on actual same-record restart paths, with synthetic records and no launches."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

from quarterdeck import provenance, service
from quarterdeck.harness.base import State, Tokens, ToolCall, Turn, Usage, Window
from quarterdeck.harness.synthetic_record import CodexShape
from quarterdeck.ledger import Ledger, Machine, table
from quarterdeck.post import Store
from quarterdeck.seat import Launched, Seat
from quarterdeck.service import Service
from quarterdeck.ship import Pool, SeatSpec, Session, Ship
from quarterdeck.tmux import Pane


def test_account_same_record_failover_restart_and_delayed_metadata(tmp_path, monkeypatch):
    pools = {name: Pool(name, 'fake') for name in ('pool-a', 'pool-b')}
    spec = SeatSpec('worker', 'crew', 'worker', 'fake', tuple(pools), cwd=tmp_path)
    ship = Ship(tmp_path, 'example', 'operator', tmp_path, pools,
                {'crew': Session('crew', 'crew')}, {'worker': spec})
    active = {'pane': Pane('%1', 'crew', 'worker', 1001, 80, 24, False, False, None)}
    tmux = SimpleNamespace(pane=lambda ident: active['pane'], kill_window=lambda ident, *, expected_pid=None: active.update(pane=None))
    seat = Seat(ship, spec, tmux)
    seat.home.mkdir(parents=True)
    seat.trusted.mkdir(parents=True, exist_ok=True)
    shape = CodexShape(seat.home, tmp_path, 'synthetic-provenance', 'synthetic-model')
    shape.begin('old input', 'synthetic-model', 'high')
    shape.end('old reply')
    record = shape.path
    (seat.trusted / 'record').write_text(str(record))
    origin = datetime.now(timezone.utc) - timedelta(minutes=10)
    initial_launch = Launched('%1', 1001, origin.isoformat(), 'pool-a', None, ['fake'], 'synthetic', start='1',
                              provider_route=provenance.provider_route(seat.pool))
    initial_launch.write(seat.trusted / 'launch.json')
    old = Turn(0, origin + timedelta(seconds=1), origin + timedelta(seconds=2),
               'old input', 'old reply', 'synthetic-model', Tokens(1, 10, 0, 2),
               (ToolCall('run_command', None, False),))
    data = {'turns': [old], 'state': seat.harness.state(record)}

    def read(path):
        if not path.exists():
            raise OSError('synthetic record missing')
        return data['state'], data['turns']

    monkeypatch.setattr(service, 'seats', lambda *args: [seat])
    monkeypatch.setattr(seat.harness, 'records', lambda home: [record])
    monkeypatch.setattr(seat.harness, 'read', read)
    monkeypatch.setattr(seat.harness, 'state', lambda path: data['state'])
    monkeypatch.setattr(seat.harness, 'session_id', lambda path: 'synthetic-resume')
    monkeypatch.setattr(seat, 'is_working', lambda: False)
    monkeypatch.setattr(seat, '_survives', lambda *args: None)
    launches = []

    def open_window(argv, env, path):
        launches.append(argv)
        active['pane'] = Pane('%2', 'crew', 'worker', 1002, 80, 24, False, False, None)
        return active['pane']

    monkeypatch.setattr(seat, '_open_window', open_window)
    from quarterdeck import seat as seatmod
    monkeypatch.setattr(seatmod, 'process_start', lambda pid: str(pid))
    monkeypatch.setattr(seatmod, 'process_alive', lambda pid, start: False)
    monkeypatch.setattr(service.machine, 'processes', lambda: {})
    svc = Service(ship, tmux)
    svc.meters['worker'] = SimpleNamespace(root=1001, sample=lambda table: None,
                                           result=lambda: Machine(3.5, 4096, 2, 800))
    svc.account()
    svc.ledger.record_usage('pool-a', Usage((Window('synthetic', 1.0, datetime.now(timezone.utc) + timedelta(hours=1)),), datetime.now(timezone.utc)))
    svc.failover()  # real Service.failover -> move_to -> Seat.restart/down/up, with only the program open mocked
    assert seat.pool.name == 'pool-b' and seat.record() == record
    assert '--resume' in launches[0] and 'synthetic-resume' in launches[0]
    resumed = seat.launched()
    journal = [json.loads(line) for line in (seat.trusted / 'record-launches.jsonl').read_text().splitlines()]
    assert [(event['pool'], event['at']) for event in journal] == [('pool-a', initial_launch.at), ('pool-b', resumed.at)]
    assert journal[0]['provider_route'] == initial_launch.provider_route
    assert journal[1]['provider_route'] == resumed.provider_route == {'provider': 'synthetic', 'address': 'default'}
    assert list(provenance.retained_routes(seat.trusted, record)) == [
        (initial_launch.provider_route, 'pool-a'), (resumed.provider_route, 'pool-b')]

    # No new turn is required for unreadability to affect both launch pools.
    record.unlink()
    svc.account()
    assert {r[2] for r in svc.ledger.incomplete_records()} == {'pool-a', 'pool-b'}
    assert all(r.unmeasured for r in svc.ledger.rollup('pool')), 'a record that would not read marks its pools, and hides nothing'
    new = replace(old, index=4, started=resumed.when + timedelta(seconds=1),
                  ended=resumed.when + timedelta(seconds=2), tokens=Tokens(2, 20, 0, 3), tools=())
    data.update(turns=[old, new], state=State(turns=2, turn_open=False))
    record.write_text('revision 2')
    svc.account()
    assert svc.ledger.incomplete_records() == []
    assert svc.ledger.db.execute('SELECT turn,pool FROM turns ORDER BY turn').fetchall() == [(0,'pool-a'),(4,'pool-b')]

    svc.ledger.close()
    svc = Service(ship, tmux)  # a service restart must retain both launch boundaries
    refreshed = replace(old, tools=(ToolCall('shell', 1.5, False),), tokens=Tokens(1, 11, 0, 2))
    delayed = replace(old, index=2, started=origin + timedelta(seconds=3), ended=origin + timedelta(seconds=4), tools=())
    data.update(turns=[refreshed, delayed, new], state=State(turns=3, turn_open=False))
    record.write_text('revision 3')
    svc.account()
    expected = [(0,'pool-a',11),(2,'pool-a',10),(4,'pool-b',20)]
    assert svc.ledger.db.execute('SELECT turn,pool,read_uncached FROM turns ORDER BY turn').fetchall() == expected
    assert svc.ledger.tools() == [('shell',1,1.5,0,0,0)]
    assert svc.ledger.db.execute('SELECT cpu_seconds,peak_rss,max_processes,write_bytes FROM turns WHERE turn=0').fetchone() == (3.5,4096,2,800)
    before = svc.ledger.db.total_changes
    svc.account()
    assert svc.ledger.db.total_changes == before, 'unchanged records and launch history are idempotent'
    svc.ledger.close()

    # Stopping removes launch.json, but a new accounting database can still import the durable history.
    seat.down()
    assert not (seat.trusted / 'launch.json').exists()
    svc = Service(ship, tmux)
    svc.ledger.close()
    svc.ledger = Ledger(ship.runtime / 'rebuilt-ledger.sqlite')
    svc.account()
    assert svc.ledger.db.execute('SELECT turn,pool,read_uncached FROM turns ORDER BY turn').fetchall() == expected
    svc.ledger.close()


def test_missing_or_ambiguous_launch_history_is_explicit_and_can_improve(tmp_path):
    book, store = Ledger(tmp_path / 'ledger.sqlite'), Store(tmp_path)
    record = tmp_path / 'synthetic-record'
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    turn = Turn(0, stamp, stamp + timedelta(seconds=1), 'input', 'reply', 'model', Tokens(1,2,3,4))
    book.ingest('worker', '-', record, [turn], store)
    assert book.rollup('pool')[0].key == '-'
    assert '1 turns with unknown pool attribution' in '\n'.join(table(book, 'seat'))
    # An unrelated current launch cannot prove which pool paid for older history.
    book.provenance.remember(record, 'worker', 'pool-b', stamp + timedelta(minutes=1))
    book.ingest('worker', 'pool-b', record, [turn], store)
    assert book.rollup('pool')[0].key == '-'
    book.provenance.remember(record, 'worker', 'pool-a', stamp - timedelta(seconds=1))
    book.ingest('worker', '-', record, [turn], store)
    assert book.rollup('pool')[0].key == 'pool-a'
    book.ingest('other-seat', 'pool-b', record, [turn], store)
    assert book.db.execute('SELECT seat,pool FROM turns WHERE turn=0').fetchone() == ('worker','pool-a')
    missing_time = replace(turn, index=4, started=None)
    book.ingest('worker', 'pool-b', record, [turn, missing_time], store)
    assert book.db.execute('SELECT pool FROM turns WHERE turn=4').fetchone() == ('-',)
    improved = replace(missing_time, started=stamp + timedelta(minutes=2), ended=stamp + timedelta(minutes=3))
    book.ingest('worker', '-', record, [turn, improved], store)
    assert book.db.execute('SELECT pool FROM turns WHERE turn=4').fetchone() == ('pool-b',)
    spanning = replace(turn, index=6, ended=stamp + timedelta(minutes=2))
    missing_end = replace(turn, index=7, ended=None)
    book.ingest('worker', '-', record, [spanning, missing_end], store)
    assert book.db.execute('SELECT pool FROM turns WHERE turn IN (6,7)').fetchall() == [('-',), ('-',)]
    # Conflicting launches at the same instant do not establish an owner for a new turn.
    book.provenance.remember(record, 'worker', 'pool-c', stamp + timedelta(minutes=1))
    book.ingest('worker', '-', record, [replace(improved,index=8)], store)
    assert book.db.execute('SELECT pool FROM turns WHERE turn=8').fetchone() == ('-',)
    book.mark_record(record, 'worker', '-', 'incomplete history')
    assert {r[2] for r in book.incomplete_records()} == {'-', 'pool-a', 'pool-b', 'pool-c'}
    assert all(r.unmeasured for r in book.rollup('pool'))
    book.close()
