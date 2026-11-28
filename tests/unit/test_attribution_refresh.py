"""Turn writes must reconcile full cached source evidence, even without file/post changes."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from quarterdeck import post, record_owner, service
from quarterdeck.harness.fake import FakeHarness
from quarterdeck.harness.types import Input
from quarterdeck.ledger import Machine
from quarterdeck.service import Service
from quarterdeck.ship import Ship
from tests.measure_status_service import identity as file_identity
from .test_order_attribution import AT, message, measurement_snapshot, turn


@pytest.mark.parametrize('kind', ['consumed', 'watch'])
@pytest.mark.parametrize('change', ['measurement-refresh', 'new-turn'])
def test_turn_write_waits_for_full_cached_evidence_and_service_replay(tmp_path, monkeypatch, kind, change):
    ship=Ship(tmp_path,'synthetic','operator',tmp_path,{}, {}, {})
    monkeypatch.setattr(service.machine,'processes',lambda:{})
    svc=Service(ship,SimpleNamespace())
    order=message(svc.store,'ord-work')
    message(svc.store,'rep-close','report','worker','issuer',1,order.id)
    record=ship.runtime/'record';record.write_text('unchanged synthetic source')
    record_owner.register(ship.runtime/'trusted/worker',record,'fake')
    calls=[]
    def inputs(self,path):
        calls.append(path)
        return [Input(post.typed([order]), AT+timedelta(seconds=2 if kind=='consumed' else second), kind,
                      AT+timedelta(seconds=second) if kind=='consumed' else None) for second in (11,31)]
    monkeypatch.setattr(FakeHarness,'attribution_inputs',inputs)
    initial=turn()
    assert svc.ledger.ingest('worker','pool',record,[initial],svc.store,{0:Machine(1.25,8192,3,512)})==1
    svc.account()
    assert svc.ledger.attributions.details()[0]['orders']==[order.id]
    source_stamp = file_identity(record.stat())
    posts=svc.store.all()
    if change=='measurement-refresh':
        updated=replace(initial,cost=3)
        expected_turns,expected_cost,expected_tokens=1,3,10
    else:
        updated=turn(1,30,40)
        expected_turns,expected_cost,expected_tokens=2,4,20
    assert svc.ledger.ingest('worker','pool',record,[updated],svc.store)==1
    assert svc.ledger.ingest('worker','pool',record,[updated],svc.store)==0
    assert svc.ledger.attributions.details()[updated.index]['status']=='pending'
    assert any(row.key.startswith('pending:') for row in svc.ledger.rollup('order'))
    measured=measurement_snapshot(svc.ledger)
    svc.account()
    rows=svc.ledger.attributions.details()
    assert all(row['status']=='attributed' and row['orders']==[order.id] for row in rows)
    assert all(row['evidence'][0]['route']==('watch' if kind=='watch' else 'input') for row in rows)
    assert measurement_snapshot(svc.ledger)==measured
    total=svc.ledger.rollup('order')[0]
    assert total.key==order.id and total.turns==expected_turns and total.cost==expected_cost
    assert total.read_cached+total.read_uncached+total.cache_written+total.written==expected_tokens
    assert total.cpu_seconds==1.25 and total.write_bytes==512
    assert calls==[record], 'reconciliation must use the unchanged cached full source, not only turn.input'
    assert file_identity(record.stat()) == source_stamp and svc.store.all() == posts
    assert svc.ledger.attributions.replay(svc.store,ship.runtime)==0
    svc.account()
    assert measurement_snapshot(svc.ledger)==measured and calls==[record]
    svc.ledger.close()
