"""One reading uses one consistent attempt inventory, and the next reading refreshes it."""

import json

import pytest

from types import SimpleNamespace

from quarterdeck import pending, post, ship as shipmod, watch
from quarterdeck.service import Service


pytestmark = pytest.mark.usefixtures('synthetic_disk_room')


def empty_ship(tmp_path):
    return shipmod.Ship(tmp_path,'synthetic','operator',tmp_path,{}, {}, {})


def test_facts_reads_one_attempt_inventory_and_observes_the_next_same_path_change(tmp_path,monkeypatch):
    ship=empty_ship(tmp_path);store=post.Store(ship.runtime);attempts=post.Attempts(ship.runtime)
    queued=store.new('order','issuer','worker','Synthetic queued work')
    blind=store.new('order','issuer','worker','Synthetic uncertain work')
    a=attempts.close(attempts.open(queued,'worker',None,None,None),'queued')
    attempts.close(attempts.open(blind,'worker',None,None,None),'blind')
    calls=[];all_attempts=post.Attempts.all
    def read(self):calls.append(True);return all_attempts(self)
    monkeypatch.setattr(post.Attempts,'all',read)
    facts=watch.facts(ship,SimpleNamespace())
    assert facts.queued==1 and facts.blind==1
    assert len(calls)==1, 'a single facts reading must not rescan mutable attempts for every derived count'
    attempts.close(a,'delivered')
    facts=watch.facts(ship,SimpleNamespace())
    assert facts.queued==0 and facts.blind==1 and len(calls)==2


@pytest.mark.parametrize('change', [None, 'receipt', 'in-place'])
def test_reconcile_reads_one_inventory_per_journal_and_attempt_phase(tmp_path,monkeypatch,change):
    ship=empty_ship(tmp_path)
    ship.pools['fake']=shipmod.Pool('fake','fake')
    ship.sessions['synthetic']=shipmod.Session('synthetic','synthetic')
    ship.seats['worker']=shipmod.SeatSpec('worker','synthetic','worker','fake',('fake',),up=False)
    store=post.Store(ship.runtime);attempts=post.Attempts(ship.runtime)
    message=store.new('order','issuer','worker','Synthetic completed work')
    attempt=attempts.close(attempts.open(message,'worker',None,None,None),'queued' if change else 'delivered')
    calls=[];all_attempts=post.Attempts.all
    def read(self):calls.append(True);return all_attempts(self)
    monkeypatch.setattr(post.Attempts,'all',read)
    rechecks=[];recheck=post.Attempts.recheck_inventory
    def refresh(self,snapshot):rechecks.append(True);return recheck(self,snapshot)
    monkeypatch.setattr(post.Attempts,'recheck_inventory',refresh)
    metadata=[];inventory_metadata=post.Attempts._inventory_metadata
    def stat(self):metadata.append(True);return inventory_metadata(self)
    monkeypatch.setattr(post.Attempts,'_inventory_metadata',stat)
    recover=pending._reconcile_inventory
    def recovery(service,records):
        paths=recover(service,records)
        if change=='receipt':
            service.attempts.close(attempt,'delivered','receipt written by journal recovery')
        elif change=='in-place':
            inode=attempt.path.stat().st_ino
            fields=json.loads(attempt.path.read_text())
            attempt.path.write_text(json.dumps(fields | {'result':'delivered'}))
            assert attempt.path.stat().st_ino==inode
        return paths
    monkeypatch.setattr(pending,'_reconcile_inventory',recovery)
    seen=[];latest=post.Attempts.latest
    def legacy_results(self,seat=None,*,attempts=None):
        if attempts is not None:seen.append({a.message:a.result for a in attempts})
        return latest(self,seat,attempts=attempts)
    monkeypatch.setattr(post.Attempts,'latest',legacy_results)
    svc=Service(ship,SimpleNamespace())
    try:svc.reconcile()
    finally:svc.ledger.close()
    assert len(calls)==1 and len(rechecks)==1, 'journal reconciliation can write receipts; latest reductions reuse the fresh inventory after it'
    assert len(metadata)==2, 'the legacy recheck must stat fresh metadata after recovery'
    assert seen[-1][message.id]=='delivered', 'legacy handling sees both recovered receipts and in-place edits'


def test_service_publishes_an_optional_fresh_presentation_for_cold_status_jobs(tmp_path):
    from quarterdeck import servicectl
    (tmp_path/'ship.toml').write_text('[ship]\nname="synthetic"\n')
    ship=shipmod.load(tmp_path);ship.runtime.mkdir(parents=True)
    lock=servicectl.acquire_lock(ship.runtime)
    svc=Service(ship,SimpleNamespace())
    try:
        svc.tick()
        assert (ship.runtime/'status-line.json').exists(), 'cold status processes need a service-produced presentation, not reparsed history'
    finally:
        lock.close();svc.ledger.close()
