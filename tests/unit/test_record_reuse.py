"""Parsing old native rows is avoidable; mutable source evidence still speaks."""

import json
import os
from pathlib import Path

import pytest

from quarterdeck.harness import claude, codex, json_record
from quarterdeck.harness.synthetic_record import ClaudeShape, CodexShape


@pytest.mark.parametrize('shape,reader', [(ClaudeShape, claude), (CodexShape, codex)])
def test_append_reuses_parsed_prefix_across_state_and_input_reads(tmp_path, monkeypatch, shape, reader):
    record=shape(tmp_path/'home', tmp_path, 'synthetic', 'synthetic')
    for i in range(5):
        record.begin(f'Synthetic input {i}', 'synthetic', 'low')
        record.end('Finished')
    record.begin('Current work', 'synthetic', 'low')
    reader.read(record.path); reader.inputs(record.path)
    decoded=[]; original=json.loads
    def load(raw, *args, **kwargs):
        decoded.append(raw)
        return original(raw, *args, **kwargs)
    monkeypatch.setattr(json_record.json, 'loads', load)
    if shape is ClaudeShape:
        record.assistant([{'type':'text','text':'Synthetic growing step'}], None)
    else:
        record.line('response_item', {'type':'message','role':'assistant','content':[{'type':'output_text','text':'Synthetic growing step'}]})
    state, turns=reader.read(record.path)
    inputs=reader.inputs(record.path)
    assert state.turn_open is True and len(turns)==5 and inputs[-1].text=='Current work'
    assert len(decoded)<=1, 'unchanged native rows must not be JSON-decoded for every state/receipt walk'


@pytest.mark.parametrize('change', ['rewrite', 'truncate', 'replace', 'same-size'])
def test_source_mutation_rebuilds_proof_instead_of_trusting_an_append(tmp_path, change):
    record=ClaudeShape(tmp_path/'home',tmp_path,'synthetic','synthetic')
    record.begin('First synthetic input', 'synthetic','low');record.end('Finished')
    assert claude.state(record.path).last_input=='First synthetic input'
    raw=record.path.read_text().replace('First synthetic input','Other synthetic input')
    if change=='truncate':
        raw=raw.splitlines()[0]+'\n'
    if change=='replace':
        other=record.path.with_suffix('.new');other.write_text(raw);os.replace(other,record.path)
    else:
        before=record.path.stat();record.path.write_text(raw)
        if change=='same-size':os.utime(record.path,ns=(before.st_atime_ns,before.st_mtime_ns))
    assert claude.state(record.path).last_input=='Other synthetic input'
    assert claude.inputs(record.path)[0].text=='Other synthetic input'


def test_partial_row_completion_and_public_mutation_do_not_poison_cached_native_rows(tmp_path):
    record=ClaudeShape(tmp_path/'home',tmp_path,'synthetic','synthetic')
    record.begin('Synthetic input','synthetic','low')
    original=record.path.read_text()
    record.path.write_text(original+'{"type":"assistant","message":')
    assert claude.state(record.path).pending_tools is None
    assert claude.inputs(record.path).diagnostics()['total']==1
    with record.path.open('a') as out:
        out.write('{"role":"assistant","content":[{"type":"text","text":"Synthetic reply"}]}}\n')
    assert claude.state(record.path).pending_tools==()
    assert claude.inputs(record.path).diagnostics()['total']==0
    exposed=json_record.object_lines(record.path)
    exposed[0][1]['message']['content']='caller mutation'
    assert claude.state(record.path).last_input=='Synthetic input'


def test_failed_refresh_cannot_return_or_certify_the_previous_success(tmp_path,monkeypatch):
    record=ClaudeShape(tmp_path/'home',tmp_path,'synthetic','synthetic')
    record.begin('First synthetic input','synthetic','low')
    claude.state(record.path)
    record.path.write_text(record.path.read_text().replace('First synthetic input','Other synthetic input'))
    read=Path.open
    with monkeypatch.context() as patch:
        def denied(path,*args,**kwargs):
            if path==record.path:raise PermissionError('synthetic unavailable source')
            return read(path,*args,**kwargs)
        patch.setattr(Path,'open',denied)
        with pytest.raises(PermissionError):claude.state(record.path)
    assert claude.state(record.path).last_input=='Other synthetic input'


def test_envelope_matching_uses_the_complete_header_bucket_without_scanning_every_post(tmp_path):
    from quarterdeck import post
    from quarterdeck.order_attribution import Threads
    store=post.Store(tmp_path)
    first=store.new('order','issuer','worker','First synthetic body')
    second=store.new('question','issuer','worker','Second synthetic body')
    for i in range(100):store.new('report','other','other',f'Unrelated synthetic body {i}')
    graph=Threads(store);scanned=[]
    class Counted(dict):
        def items(self):
            scanned.extend(self)
            return super().items()
    graph.envelopes=Counted(graph.envelopes)
    assert graph.received('ordinary synthetic input',post.now(),'worker')==[]
    assert [item['message'] for item in graph.received(post.typed([first,second]),post.now(),'worker')]==[first.id,second.id]
    assert graph.received(post.typed([first])+'extra synthetic text',post.now(),'worker')==[]
    assert scanned==[], 'matching one envelope must not inspect every unrelated envelope'


@pytest.mark.parametrize('shape,reader', [(ClaudeShape, claude), (CodexShape, codex)])
def test_growing_noninput_steps_do_not_replay_old_native_state_or_input_walks(tmp_path, monkeypatch, shape, reader):
    record=shape(tmp_path/'home',tmp_path,'synthetic','synthetic')
    record.begin('Synthetic active input','synthetic','low')
    reader.read(record.path);reader.inputs(record.path)
    taken=[];walked=[];take=reader._Reader.take;rows=reader.input_rows
    def track(self,entry):
        taken.append(entry)
        return take(self,entry)
    def inputs(*args,**kwargs):
        walked.append(True)
        return rows(*args,**kwargs)
    monkeypatch.setattr(reader._Reader,'take',track)
    monkeypatch.setattr(reader,'input_rows',inputs)
    if shape is ClaudeShape:record.assistant([{'type':'text','text':'Synthetic growing step'}],None)
    else:record.line('response_item',{'type':'message','role':'assistant','content':[{'type':'output_text','text':'Synthetic growing step'}]})
    assert reader.state(record.path).turn_open is True
    assert reader.inputs(record.path)[0].text=='Synthetic active input'
    assert len(taken)==1 and not walked, 'growing output cannot replay the entire state and receipt history'


def test_output_only_source_change_refreshes_evidence_without_rebuilding_attribution(tmp_path,monkeypatch):
    from datetime import timedelta
    from quarterdeck import attribution_cache,post
    from quarterdeck.harness.fake import FakeHarness
    from quarterdeck.harness.types import Input
    from quarterdeck.ledger import Ledger
    from .test_order_attribution import AT,message,measurement_snapshot,turn
    store=post.Store(tmp_path)
    first=message(store,'ord-first');second=message(store,'ord-second')
    for order in (first,second):message(store,'rep-'+order.id,'report','worker','issuer',1,order.id)
    record=tmp_path/'record';record.write_text('synthetic source')
    evidence=[Input(post.typed([first]),AT+timedelta(seconds=11))]
    reads=[];graphs=[];original=attribution_cache.Threads
    def source(self,path):reads.append(path);return list(evidence)
    def graph(store):graphs.append(True);return original(store)
    monkeypatch.setattr(FakeHarness,'attribution_inputs',source)
    monkeypatch.setattr(attribution_cache,'Threads',graph)
    with Ledger(tmp_path/'ledger.sqlite') as book:
        book.ingest('worker','pool',record,[turn()],store)
        replay=lambda:book.attributions.replay(store,tmp_path,{str(record):{'fake'}})
        assert replay()==1
        measured=measurement_snapshot(book)
        record.write_text('synthetic source with output only')
        assert replay()==0 and reads==[record,record]
        assert graphs==[True], 'identical freshly validated input evidence must not rebuild every verdict'
        assert book.attributions.details()[0]['orders']==[first.id]
        evidence[:]=[Input(post.typed([second]),AT+timedelta(seconds=12))]
        record.write_text('synthetic source with different input evidence')
        assert replay()==1 and len(graphs)==2
        assert book.attributions.details()[0]['orders']==[second.id]
        assert measurement_snapshot(book)==measured


def test_racy_equal_stamps_need_a_new_read_before_certification_and_clock_reversal(tmp_path,monkeypatch):
    record=ClaudeShape(tmp_path/'home',tmp_path,'synthetic','synthetic')
    record.begin('First synthetic input','synthetic','low')
    stamp=record.path.stat();clock=[stamp.st_ctime_ns+100]
    initial=record.path.read_text()
    stat=Path.stat;read=Path.open;reads=[]
    def coarse(path,*args,**kwargs):return stamp if path==record.path else stat(path,*args,**kwargs)
    def observed(path,mode='r',*args,**kwargs):
        if path==record.path and mode in ('r','rb'):reads.append(True)
        return read(path,mode,*args,**kwargs)
    monkeypatch.setattr(Path,'stat',coarse)
    monkeypatch.setattr(Path,'open',observed)
    monkeypatch.setattr(json_record.time,'time_ns',lambda:clock[0])
    assert claude.state(record.path).last_input=='First synthetic input'
    record.path.write_text(initial.replace('First synthetic input','Other synthetic input'))
    clock[0]=stamp.st_ctime_ns+3_000_000_000
    assert claude.state(record.path).last_input=='Other synthetic input'
    count=len(reads)
    assert claude.state(record.path).last_input=='Other synthetic input' and len(reads)==count
    clock[0]=stamp.st_ctime_ns
    assert claude.state(record.path).last_input=='Other synthetic input' and len(reads)>count


def test_old_unterminated_object_is_reparsed_when_new_bytes_make_it_invalid(tmp_path):
    path=tmp_path/'record'
    path.write_text('{"type":"session_meta","payload":{}}')
    assert codex.state(path).pending_tools==()
    with path.open('a') as out:out.write('unparsed\n')
    with pytest.raises(json_record.RecordError):codex.state(path)


def test_new_inputs_and_queue_receipts_refresh_independently_of_growing_output(tmp_path):
    record=ClaudeShape(tmp_path/'home',tmp_path,'synthetic','synthetic')
    record.begin('Synthetic first input','synthetic','low')
    old=claude.inputs(record.path)
    record.queued('Synthetic second input')
    assert claude.inputs(record.path)[-1].kind=='queued'
    assert claude.state(record.path).queued==('Synthetic second input',)
    record.dequeued('Synthetic second input')
    assert claude.inputs(record.path)[-1].kind=='consumed'
    assert claude.state(record.path).queued==()
    old[0]=None
    assert claude.inputs(record.path)[0].text=='Synthetic first input'


@pytest.mark.parametrize('continuous', [False, True])
def test_a_racing_record_read_requires_a_stable_fresh_snapshot(tmp_path, monkeypatch, continuous):
    record = ClaudeShape(tmp_path/'home', tmp_path, 'synthetic', 'synthetic')
    record.begin('First synthetic input', 'synthetic', 'low')
    assert claude.state(record.path).last_input == 'First synthetic input'
    read = Path.read_bytes
    reads = []

    def concurrent(path):
        raw = read(path)
        if path == record.path:
            reads.append(True)
            if continuous:
                with path.open('ab') as stream:
                    stream.write(b'\n')
            elif len(reads) == 1:
                record.begin('Second synthetic input', 'synthetic', 'low')
        return raw

    monkeypatch.setattr(Path, 'read_bytes', concurrent)
    if continuous:
        with pytest.raises(json_record.RecordError, match='changed while being read'):
            claude.state(record.path)
        assert len(reads) <= 3, 'continuous mutation must refuse within the bounded snapshot read'
        monkeypatch.setattr(Path, 'read_bytes', read)
        record.begin('Second synthetic input', 'synthetic', 'low')
    assert claude.state(record.path).last_input == 'Second synthetic input'
