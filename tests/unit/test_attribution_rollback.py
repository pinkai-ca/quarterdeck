"""The actual fdd98ec reader/writer ignores side verdicts and invalidates them safely."""

from datetime import timedelta
import json

import pytest

from quarterdeck import cli, ledger_reading
from quarterdeck.harness.fake import FakeHarness
from quarterdeck.ledger import Ledger
from quarterdeck.post import Store
from .test_order_attribution import AT, message, turn, measurement_snapshot
from .test_price_rollback import old

BASE='fdd98eca6766e8e9662f21da34b7cb347d4c728f'


@pytest.mark.parametrize('old',[BASE],indirect=True)
def test_preceding_cli_reads_then_refreshes_existing_and_inserts_new_turn_then_service_replays(tmp_path,old,monkeypatch,capsys):
    (tmp_path/'ship.toml').write_text('[ship]\nname="synthetic"\n')
    runtime=tmp_path/'runtime';store=Store(runtime)
    root=message(store,'ord-root',to='captain')
    implementation=message(store,'ord-child',sender='captain',to='worker',second=1,re=root.id)
    message(store,'rep-child','report','worker','captain',15,implementation.id)
    message(store,'ans-followup','answer','worker','captain',35,root.id)
    record=runtime/'record';record.write_text('synthetic source')
    monkeypatch.setattr(FakeHarness,'attribution_inputs',lambda self,path:[])
    with Ledger(runtime/'ledger.sqlite') as book:
        book.ingest('worker','pool',record,[turn()],store)
        book.attributions.replay(store,runtime,{str(record):{'fake'}})
        assert book.rollup('order')[0].key==root.id
        assert len(book.db.execute('PRAGMA table_info(turns)').fetchall())==22
        assert len(book.db.execute('PRAGMA table_info(tool_calls)').fetchall())==5
    text=old('''
import os,sys
from datetime import datetime,timedelta,timezone
from quarterdeck import cli
from quarterdeck.ledger import Ledger
from quarterdeck.harness.types import Turn,Tokens
from quarterdeck.post import Store
p=Path(sys.argv[1]);r=p/'runtime';at=datetime(2032,1,1,tzinfo=timezone.utc)
os.environ['QD_TMUX_SOCKET']='qd-synthetic-attribution-no-server'
for flags in (['status'],['status','--line'],['ledger'],['ledger','--by','order'],['watch']):
    for json_flag in ([],['--json']):
        assert cli.main(['--ship',str(p),*json_flag,*flags])==0
with Ledger(r/'ledger.sqlite') as book:
    before=book.db.execute('SELECT read_cached,read_uncached,cache_written,written,cost FROM turns').fetchall()
    refreshed=Turn(0,at+timedelta(seconds=10),at+timedelta(seconds=20),'input','reply','refreshed-model',Tokens(1,2,3,4),cost=2)
    inserted=Turn(1,at+timedelta(seconds=30),at+timedelta(seconds=40),'input','reply','model',Tokens(1,2,3,4),cost=2)
    assert book.ingest('worker','pool',r/'record',[refreshed,inserted],Store(r))==2
    assert book.db.execute('SELECT read_cached,read_uncached,cache_written,written,cost FROM turns ORDER BY turn').fetchall()==before+before
    assert book.db.execute('SELECT valid FROM turn_attribution WHERE turn=0').fetchone()==(0,)
    assert sum(row.cost for row in book.rollup('seat'))==4
print('preceding read, existing replacement and new insert passed')
''',tmp_path)
    assert text.endswith('preceding read, existing replacement and new insert passed\n')
    with Ledger(runtime/'ledger.sqlite') as book:
        before=measurement_snapshot(book)
        assert [r['status'] for r in book.attributions.details()]==['pending','pending']
        assert all(row.key.startswith('pending:') for row in book.rollup('order'))
        assert book.attributions.replay(store,runtime,{str(record):{'fake'}})==2
        assert measurement_snapshot(book)==before
        rows=book.rollup('order')
        assert len(rows)==1 and rows[0].key==root.id and rows[0].turns==2 and rows[0].cost==4
        assert book.attributions.replay(store,runtime,{str(record):{'fake'}})==0
    monkeypatch.delenv('QD_SHIP',raising=False);monkeypatch.delenv('QD_SEAT',raising=False)
    monkeypatch.setenv('QD_TMUX_SOCKET','qd-synthetic-attribution-no-server')
    before=(runtime/'ledger.sqlite').read_bytes()
    for flags in (['status'],['status','--line'],['ledger'],['ledger','--by','order'],['watch']):
        for json_flag in ([],['--json']):
            assert cli.main(['--ship',str(tmp_path),*json_flag,*flags])==0
            capsys.readouterr()
    assert (runtime/'ledger.sqlite').read_bytes()==before
