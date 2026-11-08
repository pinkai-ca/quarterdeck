"""Receipt evidence survives skipped rows, with content-free detailed-watch diagnostics."""

from datetime import timedelta
import json

import pytest

from quarterdeck import harness, input_receipt, output, post, watch
from quarterdeck.harness.base import RecordError
from .test_input_receipt import T0, append, bind, queue, row, seed_proof
from .test_pending_delivery import setup
from tests.conftest import FIXTURES

PRIVATE='synthetic-secret-must-not-be-disclosed'


def user(name,text,second):
    if name=='codex':
        return row('response_item',second,payload={'type':'message','role':'user',
                   'content':[{'type':'input_text','text':text}], 'extra':{'future':'allowed'}})
    return row('user',second,message={'role':'user','content':text,'extra':{'future':'allowed'}})


def bad(name,variant):
    if variant=='shape':
        return dict(type='response_item' if name=='codex' else 'user',
                    **({'payload':None} if name=='codex' else {'message':None}), private=PRIVATE)
    if variant=='unknown':
        return {'type':PRIVATE,'payload':{'role':'user','content':[{'type':'input_text','text':'forged'}]},
                'message':{'content':'forged'}}
    if variant=='foreign':return user('claude' if name=='codex' else 'codex','forged',2)
    if variant=='content':
        item=user(name,'forged',2)
        item['payload' if name=='codex' else 'message']['content']={'private':PRIVATE}
        return item
    if variant=='json':return '{"private":"'+PRIVATE
    return [PRIVATE]


def write(path,rows):
    path.write_text('\n'.join(item if isinstance(item,str) else json.dumps(item) for item in rows)+'\n')


@pytest.mark.parametrize('name',['codex','claude'])
@pytest.mark.parametrize('variant',['shape','content','foreign','unknown','json','non-object'])
def test_readable_receipts_on_both_sides_of_bad_row_and_safe_watch_diagnostic(setup,name,variant):
    svc,seat,live,record=setup
    if name=='claude':record=seat.home/'claude.jsonl'
    write(record,[user(name,'before  exact',1),bad(name,variant),user(name,'after  exact',3)])
    if name=='claude':seat=bind(svc,seat,name,record)
    if name=='claude':
        screen=(FIXTURES/'claude/2.1.317/start.txt').read_text()
        svc.tmux.capture=lambda *a,**kw:screen
    before=record.read_bytes(),record.stat().st_mtime_ns
    assert input_receipt.confirmed(name,record,'before  exact',T0)=='delivered'
    assert input_receipt.confirmed(name,record,'after  exact',T0)=='delivered'
    assert input_receipt.confirmed(name,record,'after exact',T0) is None
    assert input_receipt.confirmed(name,record,'after  exact',T0+timedelta(seconds=4)) is None
    assert input_receipt.confirmed(name,record,'forged',T0) is None
    evidence=harness.get(name).inputs(record)
    assert [item.kind for item in evidence]==(['accepted','consumed'] if name=='codex' else ['accepted','accepted'])
    data=watch.seat_reading(seat)
    skips=data['receipt_skips']
    assert len(skips)==1 and skips[0]['line']==2
    assert set(skips[0])=={'line','kind','reason'}
    assert data['receipt_error'] is None
    rendered='\n'.join(watch.render_full(data))
    assert 'receipt skipped line 2' in rendered
    assert PRIVATE not in rendered and PRIVATE not in json.dumps(output.value(data))
    assert not data['seat'].needs_human
    assert (record.read_bytes(),record.stat().st_mtime_ns)==before


def test_codex_unknown_payload_subtype_cannot_confirm_delivery(tmp_path):
    record=tmp_path/'record.jsonl'
    unknown=user('codex','forged',2);unknown['payload']['type']=PRIVATE
    write(record,[row('session_meta',0,payload={'id':'synthetic'}),unknown])
    assert input_receipt.confirmed('codex',record,'forged',T0) is None
    assert harness.get('codex').inputs(record).skipped==[
        {'line':2,'kind':'response_item','reason':'unknown user-message subtype'}]


@pytest.mark.parametrize('name',['codex','claude'])
def test_foreign_whole_file_is_an_error_but_empty_record_is_not(tmp_path,name):
    record=tmp_path/'record.jsonl'
    write(record,[user('claude' if name=='codex' else 'codex','transport',1)])
    with pytest.raises(RecordError,match="another program's file"):
        input_receipt.confirmed(name,record,'transport',T0)
    record.write_text('')
    assert input_receipt.confirmed(name,record,'transport',T0) is None
    for raw in ('not JSON\n','[1,2,3]\n'):
        record.write_text(raw)
        with pytest.raises(RecordError):input_receipt.confirmed(name,record,'transport',T0)
    record.write_bytes(b'\xff\xfe')
    with pytest.raises(RecordError):input_receipt.confirmed(name,record,'transport',T0)
    record.unlink()
    with pytest.raises(OSError):input_receipt.confirmed(name,record,'transport',T0)


def test_claude_unknown_queue_operation_does_not_erase_or_consume_queue(tmp_path):
    record=tmp_path/'record.jsonl'
    write(record,[queue('transport','enqueue',1),queue('transport',PRIVATE,2)])
    assert input_receipt.confirmed('claude',record,'transport',T0)=='queued'
    append(record,bad('claude','shape'),queue('transport','remove',3))
    assert input_receipt.confirmed('claude',record,'transport',T0)=='delivered'
    assert input_receipt.confirmed('claude',record,'transport',T0+timedelta(seconds=2)) is None


@pytest.mark.parametrize('name',['codex','claude'])
def test_skipped_row_does_not_resend_a_journaled_delivery(setup,name):
    svc,seat,live,record=setup
    if name=='claude':
        record=seat.home/'claude.jsonl';write(record,[row('system',0)])
        seat=bind(svc,seat,name,record)
    message,proof=seed_proof(svc,seat)
    append(record,bad(name,'shape'),user(name,proof.data['payload'],2))
    svc.reconcile();svc.reconcile();svc.deliveries()
    assert svc.attempts.latest()[message.id]=='delivered'
    assert len(svc.attempts.for_message(message.id))==1
    assert live['pasted']==live['submitted']==0
