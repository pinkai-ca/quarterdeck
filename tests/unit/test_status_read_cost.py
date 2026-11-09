"""One reading uses one consistent attempt inventory, and the next reading refreshes it."""

from types import SimpleNamespace

from quarterdeck import post, ship as shipmod, watch
from quarterdeck.service import Service


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


def test_reconcile_reads_one_inventory_per_journal_and_attempt_phase(tmp_path,monkeypatch):
    ship=empty_ship(tmp_path)
    ship.pools['fake']=shipmod.Pool('fake','fake')
    ship.sessions['synthetic']=shipmod.Session('synthetic','synthetic')
    ship.seats['worker']=shipmod.SeatSpec('worker','synthetic','worker','fake',('fake',),up=False)
    store=post.Store(ship.runtime);attempts=post.Attempts(ship.runtime)
    message=store.new('order','issuer','worker','Synthetic completed work')
    attempts.close(attempts.open(message,'worker',None,None,None),'delivered')
    calls=[];all_attempts=post.Attempts.all
    def read(self):calls.append(True);return all_attempts(self)
    monkeypatch.setattr(post.Attempts,'all',read)
    svc=Service(ship,SimpleNamespace())
    try:svc.reconcile()
    finally:svc.ledger.close()
    assert len(calls)==2, 'journal reconciliation can write receipts; latest reductions reuse the fresh inventory after it'


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
