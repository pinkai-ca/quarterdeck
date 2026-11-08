"""Public rollup regressions after full service reconciliation."""

from datetime import datetime, timedelta, timezone

from quarterdeck.ledger import Ledger
from quarterdeck.harness.types import Turn, Tokens
from quarterdeck.post import Store, Message

AT=datetime(2032,1,1,tzinfo=timezone.utc)


def put(store,id,kind,sender,to,at,re=None):
    message=Message(id,kind,sender,to,AT+timedelta(seconds=at),'synthetic',re)
    store.write(message)
    return message


def worked(index,start,end):
    return Turn(index,AT+timedelta(seconds=start),AT+timedelta(seconds=end),'input','reply','model',Tokens(1,2,3,4),cost=2)


def test_implementation_review_merge_roll_up_under_one_explicit_change(tmp_path):
    store=Store(tmp_path)
    root=put(store,'ord-root','order','desk','captain',0)
    implementation=put(store,'ord-implementation','order','captain','engineer',1,root.id)
    report=put(store,'rep-implementation','report','engineer','captain',15,implementation.id)
    question=put(store,'que-merge','question','captain','desk',25,report.id)
    put(store,'ans-merged','answer','desk','captain',35,question.id)
    with Ledger(tmp_path/'ledger.sqlite') as book:
        for i,seat in enumerate(('engineer','captain','desk')):
            record=tmp_path/seat;record.write_text('')
            book.ingest(seat,'pool',record,[worked(i,10+i*10,19+i*10)],store)
        book.attributions.replay(store,tmp_path,{str(tmp_path/seat):{'fake'} for seat in ('engineer','captain','desk')})
        rows=book.rollup('order')
        assert len(rows)==1 and rows[0].key==root.id and rows[0].turns==3


def test_duplicate_report_and_foreign_claim_do_not_invent_attribution(tmp_path):
    store=Store(tmp_path)
    own=put(store,'ord-own','order','desk','worker',0)
    other=put(store,'ord-other','order','desk','other',0)
    put(store,'rep-close','report','worker','desk',1,own.id)
    put(store,'ack-foreign','ack','worker','desk',11,other.id)
    put(store,'rep-duplicate','report','worker','desk',12,own.id)
    with Ledger(tmp_path/'ledger.sqlite') as book:
        (tmp_path/'record').write_text('')
        book.ingest('worker','pool',tmp_path/'record',[worked(0,10,20)],store)
        book.attributions.replay(store,tmp_path,{str(tmp_path/'record'):{'fake'}})
        assert book.rollup('order')[0].key=='-'


def test_two_changes_in_one_turn_are_shared_without_fraction_or_last_report_choice(tmp_path):
    store=Store(tmp_path)
    for i in range(2):
        order=put(store,f'ord-change-{i}','order','desk','worker',0)
        put(store,f'rep-change-{i}','report','worker','desk',11+i,order.id)
    with Ledger(tmp_path/'ledger.sqlite') as book:
        (tmp_path/'record').write_text('')
        book.ingest('worker','pool',tmp_path/'record',[worked(0,10,20)],store)
        book.attributions.replay(store,tmp_path,{str(tmp_path/'record'):{'fake'}})
        rows=book.rollup('order')
        assert len(rows)==1 and rows[0].key=='[shared]' and rows[0].turns==1
        assert rows[0].read_cached+rows[0].read_uncached+rows[0].cache_written+rows[0].written==10
