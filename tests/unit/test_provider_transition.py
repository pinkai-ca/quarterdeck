"""Actual preceding code writes/reads synthetic saved seats across spelling changes."""

from types import SimpleNamespace

import pytest

from quarterdeck import record_owner
from quarterdeck.harness.synthetic_record import ClaudeShape, CodexShape
from quarterdeck.ledger import Ledger
from quarterdeck.post import Store
from quarterdeck.seat import Seat
from quarterdeck.ship import load
from tests.antigravity_shapes import AntigravityShape
from .test_price_rollback import old  # noqa: F401
from .test_provider_vocabulary import PAIRS, declaration

BASE = "27129131c0347cdff18fed8f076de5fa5ad8d745"

SEED = '''
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from quarterdeck.ship import load
from quarterdeck.seat import Launched, Seat
from quarterdeck.ledger import Ledger
from quarterdeck.post import Store
p=Path(sys.argv[1]); record=Path(sys.argv[2]); program=sys.argv[3]
ship=load(p); seat=Seat(ship,ship.seat('worker'),SimpleNamespace())
Launched('%1',101,(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat(),
         'paid',None,[program],'synthetic',program).write(seat.folder/'launch.json')
seat.move_to('paid')
(seat.folder/'record').write_text(str(record)+'\\n')
assert seat.record()==record
with Ledger(ship.runtime/'ledger.sqlite') as book:
    state,turns=seat.harness.read(record)
    assert len(turns)==1
    book.ingest('worker','paid',record,turns,Store(ship.runtime))
    assert book.rollup('pool')[0].key=='paid'
print('old launch/selection/reader/accounting seeded')
'''

READ = '''
import sys
from types import SimpleNamespace
from quarterdeck.ship import load
from quarterdeck.seat import Seat
from quarterdeck.record_owner import records
from quarterdeck.ledger import Ledger
p=Path(sys.argv[1]); ship=load(p)
seat=Seat(ship,ship.seat('worker'),SimpleNamespace())
assert seat.pool.name=='paid' and seat.launched().pool=='paid'
assert seat.launched().harness==seat.spec.harness
assert len(records(seat))==1 and records(seat)[0].name==seat.spec.harness
assert len(seat.harness.turns(seat.record()))==1
with Ledger.reading(ship.runtime/'ledger.sqlite') as book:
    assert book.rollup('pool')[0].key=='paid' and book.rollup('pool')[0].turns==1
print('old saved seat and accounting readable')
'''


@pytest.mark.parametrize("old", [BASE], indirect=True)
@pytest.mark.parametrize("company,program", PAIRS)
def test_upgrade_edit_rollback_preserves_saved_launch_selection_records_and_ledger(tmp_path, old, company, program):
    declaration(tmp_path, program, program)
    original = (tmp_path / "ship.toml").read_text()
    home = tmp_path / "runtime/seats/worker/home"
    shape = {"codex": CodexShape, "antigravity": AntigravityShape}.get(program, ClaudeShape)
    source = shape(home, tmp_path, "synthetic-session", "synthetic-model")
    source.begin("synthetic input", "synthetic-model", "high")
    source.end("synthetic reply")
    assert old(SEED, tmp_path, source.path, program).strip().endswith("seeded")
    assert old(READ, tmp_path).strip().endswith("readable")
    source_bytes = source.path.read_bytes()
    folder = home.parent
    retained = {name: (folder / name).read_bytes() for name in
                ("launch.json", "pool", "record", "record-readers.jsonl", "record-launches.jsonl")}
    # The preceding code keeps a seat's saved files in its own folder; this code reads them from its trusted folder,
    # where the move takes them only on live evidence (seat_migration). This test is about the provider's spelling,
    # so the files stand where the move would put them, copied, and the old copies stay for the old code's reads.
    trusted = tmp_path / "runtime/trusted/worker"
    trusted.mkdir(parents=True)
    for name, data in retained.items():
        (trusted / name).write_bytes(data)
    (trusted / "launch-price.json").write_bytes((folder / "launch-price.json").read_bytes())
    for text in (original, original.replace(f'provider="{program}"', f'provider="{company}"'), original):
        (tmp_path / "ship.toml").write_text(text)
        ship = load(tmp_path)
        seat = Seat(ship, ship.seat("worker"), SimpleNamespace())
        assert seat.pool.name == "paid" and seat.pool.provider == company
        assert seat.record() == source.path
        owned = record_owner.records(seat)
        assert [(r.path, r.name) for r in owned] == [(source.path, program)]
        with Ledger(ship.runtime / "ledger.sqlite") as book:
            _, turns = owned[0].read()
            assert book.ingest("worker", "paid", source.path, turns, Store(ship.runtime)) == 0
            assert book.rollup("pool")[0].turns == 1
        assert source.path.read_bytes() == source_bytes
        assert all((folder / name).read_bytes() == data for name, data in retained.items())
        if f'provider="{company}"' in text:
            # The old parser must reject new declarations: restore aliases BEFORE downgrade.
            assert old('''
import sys
from quarterdeck.ship import load,ShipError
try: load(Path(sys.argv[1]))
except ShipError as e: assert 'unsupported subscription provider' in str(e)
else: raise AssertionError('old reader unexpectedly accepted company declaration')
print('company declaration requires new reader')
''', tmp_path).strip() == "company declaration requires new reader"
        else:
            assert old(READ, tmp_path).strip().endswith("readable")
