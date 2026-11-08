"""Supplied normal metadata and contentless-dequeue shapes, without live records."""

import json
from datetime import timedelta

import pytest

from quarterdeck import harness, input_receipt, output, watch
from quarterdeck.harness.base import RecordError
from .test_input_receipt import T0, append, bind, queue, row, seed_proof
from .test_pending_delivery import setup
from .test_receipt_rows import PRIVATE, bad, user, write
from tests.conftest import FIXTURES

NORMAL={
    'codex':('token_usage_record','world_state'),
    'claude':('api-request','api-request-blob','attachment','bridge-session','custom-title','agent-name','mode',
              'permission-mode','session-owner','atis-latch','last-prompt','file-history-delta','file-history-snapshot',
              'tools-used','api-request-shape','cost-state'),
}


@pytest.mark.parametrize('name',['codex','claude'])
def test_large_normal_and_unknown_record_has_counted_bounded_watch_diagnostics(setup,name):
    svc,seat,_,record=setup
    if name=='claude':
        record=seat.home/'claude.jsonl'
        seat=bind(svc,seat,name,record)
        screen=(FIXTURES/'claude/2.1.317/start.txt').read_text()
        svc.tmux.capture=lambda *a,**kw:screen
    normal=[{'type':kind,'private':PRIVATE} for kind in NORMAL[name]]*400
    unknown=[{'type':PRIVATE,'private':PRIVATE} for _ in range(3000)]
    write(record,[user(name,'before',1),*normal,*unknown,user(name,'after',3)])
    before=record.read_bytes(),record.stat().st_mtime_ns
    assert input_receipt.confirmed(name,record,'after',T0)=='delivered'
    data=watch.seat_reading(seat)
    summary=data['receipt_skips']
    assert isinstance(summary,dict), 'JSON diagnostics need totals and bounded examples'
    assert summary['total']==3000
    assert summary['counts']==[{'kind':'unknown','reason':'unknown row kind','count':3000}]
    assert 0<len(summary['examples'])<=8 and summary['omitted']==3000-len(summary['examples'])
    assert summary['examples'][0]['line']==len(normal)+2
    rendered='\n'.join(watch.render_full(data))
    diagnostic=[line for line in rendered.splitlines() if 'receipt skipped' in line]
    assert len(diagnostic)==1 and '3000' in diagnostic[0] and 'unknown row kind' in diagnostic[0]
    assert len(diagnostic[0])<500 and len(rendered.splitlines())<20
    assert len(json.dumps(output.value(summary)))<1500
    assert PRIVATE not in rendered and PRIVATE not in json.dumps(output.value(summary))
    assert not data['seat'].needs_human
    assert (record.read_bytes(),record.stat().st_mtime_ns)==before


@pytest.mark.parametrize('name',['codex','claude'])
def test_known_ignorable_rows_are_not_program_format_markers(tmp_path,name):
    record=tmp_path/'record.jsonl'
    write(record,[{'type':kind} for kind in NORMAL[name]])
    with pytest.raises(RecordError,match="another program's file"):
        input_receipt.confirmed(name,record,'transport',T0)
    write(record,[user(name,'transport',1),*({'type':kind} for kind in NORMAL[name])])
    assert harness.get(name).inputs(record).skipped==[]


@pytest.mark.parametrize('name',['codex','claude'])
def test_counts_include_every_skip_group_but_examples_keep_only_eight_lines(tmp_path,name):
    record=tmp_path/'record.jsonl'
    write(record,[user(name,'before',1),*[bad(name,'unknown')]*5,*[bad(name,'shape')]*5,
                  *[bad(name,'json')]*5,*[bad(name,'non-object')]*5,user(name,'after',3)])
    evidence=harness.get(name).inputs(record)
    summary=evidence.diagnostics()
    assert summary['total']==20 and summary['omitted']==12
    assert len(summary['counts'])==4 and all(item['count']==5 for item in summary['counts'])
    assert [item['line'] for item in summary['examples']]==list(range(2,10))
    assert input_receipt.confirmed(name,record,'after',T0)=='delivered'


def dequeue(second):return row('queue-operation',second,operation='dequeue')


@pytest.mark.parametrize('untimed_echo',[False,True])
def test_contentless_dequeue_waits_for_actual_input_with_original_enqueue_time(tmp_path,untimed_echo):
    record=tmp_path/'record.jsonl'
    write(record,[queue('transport','enqueue',1),dequeue(2)])
    assert input_receipt.confirmed('claude',record,'transport',T0) is None
    assert harness.get('claude').inputs(record).skipped==[]
    if untimed_echo:
        echo=user('claude','transport',3);echo['timestamp']=None
        append(record,echo)
        assert input_receipt.confirmed('claude',record,'transport',T0) is None
    append(record,user('claude','transport',3))
    assert input_receipt.confirmed('claude',record,'transport',T0)=='delivered'
    assert input_receipt.confirmed('claude',record,'transport',T0+timedelta(seconds=2)) is None
    evidence=harness.get('claude').inputs(record)
    assert evidence[-1].at==T0+timedelta(seconds=1) and evidence[-1].consumed_at==T0+timedelta(seconds=3)


@pytest.mark.parametrize('dequeue_first',[False,True])
@pytest.mark.parametrize('untimed_enqueue',[False,True])
def test_old_identical_input_echo_cannot_certify_fresh_attempt_and_fifo_survives(tmp_path,dequeue_first,untimed_enqueue):
    record=tmp_path/'record.jsonl'
    old=queue('same','enqueue',0)
    if untimed_enqueue:old['timestamp']=None
    rows=[old]
    rows.append(dequeue(1) if dequeue_first else queue('same','remove',1))
    rows.extend([queue('same','enqueue',2),user('claude','same',3)])
    write(record,rows)
    assert input_receipt.confirmed('claude',record,'same',T0+timedelta(seconds=1))=='queued'
    append(record,dequeue(4),user('claude','same',5))
    assert input_receipt.confirmed('claude',record,'same',T0+timedelta(seconds=1))=='delivered'
    assert input_receipt.confirmed('claude',record,'same',T0+timedelta(seconds=3)) is None


def test_contentless_dequeue_cannot_choose_arbitrary_queued_text(tmp_path):
    record=tmp_path/'record.jsonl'
    write(record,[queue('first','enqueue',1),queue('second','enqueue',2),dequeue(3)])
    for text in ('first','second'):
        assert input_receipt.confirmed('claude',record,text,T0) is None
    append(record,user('claude','second',4))
    assert input_receipt.confirmed('claude',record,'second',T0)=='delivered'
    assert input_receipt.confirmed('claude',record,'first',T0) is None
    write(record,[dequeue(1)])
    assert harness.get('claude').inputs(record).skipped==[]
    assert input_receipt.confirmed('claude',record,'anything',T0) is None


def test_real_shaped_queue_reconciles_once_only_after_actual_input(setup):
    svc,seat,live,_=setup
    record=seat.home/'claude.jsonl';write(record,[row('system',0)])
    seat=bind(svc,seat,'claude',record)
    message,proof=seed_proof(svc,seat)
    append(record,queue(proof.data['payload'],'enqueue',2),dequeue(3))
    svc.reconcile()
    assert svc.attempts.latest()[message.id]=='pending'
    append(record,user('claude',proof.data['payload'],4))
    svc.reconcile();svc.reconcile();svc.deliveries()
    assert svc.attempts.latest()[message.id]=='delivered'
    assert len(svc.attempts.for_message(message.id))==1
    assert live['pasted']==live['submitted']==0
