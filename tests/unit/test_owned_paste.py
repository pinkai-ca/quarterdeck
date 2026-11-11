"""Synthetic editor/record integration: no native client or historical delivery data."""
from datetime import datetime,timezone
import json
from types import SimpleNamespace

import pytest

from quarterdeck import pending,post
from quarterdeck.door import Door
from quarterdeck.harness.codex import CodexHarness
from quarterdeck.service import Service
from quarterdeck.ship import Pool,SeatSpec,Ship,Session
from quarterdeck.tmux import TmuxError


def frame(text,width=422):
    rows=[]
    for line in text.split('\n'):
        rows.extend([line[i:i+width] for i in range(0,len(line),width)] or [''])
        if line and len(line)%width==0:rows.append('')
    return '› '+rows[0]+'\n'+''.join('  '+r+'\n' for r in rows[1:])+"\n  synthetic-model low · /synthetic · main\n"


@pytest.fixture
def editor(tmp_path):
    workspace=tmp_path/'workspace';workspace.mkdir()
    spec=SeatSpec('worker','crew','worker','codex',('synthetic',),cwd=workspace)
    ship=Ship(tmp_path/'ship','synthetic','operator',workspace,{'synthetic':Pool('synthetic','openai')},
              {'crew':Session('crew','Synthetic')},{'worker':spec})
    folder=ship.runtime/'seats/worker';folder.mkdir(parents=True)
    record=folder/'synthetic.jsonl'
    record.write_text(json.dumps(dict(type='session_meta',payload={'id':'synthetic-record'}))+'\n')
    pane=SimpleNamespace(id='%1',pid=123,width=425,height=50,dead=False,in_mode=False)
    launch=SimpleNamespace(at=post.now().isoformat(),pool='synthetic',version='0.166.0',pane=pane.id,pid=pane.pid)
    class Terminal:
        def __init__(self):
            self.text='';self.pastes=[];self.clears=[];self.submitted=[];self.inputs=();self.clear_fails=False;self.prefix=''
        def capture(self,*a,**kw):return self.prefix+frame(self.text or 'Ask Codex to do anything')
        def paste(self,pane_id,text,**kw):self.pastes.append(text);self.text=text
        def session_view(self,*a):return SimpleNamespace(scrolling=False,inputs=self.inputs)
        def editor_position(self,*a):
            row=sum(max(1,(len(line)+421)//422)+(len(line)==422) for line in self.text.split('\n'))-1
            return len(self.text.split('\n')[-1])+2,row,pane.width,pane.height
        def clear_owned_paste(self,pane_id,pid,position,keys):
            if pid!=pane.pid or position!=self.editor_position():raise TmuxError('changed clear identity')
            self.clears.append(keys)
            if not self.clear_fails:self.text=''
        def keys(self,pane_id,*keys):
            self.submitted.append(keys)
            if keys==('Enter',):
                at=post.now().isoformat()
                with record.open('a') as stream:
                    stream.write(json.dumps(dict(timestamp=at,type='event_msg',payload={'type':'task_started'}))+'\n')
                    stream.write(json.dumps(dict(timestamp=at,type='response_item',payload={
                        'type':'message','role':'user','content':[{'type':'input_text','text':self.text}]}))+'\n')
                self.text=''
        def pane(self,*a):return pane
    terminal=Terminal()
    seat=SimpleNamespace(name='worker',harness=CodexHarness(),tmux=terminal,spec=spec,ship=ship,
                         session=SimpleNamespace(human=False,title='Synthetic'),pool=ship.pool('synthetic'),folder=folder,
                         pane=lambda:pane,launched=lambda:launch,record=lambda:record)
    def proof(text):
        message=post.Store(ship.runtime).new('order','issuer','worker',text)
        attempt=post.Attempts(ship.runtime).open(message,'worker',pane.id,pane.pid,launch.at)
        return pending.Proof.begin(seat,attempt,text)
    return SimpleNamespace(seat=seat,terminal=terminal,pane=pane,launch=launch,record=record,proof=proof)


@pytest.mark.parametrize('width',[421,422,423])
def test_exact_width_input_is_submitted_once_and_confirmed_by_native_shape_record(editor,width):
    text='header\n'+'x'*width+'\nreply';proof=editor.proof(text)
    result=Door(editor.seat).deliver(text,proof=proof)
    assert result.result=='delivered' and editor.terminal.submitted==[('Enter',)]  # law: one-door
    assert editor.terminal.pastes==[text] and not editor.terminal.clears


def delayed(door,editor):
    # The verification deadline saw only a drawing prefix. A fresh full view
    # must independently establish ownership before the clear is authorized.
    door.box_shows=lambda *a:editor.seat.harness.screen(frame('drawing prefix'))


def test_owned_failed_paste_clears_and_another_message_progresses(editor):
    text='header\nbody\nreply';proof=editor.proof(text);door=Door(editor.seat);delayed(door,editor)
    result=door.deliver(text,proof=proof)
    assert result.result=='withheld' and 'empty box confirmed' in result.detail
    assert editor.terminal.clears and editor.terminal.text=='' and not editor.terminal.submitted
    assert proof.data['phase']=='withheld' and not pending.active(editor.seat)
    later='another deliverable message';next_proof=editor.proof(later)
    assert Door(editor.seat).deliver(later,proof=next_proof).result=='delivered'


@pytest.mark.parametrize('change',['foreign','edited','identity','human','submitted','client-input','cursor','clear-not-empty','unknown-owner','folded','record-changed'])
def test_foreign_changed_or_uncertain_input_is_preserved(editor,change):
    text='header\nbody\nreply';proof=editor.proof(text);door=Door(editor.seat)
    def failure(*a):
        if change in ('foreign','edited'):editor.terminal.text='someone else' if change=='foreign' else text+' edit'
        elif change=='identity':editor.pane.pid+=1
        elif change=='human':editor.seat.session.human=True
        elif change=='submitted':proof.save(phase='submitted')
        elif change=='client-input':editor.terminal.inputs=(int(post.now().timestamp()),)
        elif change=='cursor':editor.terminal.editor_position=lambda *a:(0,0,425,50)
        elif change=='unknown-owner':editor.terminal.session_view=lambda *a:None
        elif change=='folded':editor.terminal.text=f'[Pasted Content {len(text)} chars]'
        elif change=='record-changed':editor.record.write_text(editor.record.read_text()+'{}\n')
        else:editor.terminal.clear_fails=True
        return editor.seat.harness.screen(frame('drawing prefix'))
    door.box_shows=failure
    # Avoid spending the ordinary display deadline on a deliberately stuck clear.
    from quarterdeck import paste_refusal
    original=paste_refusal.clear
    if change=='clear-not-empty':
        door_clear=lambda *a:original(*a[:-1],0)
        from unittest.mock import patch
        with patch.object(paste_refusal,'clear',door_clear):result=door.deliver(text,proof=proof)
    else:result=door.deliver(text,proof=proof)
    assert result.result=='blind' and not editor.terminal.submitted
    assert proof.data['phase']!='withheld'
    if change!='clear-not-empty':assert not editor.terminal.clears
    else:assert proof.data['paste_clear']['stage']=='checking_empty' and pending.active(editor.seat)
    assert editor.terminal.text


@pytest.mark.parametrize('change',['payload','version','geometry','launch','frame','record'])
def test_unchanged_refused_resend_types_nothing_and_changed_conditions_are_reevaluated(editor,change):
    text='header\nbody\nreply';proof=editor.proof(text);door=Door(editor.seat);delayed(door,editor)
    assert door.deliver(text,proof=proof).result=='withheld'
    old=proof.path.read_bytes();count=len(editor.terminal.pastes)
    blocked=editor.proof(text);result=Door(editor.seat).deliver(text,proof=blocked)
    blocked.finish(result.result)
    assert result.result=='withheld' and 'unchanged' in result.detail
    assert len(editor.terminal.pastes)==count and proof.path.read_bytes()==old
    if change=='payload':text+=' changed'
    elif change=='version':editor.launch.version='0.167.1'
    elif change=='geometry':editor.pane.width+=1
    elif change=='launch':editor.launch.at=post.now().isoformat()
    elif change=='frame':editor.terminal.prefix='Different native header\n'
    else:editor.record.write_text(editor.record.read_text()+json.dumps(dict(type='turn_context',payload={'model':'synthetic'}))+'\n')
    assert Door(editor.seat).deliver(text,proof=editor.proof(text)).result=='delivered'


def test_explicit_service_resend_returns_refusal_without_retiring_old_evidence(editor):
    store=post.Store(editor.seat.ship.runtime);message=store.new('answer','issuer','worker','synthetic body')
    text=post.typed([message]);proof=editor.proof(text);door=Door(editor.seat);delayed(door,editor)
    assert door.deliver(text,proof=proof).result=='withheld'
    before=proof.path.read_bytes();service=Service(editor.seat.ship,None)
    try:
        detail=service.deliver(editor.seat,[message],resend=True)
        assert 'unchanged' in detail and len(editor.terminal.pastes)==1
        assert proof.path.read_bytes()==before
    finally:service.ledger.close()


def test_service_settles_withheld_evidence_and_delivers_the_next_message(editor,monkeypatch):
    service=Service(editor.seat.ship,None)
    first=service.store.new('answer','issuer','worker','first synthetic body')
    second=service.store.new('answer','issuer','worker','second synthetic body')
    original=Door.box_shows
    def shows(door,box,*a,**kw):
        return editor.seat.harness.screen(frame('drawing prefix')) if 'first synthetic body' in box.text else original(door,box,*a,**kw)
    monkeypatch.setattr(Door,'box_shows',shows)
    try:
        result=service.deliver(editor.seat,[first,second])
        assert 'withheld:' in result and 'delivered:' in result
        attempts=service.attempts.all()
        assert {a.message:a.result for a in attempts}=={first.id:'withheld',second.id:'delivered'}
    finally:service.ledger.close()


def test_a_synthetic_detached_journal_is_preserved(editor):
    text='historical unconfirmed input';proof=editor.proof(text);proof.save(phase='pasting')
    before=proof.path.read_bytes();editor.launch.at=post.now().isoformat()
    result=Door(editor.seat).deliver('another message',proof=editor.proof('another message'))
    assert result.result=='delivered'  # an empty new launch remains usable under the existing rule
    assert proof.path.read_bytes()==before
    assert pending.detached(editor.seat), 'old uncertainty stays visible, never settled by this change'


def test_clear_intent_is_durable_before_key_failure_and_blocks_another_paste(editor):
    text='header\nbody\nreply';proof=editor.proof(text);door=Door(editor.seat);delayed(door,editor)
    def failed_queue(*a):raise OSError('synthetic transport failure')
    editor.terminal.clear_owned_paste=failed_queue
    assert door.deliver(text,proof=proof).result=='blind'
    saved=json.loads(proof.path.read_text())
    assert saved['phase']=='pasting' and saved['paste_clear']['stage']=='clearing'
    from quarterdeck.service import person_only
    assert person_only(Door(editor.seat).reading().reason), 'uncertain clear uses the normal person-needed alarm path'
    before=proof.path.read_bytes()
    assert Door(editor.seat).deliver('another message',proof=editor.proof('another message')).result=='withheld'
    assert len(editor.terminal.pastes)==1 and not editor.terminal.clears
    assert proof.path.read_bytes()==before
