"""Synthetic immutable threads and conserved, replayable whole-turn attribution."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import post
from quarterdeck.order_attribution import Threads
from quarterdeck.harness.types import Input, Tokens, Turn
from quarterdeck.ledger import Ledger
from quarterdeck.harness.fake import FakeHarness

AT = datetime(2032, 1, 1, tzinfo=timezone.utc)


def message(store, name, kind='order', sender='issuer', to='worker', second=0, re=None, body='synthetic work'):
    m = post.Message(name, kind, sender, to, AT+timedelta(seconds=second), body, re)
    store.write(m)
    return m


def turn(index=0, start=10, end=20, text='synthetic input'):
    return Turn(index, AT+timedelta(seconds=start), AT+timedelta(seconds=end), text, 'reply', 'model', Tokens(1,2,3,4), cost=2)


def decide(store, seat='worker', start=10, end=20, received=()):
    return Threads(store).decide(seat, AT+timedelta(seconds=start), AT+timedelta(seconds=end), received)


def test_implementation_review_merge_and_test_orders_follow_explicit_change_thread(tmp_path):
    store=post.Store(tmp_path)
    root=message(store,'ord-change',to='reviewer')
    child=message(store,'ord-implement',sender='reviewer',second=1,re=root.id)
    task=message(store,'ord-test',sender='worker',to='helper',second=11,re=child.id)
    message(store,'rep-test','report','helper','worker',12,task.id)
    report=message(store,'rep-implement','report','worker','reviewer',19,child.id)
    question=message(store,'que-merge','question','reviewer','issuer',31,report.id)
    message(store,'ans-merge','answer','issuer','reviewer',41,question.id)
    for seat,start,end in [('worker',10,20),('helper',10,20),('reviewer',30,35),('issuer',40,45)]:
        result=decide(store,seat,start,end)
        assert result.status=='attributed' and result.orders==(root.id,)


def test_separate_changes_and_campaign_are_not_inferred_from_words(tmp_path):
    store=post.Store(tmp_path)
    message(store,'ord-campaign',to='reviewer',body='umbrella mentions ord-first and ord-second')
    a=message(store,'ord-first',sender='reviewer',second=11)
    b=message(store,'ord-second',sender='reviewer',second=12)
    result=decide(store,'reviewer')
    assert result.status=='shared' and result.orders==(a.id,b.id)
    assert 'campaign' not in result.key


def test_full_envelope_is_required_and_quoted_ids_and_truncated_messages_prove_nothing(tmp_path):
    store=post.Store(tmp_path); m=message(store,'ord-change')
    graph=Threads(store)
    for text in (m.id, 'quoted '+post.typed([m]), post.typed([m])[:-10], '```\n'+post.typed([m])+'\n```'):
        assert graph.received(text,AT+timedelta(seconds=12),'worker')==[]
    assert graph.received(post.typed([m]),AT-timedelta(seconds=1),'worker')==[]
    assert graph.received(post.typed([m]),AT+timedelta(seconds=12),'foreign')==[]
    assert graph.received(post.typed([m]),AT+timedelta(seconds=12),'worker')[0]['message']==m.id


def test_question_is_not_an_order_and_invalid_claims_and_duplicate_reports_cannot_create_work(tmp_path):
    store=post.Store(tmp_path)
    question=message(store,'que-alone','question')
    other=message(store,'ord-foreign',to='other')
    message(store,'ack-question','ack','worker','issuer',11,question.id)
    message(store,'ack-foreign','ack','worker','issuer',12,other.id)
    assert decide(store).status=='unassociated'
    order=message(store,'ord-closed',second=1)
    message(store,'rep-first','report','worker','issuer',2,order.id)
    message(store,'rep-duplicate','report','worker','issuer',13,order.id)
    message(store,'rep-foreign','report','worker','issuer',14,other.id)
    assert decide(store).status=='unassociated'
    assert Threads(store).root(question)==(None,None)


def test_claim_fallback_does_not_override_direct_work_or_choose_last_of_two_changes(tmp_path):
    store=post.Store(tmp_path)
    a=message(store,'ord-alpha'); b=message(store,'ord-beta')
    message(store,'ack-claimed','ack','worker','issuer',1,a.id)
    assert decide(store).orders==(a.id,)
    message(store,'ans-work','answer','worker','issuer',11,b.id)
    assert decide(store).orders==(b.id,)
    message(store,'rep-close','report','worker','issuer',12,a.id)
    assert decide(store).status=='shared' and decide(store).orders==(a.id,b.id)


def test_multiple_open_orders_without_direct_evidence_are_uncertain_not_shared_work(tmp_path):
    store=post.Store(tmp_path); message(store,'ord-alpha'); message(store,'ord-beta')
    assert decide(store).status=='unresolved'


def test_equal_latest_claim_times_cannot_arbitrarily_choose_between_changes(tmp_path):
    store=post.Store(tmp_path)
    a=message(store,'ord-alpha'); b=message(store,'ord-beta')
    message(store,'ack-first','ack','worker','issuer',1,a.id)
    message(store,'ack-second','ack','worker','issuer',1,b.id)
    result=decide(store)
    assert result.status=='unresolved' and result.orders==(a.id,b.id)
    message(store,'ack-later','ack','worker','issuer',2,a.id)
    assert decide(store).orders==(a.id,)


@pytest.mark.parametrize('kind',['missing','cycle','conflict','future'])
def test_broken_threads_stay_unresolved(tmp_path,kind):
    store=post.Store(tmp_path)
    if kind=='conflict':
        a=post.Message('ord-same','order','issuer','worker',AT,'a')
        b=post.Message('ord-same','order','other','worker',AT,'b')
        graph=Threads(SimpleNamespace(all=lambda:[a,b]))
        assert graph.root(a)[1]=='conflicting immutable message identity'
        return
    target='ord-missing' if kind=='missing' else 'ord-b'
    message(store,'ord-a',second=11,re=target,sender='worker')
    if kind=='cycle': message(store,'ord-b',second=11,re='ord-a')
    if kind=='future': message(store,'ord-b',second=12)
    assert decide(store).status=='unresolved'


def seed_book(tmp_path,store,seat='worker'):
    path=tmp_path/'record'
    path.write_text('synthetic source')
    book=Ledger(tmp_path/'ledger.sqlite')
    book.ingest(seat,'pool',path,[turn()],store)
    return book,path


def measurement_snapshot(book):
    cols=[r[1] for r in book.db.execute('PRAGMA table_info(turns)')]
    return (book.db.execute('SELECT * FROM turns').fetchall(), book.db.execute('SELECT * FROM tool_calls').fetchall(),
            book.db.execute('SELECT * FROM turn_costs').fetchall(),cols)


def test_replay_existing_unchanged_retired_record_new_posts_restart_and_missing_source(tmp_path,monkeypatch):
    store=post.Store(tmp_path)
    book,path=seed_book(tmp_path,store,'retired')
    before=measurement_snapshot(book)
    order=message(store,'ord-change',to='other',second=1)
    reply=message(store,'ans-review','answer','retired','issuer',15,order.id)
    monkeypatch.setattr(FakeHarness,'attribution_inputs',lambda self,p:[])
    bindings={str(path):{'fake'}}
    assert book.attributions.replay(store,tmp_path,bindings)==1
    assert book.rollup('order')[0].key==order.id
    assert book.attributions.replay(store,tmp_path,bindings)==0
    assert measurement_snapshot(book)==before
    book.close(); path.unlink()
    with Ledger(tmp_path/'ledger.sqlite') as book:
        book.attributions.replay(store,tmp_path,bindings)
        assert book.rollup('order')[0].key==order.id
        assert measurement_snapshot(book)==before
        assert book.attributions.replay(store,tmp_path,bindings)==0


def test_received_proof_survives_missing_record_but_old_writer_change_invalidates_it(tmp_path,monkeypatch):
    store=post.Store(tmp_path); order=message(store,'ord-change',to='worker')
    book,path=seed_book(tmp_path,store)
    monkeypatch.setattr(FakeHarness,'attribution_inputs',lambda self,p: [Input(post.typed([order]),AT+timedelta(seconds=11))] if p.exists() else (_ for _ in ()).throw(OSError()))
    bindings={str(path):{'fake'}}
    book.attributions.replay(store,tmp_path,bindings)
    before=measurement_snapshot(book)
    path.unlink();book.attributions.replay(store,tmp_path,bindings)
    assert book.attributions.details()[0]['evidence'][0]['route']=='input'
    assert book.attributions.details()[0]['evidence'][0]['retained'] is True
    assert 'retained verified incoming' in book.attributions.details()[0]['reason']
    assert measurement_snapshot(book)==before
    book.db.execute('UPDATE turns SET started=?,ended=?',((AT+timedelta(seconds=30)).isoformat(),(AT+timedelta(seconds=35)).isoformat()))
    assert book.attributions.details()[0]['status']=='pending'
    assert book.rollup('order')[0].key.startswith('pending:')
    book.attributions.replay(store,tmp_path,bindings)
    assert all(e['route']!='input' for e in book.attributions.details()[0]['evidence'])
    book.close()


def test_shared_measurements_are_counted_once_and_side_replay_does_not_invalidate_cost(tmp_path,monkeypatch):
    store=post.Store(tmp_path)
    a=message(store,'ord-first',sender='worker',second=11)
    b=message(store,'ord-second',sender='worker',second=12)
    book,path=seed_book(tmp_path,store)
    monkeypatch.setattr(FakeHarness,'attribution_inputs',lambda self,path:[])
    book.attributions.replay(store,tmp_path,{str(path):{'fake'}})
    row=book.rollup('order')[0]
    assert row.key=='[shared]' and row.turns==1 and row.read_cached+row.read_uncached+row.cache_written+row.written==10
    assert row.cost==2 and book.costs.get(path,0).valid==1
    assert book.attributions.details()[0]['orders']==[a.id,b.id]
    book.close()


def test_service_revisits_unchanged_retired_record_on_new_post_and_restart(tmp_path,monkeypatch):
    from quarterdeck import service, record_owner
    from quarterdeck.service import Service
    from quarterdeck.ship import Ship
    ship=Ship(tmp_path,'synthetic','operator',tmp_path,{}, {}, {})
    monkeypatch.setattr(service.machine,'processes',lambda:{})
    calls=[]
    def inputs(self,path): calls.append(path); return []
    monkeypatch.setattr(FakeHarness,'attribution_inputs',inputs)
    svc=Service(ship,SimpleNamespace())
    path=ship.runtime/'retired-record';path.write_text('synthetic')
    svc.ledger.ingest('retired','pool',path,[turn()],svc.store)
    record_owner.register(ship.runtime/'seats/retired',path,'fake')
    before=measurement_snapshot(svc.ledger)
    svc.account()
    assert svc.ledger.attributions.details()[0]['status']=='unassociated'
    root=message(svc.store,'ord-root',to='other')
    message(svc.store,'ans-work','answer','retired','issuer',15,root.id)
    svc.account()
    assert calls==[path], 'immutable post arrival revisits cached source evidence, not only changed files'
    assert svc.ledger.rollup('order')[0].key==root.id
    assert measurement_snapshot(svc.ledger)==before
    svc.ledger.close()
    svc=Service(ship,SimpleNamespace())
    svc.account()
    assert calls==[path,path] and svc.ledger.rollup('order')[0].key==root.id
    assert measurement_snapshot(svc.ledger)==before
    svc.ledger.close()


def test_outgoing_post_cannot_choose_between_overlapping_records(tmp_path,monkeypatch):
    store=post.Store(tmp_path)
    message(store,'ord-work',sender='worker',second=15)
    book,path=seed_book(tmp_path,store)
    second=tmp_path/'other';second.write_text('synthetic')
    book.ingest('worker','pool',second,[turn()],store)
    monkeypatch.setattr(FakeHarness,'attribution_inputs',lambda self,path:[])
    book.attributions.replay(store,tmp_path,{str(path):{'fake'},str(second):{'fake'}})
    assert all(row['status']=='unresolved' and 'overlaps' in row['reason'] for row in book.attributions.details())
    assert sum(row.turns for row in book.rollup('order'))==2
    book.close()


def test_queued_input_without_consumption_is_not_turn_evidence(tmp_path,monkeypatch):
    store=post.Store(tmp_path);order=message(store,'ord-other',to='worker')
    message(store,'rep-close','report','worker','issuer',1,order.id)
    book,path=seed_book(tmp_path,store)
    monkeypatch.setattr(FakeHarness,'attribution_inputs',lambda self,path:[Input(post.typed([order]),AT+timedelta(seconds=11),'queued')])
    book.attributions.replay(store,tmp_path,{str(path):{'fake'}})
    assert book.attributions.details()[0]['status']=='unassociated'
    book.close()


def test_watch_read_evidence_belongs_only_to_actual_read_turn(tmp_path):
    from .test_watch_attribution import call,result,origin,write,OUTPUT
    store=post.Store(tmp_path)
    order=message(store,'ord-worker',to='implementer')
    report=message(store,'rep-done','report','implementer','reviewer',9,order.id)
    path=write(tmp_path/'watch.jsonl',[call('watch','qd inbox --wait'),origin(),call('read',f'cat {OUTPUT}',11),result('read',post.typed([report]),12)])
    with Ledger(tmp_path/'ledger.sqlite') as book:
        book.ingest('reviewer','pool',path,[turn(0,0,5),turn(1,10,20)],store)
        book.attributions.replay(store,tmp_path,{str(path):{'claude'}})
        rows=book.attributions.details()
        assert rows[0]['status']=='unassociated'
        assert rows[1]['status']=='attributed' and rows[1]['orders']==[order.id]
        assert rows[1]['evidence'][0]['route']=='watch' and not rows[1]['evidence'][0]['retained']


def test_input_overlapping_turn_intervals_is_unresolved(tmp_path,monkeypatch):
    store=post.Store(tmp_path);order=message(store,'ord-other',to='worker')
    message(store,'rep-close','report','worker','issuer',1,order.id)
    book,path=seed_book(tmp_path,store)
    book.ingest('worker','pool',path,[turn(1,11,21)],store)
    monkeypatch.setattr(FakeHarness,'attribution_inputs',lambda self,path:[Input(post.typed([order]),AT+timedelta(seconds=12))])
    book.attributions.replay(store,tmp_path,{str(path):{'fake'}})
    assert all(row['status']=='unresolved' and 'overlaps' in row['reason'] for row in book.attributions.details())
    book.close()


def test_an_answered_question_no_longer_claims_the_seats_later_turns(tmp_path):
    from quarterdeck.ledger import attribution
    store = post.Store(tmp_path)
    question = message(store, 'que-asked', 'question', 'issuer', 'worker', 1)
    message(store, 'ack-asked', 'ack', 'worker', 'issuer', 2, question.id)
    before = turn(0, 3, 5)
    assert attribution(store, 'worker', before) == question.id, 'the turn before the answer was spent on the question'
    message(store, 'ans-asked', 'answer', 'worker', 'issuer', 6, question.id)
    assert attribution(store, 'worker', before) == question.id, 'a later answer does not rewrite an earlier turn'
    later = turn(1, 10, 20)
    assert attribution(store, 'worker', later) != question.id, 'a closed question claims no later turn'


def test_an_order_claim_still_ends_only_with_its_addressees_report(tmp_path):
    from quarterdeck.ledger import attribution
    store = post.Store(tmp_path)
    order = message(store, 'ord-claimed', 'order', 'issuer', 'worker', 1)
    message(store, 'ack-claimed', 'ack', 'worker', 'issuer', 2, order.id)
    message(store, 'ans-other', 'answer', 'issuer', 'worker', 3, order.id)
    message(store, 'rep-stranger', 'report', 'issuer', 'worker', 4, order.id)
    assert attribution(store, 'worker', turn(0, 5, 6)) == order.id, 'only the addressee closes an order'
    message(store, 'rep-claimed', 'report', 'worker', 'issuer', 7, order.id)
    assert store.claimed('worker', AT + timedelta(seconds=8)) is None
