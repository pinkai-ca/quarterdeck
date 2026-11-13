"""The actual base can read/write the unchanged book after reverting new declarations."""

import pytest

from quarterdeck.ledger import Ledger
from .test_cross_program_failover import running, CHOICES
from .test_price_rollback import old

BASE = '24dab4b52daa20f4c7f7be769132c3d859674629'


@pytest.mark.parametrize('old', [BASE], indirect=True)
def test_base_reads_and_writes_after_cross_program_move_with_declaration_reverted(running, old):
    r = running
    r.svc.account()
    r.svc.failover()
    r.svc.account()
    original = r.seat.ship.file.read_text()
    try:
        text = old('''
import sys
from quarterdeck.ship import load, ShipError
try:
    load(Path(sys.argv[1]))
except (ShipError, TypeError):
    print('new inline declaration requires reversion before rollback')
else:
    raise AssertionError('base unexpectedly accepted inline declarations')
''', r.seat.ship.folder)
        assert 'requires reversion' in text
        r.seat.ship.file.write_text(original.replace('pool='+CHOICES, 'pool="first"').replace('provider="openai"', 'provider="codex"').replace('provider="anthropic"', 'provider="claude"'))
        text = old('''
import os, sys
from datetime import datetime, timedelta, timezone
from quarterdeck import cli
from quarterdeck.harness.types import Tokens, Turn
from quarterdeck.ledger import Ledger
from quarterdeck.post import Store
root=Path(sys.argv[1]);runtime=root/'runtime'
os.environ.pop('QD_TMUX_SOCKET',None);os.environ.pop('TMUX',None)
for args in (['status'],['status','--line'],['ledger'],['--json','ledger'],['--json','status']):
    assert cli.main(['--ship',str(root),*args])==0
with Ledger(runtime/'ledger.sqlite') as book:
    assert len(book.db.execute('PRAGMA table_info(turns)').fetchall())==22
    assert len(book.db.execute('SELECT * FROM turns').fetchall())==2
    at=datetime(2032,1,1,tzinfo=timezone.utc)
    record=runtime/'synthetic-old-writer.jsonl'
    record.write_text('')
    assert book.ingest('worker','first',record,[Turn(0,at,at+timedelta(seconds=1),'input','reply','synthetic',Tokens(1),cost=2)],Store(runtime))==1
    assert sum(row.turns for row in book.rollup('seat'))==3
print('base readers and writer passed after declaration reversion')
''', r.seat.ship.folder)
        assert text.endswith('base readers and writer passed after declaration reversion\n')
        with Ledger.reading(r.seat.ship.runtime/'ledger.sqlite') as book:
            assert sum(row.turns for row in book.rollup('seat')) == 3
            assert set(row.key for row in book.rollup('pool')) == {'first', 'second'}
    finally:
        r.seat.ship.file.write_text(original)
