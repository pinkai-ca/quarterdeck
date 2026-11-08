"""Fresh owned-record evidence survives queue consumption and later input rows."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest

from quarterdeck import input_receipt, pending, post, watch
from quarterdeck.harness.synthetic_record import AntigravityShape
from quarterdeck.record_owner import register
from quarterdeck.service import Service
from .test_pending_delivery import setup, send, input_row  # shared synthetic seat fixture

T0 = datetime(2025, 1, 1, tzinfo=timezone.utc)


def row(kind, second, **data):
    return dict(type=kind, timestamp=(T0+timedelta(seconds=second)).isoformat(), **data)


def append(path, *rows):
    with path.open('a') as f:
        for item in rows:
            f.write(json.dumps(item)+'\n')


def queue(text, operation, second):
    return row('queue-operation', second, operation=operation,
               content='<pasted_content id="synthetic">\n'+text+'\n</pasted_content>')


def test_claude_timestamped_queue_and_consumption_without_new_user_row(tmp_path):
    record = tmp_path/'claude.jsonl'
    text = 'exact  transport\nsecond line'
    append(record, row('user', 0, message={'role':'user','content':'initial'}))
    assert input_receipt.confirmed('claude',record,text,T0+timedelta(seconds=1)) is None
    append(record,queue(text,'enqueue',2))
    assert input_receipt.confirmed('claude',record,text,T0+timedelta(seconds=1)) == 'queued'
    append(record,queue(text,'remove',3),row('assistant',4,message={'content':[],'model':'synthetic'}))
    assert input_receipt.confirmed('claude',record,text,T0+timedelta(seconds=1)) == 'delivered'
    assert input_receipt.confirmed('claude',record,text,T0+timedelta(seconds=5)) is None
    assert input_receipt.confirmed('claude',record,text.replace('  ',' '),T0) is None
    # Fresh removal of an old identical enqueue cannot confirm a new attempt.
    other = tmp_path/'old.jsonl'
    append(other,queue(text,'enqueue',0),queue(text,'dequeue',3))
    assert input_receipt.confirmed('claude',other,text,T0+timedelta(seconds=1)) is None
    append(other,queue(text,'enqueue',4),queue(text,'dequeue',5))
    assert input_receipt.confirmed('claude',other,text,T0+timedelta(seconds=1)) == 'delivered'


def bind(svc, old, name, path):
    svc.ship.seats[old.name] = replace(old.spec,harness=name)
    seat = svc.seat(old.name)
    launch_path = seat.folder/'launch.json'
    data = json.loads(launch_path.read_text())
    data.update(harness=name,argv=[{'claude':'claude','antigravity':'agy'}[name]])
    launch_path.write_text(json.dumps(data))
    (seat.folder/'record').write_text(str(path)+'\n')
    register(seat.folder,path,name)
    return seat


def seed_proof(svc, seat):
    message = svc.store.new('order','desk',seat.name,'exact  queued transport')
    a = svc.attempts.open(message,seat.name,'%1',123,seat.launched().at)
    proof = pending.Proof.begin(seat,[a],post.typed([message]))
    proof.save(phase='submitted',shown=proof.data['payload'].rstrip('\n'),since=(T0+timedelta(seconds=1)).isoformat())
    svc.attempts.close(a,'pending','submitted once')
    return message, proof


def test_service_restart_reconciles_claude_consumed_exactly_once(setup):
    svc, original, live, _ = setup
    record = original.home/'claude.jsonl'
    append(record,row('user',0,message={'role':'user','content':'initial'}))
    seat = bind(svc,original,'claude',record)
    message, proof = seed_proof(svc,seat)
    append(record,queue(proof.data['payload'],'enqueue',2))
    svc.reconcile()
    assert svc.attempts.latest()[message.id] == 'queued'
    append(record,queue(proof.data['payload'],'remove',3),row('user',4,message={'role':'user','content':'later input'}))
    restarted = Service(svc.ship,svc.tmux)
    try:
        restarted.reconcile(); restarted.reconcile()
        assert svc.attempts.latest()[message.id] == 'delivered'
        assert len(svc.attempts.for_message(message.id)) == 1
        assert live['pasted'] == live['submitted'] == 0  # reconciliation never sends keys
    finally:
        restarted.ledger.close()


def test_antigravity_every_same_execution_input_has_its_own_time(setup, monkeypatch):
    from quarterdeck.harness import synthetic_record as fakerecord
    from quarterdeck.harness import antigravity

    svc, original, live, _ = setup
    monkeypatch.setattr(fakerecord,'now',lambda:T0)
    shape = AntigravityShape(original.home,original.ship.folder,'synthetic','synthetic-model')
    shape.begin('initial','synthetic-model','high')
    seat = bind(svc,original,'antigravity',shape.path)
    message, proof = seed_proof(svc,seat)
    # Identical history before the attempt cannot prove it was taken.
    shape.consumed(proof.data['payload'])
    svc.reconcile()
    assert svc.attempts.latest()[message.id] == 'pending'
    monkeypatch.setattr(fakerecord,'now',lambda:T0+timedelta(seconds=2))
    shape.consumed(proof.data['payload'])
    monkeypatch.setattr(fakerecord,'now',lambda:T0+timedelta(seconds=3))
    shape.consumed('later unrelated input in the same execution')
    state = antigravity.state(shape.path)
    assert state.last_input != proof.data['payload'] and proof.data['payload'] in state.consumed
    files = list(shape.path.parent.glob(shape.path.name+'*'))
    before = {str(p):(hashlib.sha256(p.read_bytes()).hexdigest(),p.stat().st_mtime_ns) for p in files}
    restarted = Service(svc.ship,svc.tmux)
    try:
        restarted.reconcile(); restarted.reconcile()
        assert svc.attempts.latest()[message.id] == 'delivered'
        assert len(svc.attempts.for_message(message.id)) == 1
        assert live['pasted'] == live['submitted'] == 0
    finally:
        restarted.ledger.close()
    after = {str(p):(hashlib.sha256(p.read_bytes()).hexdigest(),p.stat().st_mtime_ns)
             for p in shape.path.parent.glob(shape.path.name+'*')}
    assert after == before  # law: one-folder
    assert input_receipt.confirmed('antigravity',shape.path,proof.data['payload'],T0+timedelta(seconds=4)) is None


@pytest.mark.parametrize('broken',['{ broken','{}','{"phase":"submitted","identity":{}}'])
def test_corrupt_journal_does_not_block_another_seats_confirmation(setup, broken):
    svc, seat, live, record = setup
    first, damaged = send(svc,seat)
    # A second proven seat has its own owned record and exact transport.
    spec = replace(seat.spec,name='second')
    svc.ship.seats['second'] = spec
    other = svc.seat('second')
    other.folder.mkdir(parents=True)
    (other.folder/'launch.json').write_text((seat.folder/'launch.json').read_text())
    second_record = other.home/'sessions'/'synthetic.jsonl'
    second_record.parent.mkdir(parents=True)
    second_record.write_text(record.read_text())
    (other.folder/'record').write_text(str(second_record)+'\n')
    register(other.folder,second_record,'codex')
    second, intact = seed_proof(svc,other)
    input_row(second_record,intact.data['payload'],post.now())
    damaged.path.write_text(broken)
    svc.reconcile(); svc.reconcile()
    assert svc.attempts.latest()[second.id] == 'delivered'
    assert svc.attempts.latest()[first.id] == 'pending'
    assert len(svc.attempts.for_message(second.id)) == 1
    assert 'worker needs a human' in watch.facts(svc.ship,svc.tmux,walls=[],login=[]).line()
    assert 'journal unreadable' in watch.facts(svc.ship,svc.tmux,walls=[],login=[]).line()
    svc.deliveries()
    assert live['pasted'] == live['submitted'] == 1
