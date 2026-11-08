"""Actual preceding readers/writers can operate after new writes, with no live runtime."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace

import pytest

from quarterdeck import endpoint
from quarterdeck.harness.types import Tokens, Turn
from quarterdeck.ledger import Ledger
from quarterdeck.post import Store
from quarterdeck.pricing import Price
from quarterdeck.seat import Launched, Seat
from quarterdeck.ship import Pool, SeatSpec, Session, Ship
from quarterdeck.tmux import Pane

BASE = '0f6b915fc78827f3b53c79b7be89b6eddd62e9a4'
AT = datetime(2032, 1, 1, tzinfo=timezone.utc)
RATES = dict(currency='USD', unit='million_tokens', input=2, output=4, cache_read=1, cache_write=3)


@pytest.fixture
def old(tmp_path, request):
    """Extract only committed Python sources in a disposable tree, never an evidence bundle."""
    with tempfile.TemporaryDirectory(dir=tmp_path) as directory:
        root = Path(directory)
        source = subprocess.check_output(['git', 'archive', getattr(request, 'param', BASE), 'src/quarterdeck'], cwd=Path(__file__).parents[2])
        with tarfile.open(fileobj=io.BytesIO(source)) as archive:
            archive.extractall(root, filter='data')
        env = {k: v for k, v in os.environ.items() if k not in ('QD_SHIP', 'QD_SEAT')}
        env['PYTHONPATH'] = str(root / 'src')

        def run(code, *args):
            prefix = 'import quarterdeck; from pathlib import Path; assert Path(quarterdeck.__file__).is_relative_to(Path.cwd())\n'
            result = subprocess.run([sys.executable, '-c', prefix + code, *map(str, args)],
                                    cwd=root, env=env, capture_output=True, text=True, timeout=30)
            assert result.returncode == 0, result.stderr + result.stdout
            return result.stdout

        yield run


OLD_WRITE = '''
import sys
from datetime import datetime,timedelta,timezone
from quarterdeck.ledger import Ledger
from quarterdeck.harness.types import Turn,Tokens
from quarterdeck.post import Store
p=Path(sys.argv[1]); at=datetime(2032,1,1,tzinfo=timezone.utc)
with Ledger(p/'ledger.sqlite') as b:
    b.provenance.remember(p/'record','worker','a',at)
    b.provenance.remember(p/'record','worker','b',at+timedelta(hours=1))
    indices=(0,) if len(sys.argv)>2 and sys.argv[2]=='seed' else (0,1)
    turns=[Turn(i,at+timedelta(minutes=i+1),at+timedelta(minutes=i+1,seconds=1),'in','out','m',Tokens(read_uncached=i+1),cost=0) for i in indices]
    b.ingest('worker','a',p/'record',turns,Store(p))
    assert b.ingested(p/'record') == len(indices)
    assert len(b.rollup('pool')) == 1
print('old writes and reads passed')
'''


@pytest.mark.parametrize('existing', [False, True])
def test_old_writer_round_trip_invalidates_stale_cost_evidence_without_losing_raw(tmp_path, old, existing):
    if existing:
        old(OLD_WRITE, tmp_path, 'seed')
    record = tmp_path / 'record'
    turn = Turn(0, AT+timedelta(minutes=1), AT+timedelta(minutes=1,seconds=1), 'in', 'out', 'm',
                Tokens(read_uncached=1), cost=2, recorded_cost=0, cost_basis='stated launch rates')
    price = Price.parse(RATES).snapshot('m')
    with Ledger(tmp_path / 'ledger.sqlite') as book:
        book.provenance.remember(record, 'worker', 'a', AT, price)
        book.ingest('worker', 'a', record, [turn], Store(tmp_path))
        assert len(book.db.execute('PRAGMA table_info(turns)').fetchall()) == 22
        assert len(book.db.execute('PRAGMA table_info(record_launches)').fetchall()) == 4
    assert old(OLD_WRITE, tmp_path).strip() == 'old writes and reads passed'
    with Ledger.reading(tmp_path / 'ledger.sqlite') as book:
        assert book.rollup('pool')[0].cost is None and book.rollup('pool')[0].unpriced == 2
    with Ledger(tmp_path / 'ledger.sqlite') as book:
        assert book.costs.get(record, 0) == (0, 'stated launch rates', 0)
        assert book.provenance.price(record, turn.started, turn.ended) == price
        assert book.provenance.price(record, AT+timedelta(hours=2), AT+timedelta(hours=3)) is None
        # A new source read without sufficient model/time evidence cannot promote the old default zero.
        book.ingest('worker', 'other', record, [replace(turn, cost=None, cost_basis=None)], Store(tmp_path))
        assert book.rollup('pool')[0].cost is None
        assert book.costs.get(record, 0) == (0, None, 1)
        # An actual qualified source refresh can restore the cost, once, without changing identity.
        assert book.ingest('worker', 'other', record, [turn], Store(tmp_path)) == 1
        assert book.ingest('worker', 'other', record, [turn], Store(tmp_path)) == 0
        assert book.ingested(record) == 2 and book.rollup('pool')[0].cost == 2
        assert book.db.execute('SELECT pool FROM turns ORDER BY turn').fetchall() == [('a',), ('a',)]


@pytest.mark.parametrize('reader,priced', [('pi', False), ('pi', True), ('opencode', False), ('opencode', True), ('fake', False)])
def test_real_launch_writer_keeps_old_launch_and_model_readers_usable(tmp_path, monkeypatch, old, reader, priced):
    pool = Pool('local', 'local', endpoint='http://127.0.0.1:1/v1', price=RATES if priced else None) if reader != 'fake' else Pool('local', 'fake')
    spec = SeatSpec('worker', 'crew', 'worker', reader, ('local',), cwd=tmp_path, sandbox=False)
    ship = Ship(tmp_path, 'synthetic', 'admiral', tmp_path, {'local': pool},
                {'crew': Session('crew', 'crew')}, {'worker': spec})
    declaration = '[ship]\nname="synthetic"\n[pools.local]\nprovider="fake"\n' if reader == 'fake' else (
        '[ship]\nname="synthetic"\n[pools.local]\nprovider="local"\nendpoint="http://127.0.0.1:1/v1"\n')
    if priced:
        declaration += '[pools.local.price]\ncurrency="USD"\nunit="million_tokens"\ninput=2\noutput=4\ncache_read=1\ncache_write=3\n'
    (tmp_path / 'ship.toml').write_text(declaration)
    seat = Seat(ship, spec, SimpleNamespace(pane=lambda ident: None))
    monkeypatch.setattr(endpoint, 'choose', lambda pool, model: endpoint.Choice('local', 'm', 4096, 1024))
    monkeypatch.setattr(seat.harness, 'version', lambda: 'synthetic')
    monkeypatch.setattr(seat.harness, 'program', sys.executable)
    monkeypatch.setattr(seat, '_open_window', lambda *args: Pane('%1', 'crew', 'worker', 2, 80, 24, False, False, None))
    monkeypatch.setattr(seat, '_survives', lambda *args: None)
    seat.up(resume=False)  # real persistence path, no actual program, pane or provider
    launched = seat.launched()
    expected = Price.parse(RATES).snapshot('m') if priced else None
    assert launched.price == expected
    assert 'price' not in json.loads((seat.folder / 'launch.json').read_text())
    output = old('''
import sys,json
from dataclasses import asdict,replace
from types import SimpleNamespace
from quarterdeck.seat import Seat
from quarterdeck import endpoint,harness
from quarterdeck.ship import Pool,SeatSpec,load
p=Path(sys.argv[1]); name=sys.argv[2]
launch=Seat.launched(SimpleNamespace(folder=p))
assert launch.pool=='local'
if name!='fake':
    assert load(p.parents[2]).pools['local'].endpoint=='http://127.0.0.1:1/v1'
    choice=endpoint.Choice.read(p/'home'); assert choice.model=='m'
    endpoint.choose=lambda pool,model: endpoint.Choice('local','m',4096,1024)
    pool=Pool('local','local',endpoint='http://127.0.0.1:1/v1')
    harness.get(name).build_home(p/'home',SeatSpec('worker','crew','worker',name,('local',)),pool,None)
path=p/'launch.json'
path.write_text(json.dumps(asdict(replace(launch,pid=3))))
print('old launch/model read and rewrite passed')
''', seat.folder, reader)
    assert output.strip() == 'old launch/model read and rewrite passed'
    assert seat.launched().price is None
    if reader != 'fake':
        assert endpoint.Choice.read(seat.home).price is None


def test_old_updates_invalidate_even_a_zero_cost_and_new_refresh_restores_it(tmp_path, old):
    turn = Turn(0, AT, AT, 'in', 'out', 'm', Tokens(read_uncached=1), cost=0, recorded_cost=0)
    record = tmp_path / 'record'
    with Ledger(tmp_path / 'ledger.sqlite') as book:
        book.ingest('worker', 'a', record, [turn], Store(tmp_path))
        assert book.rollup('pool')[0].cost == 0
    old('''
import sys
from quarterdeck.ledger import Ledger
with Ledger(Path(sys.argv[1])/'ledger.sqlite') as b:
    b.db.execute('UPDATE turns SET read_uncached=2')
    b.db.commit()
''', tmp_path)
    with Ledger(tmp_path / 'ledger.sqlite') as book:
        assert book.costs.get(record, 0) == (0, 'record', 0)
        assert book.rollup('pool')[0].cost is None
        book.ingest('worker', 'a', record, [replace(turn, tokens=Tokens(read_uncached=2))], Store(tmp_path))
        assert book.costs.get(record, 0) == (0, 'record', 1)
        assert book.rollup('pool')[0].cost == 0 and book.rollup('pool')[0].unpriced == 0
