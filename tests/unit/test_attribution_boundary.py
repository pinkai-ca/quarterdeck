"""Default reader discovery cannot authorize attribution reads outside the runtime."""

from datetime import timedelta
from pathlib import Path

import pytest

from quarterdeck import attribution_cache, ledger_reading, post, record_owner
from quarterdeck.harness.fake import FakeHarness
from quarterdeck.harness.types import Input
from quarterdeck.ledger import Ledger
from .test_order_attribution import AT, message, measurement_snapshot, turn


@pytest.mark.parametrize('relative_escape', [False, True])
def test_default_replay_excludes_bound_external_source_before_any_access(tmp_path, monkeypatch, relative_escape):
    runtime=tmp_path/'runtime';runtime.mkdir()
    external=tmp_path/'legacy'/'record'
    bound=runtime/'..'/'legacy'/'record' if relative_escape else external
    store=post.Store(runtime)
    outside_order=message(store,'ord-outside')
    message(store,'ack-work','ack','worker','issuer',1,outside_order.id)
    message(store,'ans-work','answer','worker','issuer',15,outside_order.id)
    inside_order=message(store,'ord-inside',to='internal')
    message(store,'rep-closed','report','internal','issuer',1,inside_order.id)
    internal=runtime/'record';internal.write_text('synthetic internal source')
    record_owner.register(runtime/'seats/worker',bound,'fake')
    record_owner.register(runtime/'seats/internal',internal,'fake')
    fingerprints=[]
    fingerprint=attribution_cache.record_fingerprint
    def checked_fingerprint(path):
        assert path==internal, 'external source must be excluded before fingerprinting'
        fingerprints.append(path)
        return fingerprint(path)
    monkeypatch.setattr(attribution_cache,'record_fingerprint',checked_fingerprint)
    for name in ('stat','open'):
        original=getattr(Path,name)
        def checked(self,*args,_original=original,**kwargs):
            assert not self.is_relative_to(external.parent), 'external source must never be statted or opened'
            return _original(self,*args,**kwargs)
        monkeypatch.setattr(Path,name,checked)
    reads=[]
    def inputs(self,path):
        assert path==internal, 'external source must never reach an adapter'
        reads.append(path)
        return [Input(post.typed([inside_order]),AT+timedelta(seconds=51))]
    monkeypatch.setattr(FakeHarness,'attribution_inputs',inputs)
    with Ledger(runtime/'ledger.sqlite') as book:
        book.ingest('worker','pool',bound,[turn(),turn(1,30,40)],store)
        book.ingest('internal','pool',internal,[turn(0,50,60)],store)
        before=measurement_snapshot(book)
        assert book.attributions.replay(store,runtime)==3
        rows=book.attributions.details()
        outside=[r for r in rows if r['seat']=='worker']
        assert len(outside)==2
        assert all(r['status']=='unassociated' and r['orders']==[] and r['evidence']==[] for r in outside)
        assert all(r['reason']=='legacy binding outside the runtime, no saved fingerprint' for r in outside)
        inside=next(r for r in rows if r['seat']=='internal')
        assert inside['status']=='attributed' and inside['orders']==[inside_order.id]
        assert measurement_snapshot(book)==before
        totals=book.rollup('order')
        assert sum(r.turns for r in totals)==3 and sum(r.cost for r in totals)==6
        assert next(r for r in totals if r.key=='-').turns==2
        assert 'legacy binding outside the runtime, no saved fingerprint' in '\n'.join(
            ledger_reading.render_rollup(ledger_reading.collect(book,by='order')))
        assert book.attributions.replay(store,runtime)==0
        assert fingerprints==[internal,internal] and reads==[internal]
