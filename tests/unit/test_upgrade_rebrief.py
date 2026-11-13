"""Upgrade carries old brief baselines across a new service's initial card stamp."""

from io import BytesIO
from dataclasses import replace
import json
from pathlib import Path
import shutil
import tarfile
from types import SimpleNamespace

import pytest

from quarterdeck import durable, install, post, service, standing
from quarterdeck.ship import ShipError
from quarterdeck.service import Service
from quarterdeck.ship import load
from quarterdeck.tmux import Pane

OLD='a'*40
NEW='b'*40


@pytest.fixture
def upgraded(drydock, tmp_path, monkeypatch):
    declaration=drydock/"ship.toml"
    declaration.write_text(declaration.read_text().replace('code = "checkout"\n',""))
    ship=load(drydock)
    framework=tmp_path/'framework';shutil.copytree(standing.FRAMEWORK_DIR,framework)
    monkeypatch.setattr(standing,'FRAMEWORK_DIR',framework)
    for spec in ship.seats.values():standing.render(ship,spec)
    panes={}
    for i,name in enumerate(('captain','desk'),1):
        folder=ship.runtime/'seats'/name;folder.mkdir(parents=True)
        launch=dict(pane=f'%{i}',pid=100+i,at='2025-01-01T00:00:00+00:00',pool='fake',
                    argv=['synthetic'],harness='fake',resume='same-conversation',version='synthetic')
        (folder/'launch.json').write_text(json.dumps(launch))
        panes[f'%{i}']=Pane(f'%{i}',ship.session_of(ship.seat(name)).title,name,100+i,80,24,False,False,None)
    tmux=SimpleNamespace(pane=panes.get,has_session=lambda title:False)
    venv=ship.runtime/'venv';(venv/'bin').mkdir(parents=True)
    (venv/'bin/python').touch();(venv/'bin/python').chmod(0o755)
    shutil.copytree(framework,venv/'share/quarterdeck/standing-orders')
    qd=venv/'bin/qd';qd.write_text('#!/bin/sh\nprintf "qd synthetic\\n"\n');qd.chmod(0o755)
    (ship.runtime/'installed.json').write_text(json.dumps(dict(commit=OLD,ref='old',at='2025-01-01T00:01:00+00:00',previous=None)))
    archive=BytesIO()
    with tarfile.open(fileobj=archive,mode='w') as bundle:
        bundle.add(framework,arcname='standing-orders')
        bundle.add(drydock,arcname='ships/drydock',filter=lambda info:None if '/runtime' in info.name else info)
        declaration={}
        for member in bundle.getmembers():
            if member.isfile():
                destination='share/quarterdeck/'+Path(member.name).parent.as_posix()
                declaration.setdefault(destination,[]).append(member.name)
        metadata=('[tool.setuptools.data-files]\n'+''.join(
            f'{json.dumps(destination)} = {json.dumps(sorted(names))}\n'
            for destination,names in sorted(declaration.items()))).encode()
        info=tarfile.TarInfo('pyproject.toml');info.size=len(metadata)
        bundle.addfile(info,BytesIO(metadata))
    def git(repo,*args):
        if args[0]=='status':return b''
        return (NEW+'\n').encode() if args[0]=='rev-parse' else archive.getvalue()
    monkeypatch.setattr(install,'source_repo',lambda ship:tmp_path)
    monkeypatch.setattr(install,'git',git)
    def run(argv,doing,**kwargs):
        if argv[0]=='cp':
            shutil.copytree(argv[-2],argv[-1])
        if doing=='checking live package, prefix and resources':
            return SimpleNamespace(stdout=json.dumps(dict(package=str(venv/'synthetic-package'),package_digest='1'*64)),returncode=0)
        return SimpleNamespace(stdout='qd synthetic\n',returncode=0)
    monkeypatch.setattr(install,'_run',run)
    monkeypatch.setattr(install,'running_from',lambda path:True)
    monkeypatch.setattr(service.machine,'processes',lambda:{})
    active=[True];started=[];stops=[]
    monkeypatch.setattr(install.servicectl,'alive',lambda runtime:active[0])
    def stop(runtime):active[0]=False;stops.append(True);return 'service stopped · synthetic'
    def start(ship,qd=None,**kwargs):
        active[0]=True
        monkeypatch.setattr(standing,'FRAMEWORK_DIR',venv/'share/quarterdeck/standing-orders')
        svc=Service(ship,tmux);started.append(svc)
        return 'service started · synthetic'
    monkeypatch.setattr(install.servicectl,'stop',stop)
    monkeypatch.setattr(install.servicectl,'start',start)
    original_result=install.rebrief_result
    def result(path,data,**kwargs):
        # Service publication happens after activation, not inside its startup handshake.
        try:
            started[-1].requests();started[-1].rebrief()
        except (OSError,ShipError):
            raise OSError('synthetic rebrief response pending') from None
        return original_result(path,data,timeout=0.01)
    monkeypatch.setattr(install,'rebrief_result',result)
    store=post.Store(ship.runtime)
    order=store.new('order','operator','captain','still open')
    state=SimpleNamespace(ship=ship,tmux=tmux,framework=framework,venv=venv,archive=archive,started=started,
                          active=active,start=start,stop=stop,stops=stops,panes=panes,store=store,order=order)
    yield state
    for svc in started:svc.ledger.close()


def notices(state):return [m for m in state.store.all() if m.sender==post.SHIP]


def pending_upgrade(state):
    proofs=install.upgrade(state.ship,'changed',state.tmux)
    assert any('rebrief confirmation pending' in line for line in proofs)
    data=json.loads((state.ship.runtime/'installed.json').read_text())
    assert data['current']['commit']==NEW
    assert data['last_failure']['reason']=='rebrief confirmation pending'
    return proofs


def poll_rebrief(state):
    state.started[-1].requests();state.started[-1].rebrief()


def test_upgrade_to_changed_card_rebriefs_affected_live_launch_without_restarting_seats(upgraded):
    s=upgraded
    # Rebuild the synthetic installed archive with a changed card; the running service's copy stays old.
    s.archive.seek(0)
    with tarfile.open(fileobj=s.archive,mode='r') as bundle:
        files={member.name:bundle.extractfile(member).read() for member in bundle.getmembers() if member.isfile()}
    files['standing-orders/roles/captain.md']+=b'\nChanged framework instruction.\n'
    changed=BytesIO()
    with tarfile.open(fileobj=changed,mode='w') as bundle:
        for name,body in files.items():
            info=tarfile.TarInfo(name);info.size=len(body);bundle.addfile(info,BytesIO(body))
    s.archive.seek(0);s.archive.truncate();s.archive.write(changed.getvalue())
    launch_files=list((s.ship.runtime/'seats').glob('*/launch.json'))
    identities={p:p.read_bytes() for p in launch_files}
    proofs=install.upgrade(s.ship,'changed',s.tmux)
    assert 'Changed framework instruction.' in (s.ship.runtime/'brief/captain.md').read_text()
    messages=notices(s)
    assert len(messages)==1 and messages[0].to=='captain'
    assert 'runtime/brief/captain.md' in messages[0].body and 'read' in messages[0].body
    assert not any(word in messages[0].body for word in ('has read','delivered'))
    assert any('captain' in line and 'posted' in line for line in proofs)
    assert {p:p.read_bytes() for p in launch_files}==identities
    assert s.store.open_for('captain')==[s.order]
    svc=s.started[-1];svc.requests();svc.rebrief()
    assert notices(s)==messages


def change_archive(s, relative='laws.md'):
    s.archive.seek(0)
    with tarfile.open(fileobj=s.archive,mode='r') as bundle:
        files={member.name:bundle.extractfile(member).read() for member in bundle.getmembers() if member.isfile()}
    files['standing-orders/'+relative]+=b'\nChanged framework instruction.\n'
    changed=BytesIO()
    with tarfile.open(fileobj=changed,mode='w') as bundle:
        for name,body in files.items():
            info=tarfile.TarInfo(name);info.size=len(body);bundle.addfile(info,BytesIO(body))
    s.archive.seek(0);s.archive.truncate();s.archive.write(changed.getvalue())


def test_code_only_and_same_commit_upgrades_are_quiet_and_ordinary_restart_is_quiet(upgraded):
    s=upgraded
    for attempt in range(2):
        proofs=install.upgrade(s.ship,'same',s.tmux)
        expected='no brief changed' if attempt==0 else f'already runs {NEW[:7]}; nothing to do'
        assert any(expected in line for line in proofs)
        assert notices(s)==[]
    svc=Service(s.ship,s.tmux);s.started.append(svc)
    svc.requests();svc.rebrief();svc.rebrief()
    assert notices(s)==[]


def test_partial_notice_failure_keeps_original_baseline_and_deduplicates_after_same_commit_retry(upgraded,monkeypatch):
    s=upgraded;change_archive(s)
    original=post.Store.write;failed=[]
    def write(self,message):
        if message.sender==post.SHIP and message.to=='desk' and not failed:
            failed.append(True);raise OSError('synthetic interrupted post')
        return original(self,message)
    monkeypatch.setattr(post.Store,'write',write)
    pending_upgrade(s)
    pending=s.ship.runtime/'requests'/f'upgrade-rebrief-{NEW}.json'
    payload=json.loads(pending.read_text())
    assert [m.to for m in notices(s)]==['captain']
    assert 'Changed framework instruction.' in (s.ship.runtime/'brief/desk.md').read_text()
    assert payload['before']['desk']!=install.brief_snapshot(s.ship)['before']['desk']
    assert any('nothing to do' in line for line in install.upgrade(s.ship,'changed',s.tmux))
    poll_rebrief(s)
    assert sorted(m.to for m in notices(s))==['captain','desk']
    ids={m.id for m in notices(s)}
    assert len(ids)==2 and not pending.exists()
    svc=Service(s.ship,s.tmux);s.started.append(svc);svc.requests();svc.rebrief()
    assert {m.id for m in notices(s)}==ids


@pytest.mark.parametrize('stage',['install','queue'])
def test_interruption_after_service_stop_and_install_before_queue_recovers(upgraded,monkeypatch,stage):
    s=upgraded;change_archive(s)
    original=install.queue_rebrief;failed=[]
    def queue(*args):
        if not failed:failed.append(True);raise OSError('synthetic before request')
        return original(*args)
    monkeypatch.setattr(install,'queue_rebrief',queue)
    if stage=='install':
        fixture_run=install._run
        def run(argv,doing,**kwargs):
            if doing.startswith('installing') and not failed:
                failed.append(True);raise OSError('synthetic before request')
            return fixture_run(argv,doing,**kwargs)
        monkeypatch.setattr(install,'_run',run)
    with pytest.raises(ShipError,match='package build failed' if stage=='install' else 'copy activation failed'):
        install.upgrade(s.ship,'changed',s.tmux)
    assert s.stops and install.installed(s.ship.runtime)['commit']==OLD
    assert notices(s)==[]
    # Recovery preserves ordinary upgrade behavior: it does not start a previously down service.
    proofs=install.upgrade(s.ship,'changed',s.tmux)
    assert any('posted' in line for line in proofs)
    s.start(s.ship)
    assert sorted(m.to for m in notices(s))==['captain','desk']


def test_superseded_request_never_renders_and_wrong_running_revision_holds(upgraded,monkeypatch):
    s=upgraded
    snapshot=install.brief_snapshot(s.ship)
    path,data=install.queue_rebrief(s.ship,NEW,None,snapshot)
    svc=Service(s.ship,s.tmux);s.started.append(svc)  # running OLD
    (s.ship.runtime/'installed.json').write_text(json.dumps(dict(commit=NEW,ref='synthetic-new',at='2025-01-01T00:02:00+00:00')))
    monkeypatch.setattr(standing,'render',lambda *a:(_ for _ in ()).throw(AssertionError('must not render')))
    svc.requests()
    assert path.exists() and notices(s)==[]
    data['commit']=OLD;path.write_text(json.dumps(data))
    svc.requests()
    assert not path.exists() and json.loads(path.with_suffix('.done').read_text())['status']=='superseded'
    assert notices(s)==[]


def test_replaced_launch_gets_no_old_notice_and_down_seat_brief_is_current(upgraded,monkeypatch):
    s=upgraded;change_archive(s)
    start=s.start
    def replaced(ship,qd=None,**kwargs):
        path=ship.runtime/'seats/desk/launch.json'
        data=json.loads(path.read_text());data['resume']='different-conversation'
        path.write_text(json.dumps(data))
        return start(ship,qd)
    monkeypatch.setattr(install.servicectl,'start',replaced)
    install.upgrade(s.ship,'changed',s.tmux)
    assert [m.to for m in notices(s)]==['captain']
    assert 'Changed framework instruction.' in (s.ship.runtime/'brief/worker-1.md').read_text()
    assert 'Changed framework instruction.' in standing.brief(s.ship,s.ship.seat('worker-1')).text


def test_older_service_unknown_kind_is_not_completion_and_force_can_recover(upgraded,monkeypatch):
    s=upgraded;change_archive(s)
    original=Service.answer
    monkeypatch.setattr(Service,'answer',lambda self,data:"unknown request 'rebrief'" if data['kind']=='rebrief' else original(self,data))
    proofs=install.upgrade(s.ship,'changed',s.tmux)
    assert any('rebrief incomplete' in line for line in proofs) and notices(s)==[]
    monkeypatch.setattr(Service,'answer',original)
    install.upgrade(s.ship,'changed',s.tmux,force=True)
    assert sorted(m.to for m in notices(s))==['captain','desk']


def test_return_to_old_commit_is_new_event_but_only_content_changes_notify(upgraded,monkeypatch):
    s=upgraded;change_archive(s)
    git=install.git
    install.upgrade(s.ship,'first',s.tmux)
    assert len(notices(s))==2
    # Different framework revision with identical instructions: no notice.
    monkeypatch.setattr(install,'git',lambda repo,*args:(OLD+'\n').encode() if args[0]=='rev-parse' else git(repo,*args))
    assert any('no brief changed' in line for line in install.upgrade(s.ship,'return',s.tmux))
    assert len(notices(s))==2
    # Returning to NEW after OLD must replace its old done state and compare actual content.
    change_archive(s)
    monkeypatch.setattr(install,'git',git)
    install.upgrade(s.ship,'again',s.tmux)
    assert len(notices(s))==4 and len({m.id for m in notices(s)})==4


def test_posted_notices_do_not_duplicate_when_request_answer_write_is_interrupted(upgraded,monkeypatch):
    s=upgraded;change_archive(s)
    original=durable.replace_file;failed=[]
    def write(path,*args,**kwargs):
        if path.suffix=='.done' and path.name.startswith('upgrade-rebrief-') and not failed:
            failed.append(True);raise OSError('synthetic answer interruption')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(durable,'replace_file',write)
    pending_upgrade(s)
    ids={m.id for m in notices(s)};assert len(ids)==2
    assert any('nothing to do' in line for line in install.upgrade(s.ship,'changed',s.tmux))
    poll_rebrief(s)
    assert {m.id for m in notices(s)}==ids


def test_immutable_notice_conflict_retains_the_exact_rebrief_intent_and_answers_later_requests(upgraded,monkeypatch):
    s=upgraded;change_archive(s)
    original=post.Store.write;conflicts=[]
    def write(store,message):
        if message.sender==post.SHIP and not conflicts:
            conflict=replace(message,body='Synthetic immutable conflict')
            conflicts.append(original(store,conflict))
            raise ShipError('synthetic publication conflict')
        return original(store,message)
    monkeypatch.setattr(post.Store,'write',write)
    pending_upgrade(s)
    request=s.ship.runtime/'requests'/f'upgrade-rebrief-{NEW}.json'
    before=request.read_bytes();notice_before=conflicts[0].read_bytes()
    later=request.parent/'zz-synthetic-healthy.json'
    later.write_text(json.dumps(dict(kind='synthetic-healthy')))
    with pytest.raises(ShipError,match='identity conflicts'):
        s.started[-1].requests()
    assert request.read_bytes()==before and conflicts[0].read_bytes()==notice_before
    assert not request.with_suffix('.done').exists() and not (request.parent/'failed').exists()
    assert later.with_suffix('.done').read_text()=="unknown request 'synthetic-healthy'\n"


def test_missing_running_baseline_holds_without_rendering_a_new_baseline(upgraded):
    s=upgraded
    (s.ship.runtime/'brief/captain.md').unlink()
    path,data=install.queue_rebrief(s.ship,OLD,None,install.brief_snapshot(s.ship))
    svc=Service(s.ship,s.tmux);s.started.append(svc);svc.requests()
    assert path.exists() and not (s.ship.runtime/'brief/captain.md').exists()
    assert notices(s)==[]


def test_existing_edited_card_path_only_notifies_changed_running_briefs(upgraded):
    s=upgraded
    svc=Service(s.ship,s.tmux);s.started.append(svc)
    (s.framework/'roles/captain.md').write_text('Changed role instruction.\n')
    svc.rebrief();svc.rebrief()
    card=s.framework/'roles/captain.md';card.write_text(card.read_text())
    svc.rebrief()
    assert [m.to for m in notices(s)]==['captain']
    assert notices(s)[0].body=="ship's orders changed · read runtime/brief/captain.md"


def test_brief_updated_by_old_service_during_install_is_the_upgrade_baseline(upgraded,monkeypatch):
    s=upgraded;change_archive(s)
    old=Service(s.ship,s.tmux);s.started.append(old)
    stop=s.stop
    def stop_after_card_event(runtime):
        # A genuine card edit just before stop must become the comparison baseline.
        card=s.framework/'laws.md';card.write_text(card.read_text()+'\nChanged framework instruction.\n')
        old.rebrief()
        assert len(notices(s))==2
        return stop(runtime)
    monkeypatch.setattr(install.servicectl,'stop',stop_after_card_event)
    proofs=install.upgrade(s.ship,'changed',s.tmux)
    assert any('no brief changed' in line for line in proofs)
    assert len(notices(s))==2, 'preserve existing notices rather than post the same content again'


def test_old_service_is_stopped_before_replacing_live_code_or_cards(upgraded,monkeypatch):
    s=upgraded
    fixture_run=install._run
    def run(argv,doing,**kwargs):
        if doing.startswith('installing'):
            assert not s.active[0], 'the old mtime watcher must not observe replacement cards'
        return fixture_run(argv,doing,**kwargs)
    monkeypatch.setattr(install,'_run',run)
    assert any('no brief changed' in line for line in install.upgrade(s.ship,'same-content',s.tmux))
    assert notices(s)==[]


@pytest.mark.parametrize('return_to_baseline',[False,True])
@pytest.mark.parametrize('replace_desk',[False,True])
def test_card_edit_during_partial_upgrade_gets_one_new_notice(upgraded,monkeypatch,return_to_baseline,replace_desk):
    s=upgraded;change_archive(s)
    original_brief=(s.ship.runtime/'brief/captain.md').read_text()
    original=post.Store.write;failed=[]
    def write(self,message):
        if message.sender==post.SHIP and message.to=='desk' and not failed:
            failed.append(True);raise OSError('synthetic desk post interruption')
        return original(self,message)
    monkeypatch.setattr(post.Store,'write',write)
    pending_upgrade(s)
    svc=s.started[-1]
    first=notices(s)[0]
    first_brief=(s.ship.runtime/'brief/captain.md').read_text()
    assert first.to=='captain' and first_brief!=original_brief
    assert 'Changed framework instruction.' in first_brief  # first version/file and immutable notice were offered
    if replace_desk:
        launch=s.ship.runtime/'seats/desk/launch.json'
        data=json.loads(launch.read_text());data['resume']='replacement';launch.write_text(json.dumps(data))
    if return_to_baseline:
        laws=standing.FRAMEWORK_DIR/'laws.md'
        laws.write_text(laws.read_text().replace('\nChanged framework instruction.\n',''))
    else:
        card=standing.FRAMEWORK_DIR/'roles/captain.md'
        card.write_text(card.read_text()+'\nLater independent instruction.\n')
    svc.requests();svc.rebrief()
    captain=[m for m in notices(s) if m.to=='captain']
    assert len(captain)==2 and len({m.id for m in captain})==2
    current=(s.ship.runtime/'brief/captain.md').read_text()
    assert current==original_brief if return_to_baseline else 'Later independent instruction.' in current
    assert len([m for m in notices(s) if m.to=='desk'])==(0 if replace_desk or return_to_baseline else 1)
    assert not [m for m in notices(s) if m.to=='worker-1']
    ids={m.id for m in notices(s)}
    svc.requests();svc.rebrief()
    restarted=Service(s.ship,s.tmux);s.started.append(restarted)
    restarted.requests();restarted.rebrief()
    assert {m.id for m in notices(s)}==ids


def test_repeated_content_changes_during_one_pending_request_each_get_one_notice(upgraded,monkeypatch):
    s=upgraded;change_archive(s)
    original=post.Store.write;hold=[True]
    def write(self,message):
        if message.sender==post.SHIP and message.to=='desk' and hold[0]:
            raise OSError('synthetic desk held')
        return original(self,message)
    monkeypatch.setattr(post.Store,'write',write)
    pending_upgrade(s)
    svc=s.started[-1]
    card=standing.FRAMEWORK_DIR/'roles/captain.md';base=card.read_text()
    for index,text in enumerate((base+'\nLater edit.\n',base,base+'\nLater edit.\n'),2):
        card.write_text(text)
        with pytest.raises(OSError):svc.requests()
        svc.rebrief()
        captain=[m for m in notices(s) if m.to=='captain']
        assert len(captain)==index and len({m.id for m in captain})==index
    hold[0]=False;svc.requests();svc.rebrief()
    ids={m.id for m in notices(s)}
    assert len(ids)==5
    svc.requests();svc.rebrief()
    assert {m.id for m in notices(s)}==ids


def test_unposted_intermediate_content_is_not_a_new_offered_baseline(upgraded,monkeypatch):
    s=upgraded;change_archive(s)
    original=post.Store.write;captain_calls=[];desk_failed=[]
    def write(self,message):
        if message.sender==post.SHIP and message.to=='captain':
            captain_calls.append(True)
            if len(captain_calls)==2:raise OSError('synthetic later failure')
        if message.sender==post.SHIP and message.to=='desk' and not desk_failed:
            desk_failed.append(True);raise OSError('synthetic first failure')
        return original(self,message)
    monkeypatch.setattr(post.Store,'write',write)
    pending_upgrade(s)
    svc=s.started[-1]
    card=standing.FRAMEWORK_DIR/'roles/captain.md';base=card.read_text()
    card.write_text(base+'\nUnposted version.\n')
    with pytest.raises(OSError):svc.requests()
    assert len([m for m in notices(s) if m.to=='captain'])==1
    card.write_text(base+'\nFinal version.\n')
    svc.requests();svc.rebrief()
    assert len([m for m in notices(s) if m.to=='captain'])==2
    assert 'Final version.' in (s.ship.runtime/'brief/captain.md').read_text()
    assert len(notices(s))==3


def test_intent_write_failure_does_not_duplicate_a_later_edit_via_ordinary_rebrief(upgraded,monkeypatch):
    s=upgraded;change_archive(s)
    original=post.Store.write;failed=[]
    def write(self,message):
        if message.sender==post.SHIP and message.to=='desk' and not failed:
            failed.append(True);raise OSError('synthetic partial')
        return original(self,message)
    monkeypatch.setattr(post.Store,'write',write)
    pending_upgrade(s)
    svc=s.started[-1]
    card=standing.FRAMEWORK_DIR/'roles/captain.md';card.write_text(card.read_text()+'\nLater edit.\n')
    persist=install.write_rebrief;interrupted=[]
    def save(*args):
        if not interrupted:
            interrupted.append(True);raise OSError('synthetic intent failure')
        return persist(*args)
    monkeypatch.setattr(install,'write_rebrief',save)
    with pytest.raises(OSError):svc.requests()
    svc.rebrief()
    svc.requests();svc.rebrief()
    assert len([m for m in notices(s) if m.to=='captain'])==2
    assert len(notices(s))==3


def test_edit_arriving_during_recovery_remains_visible_to_ordinary_rebrief(upgraded,monkeypatch):
    s=upgraded;change_archive(s)
    render=standing.render;edited=[]
    def render_then_edit(ship,spec,**kwargs):
        path=render(ship,spec,**kwargs)
        if spec.name=='worker-1' and not edited:
            edited.append(True)
            card=standing.FRAMEWORK_DIR/'roles/captain.md'
            card.write_text(card.read_text()+'\nEdit after captain rendering.\n')
        return path
    monkeypatch.setattr(standing,'render',render_then_edit)
    install.upgrade(s.ship,'changed',s.tmux)
    svc=s.started[-1]
    assert len([m for m in notices(s) if m.to=='captain'])==2
    assert 'Edit after captain rendering.' in (s.ship.runtime/'brief/captain.md').read_text()
    ids={m.id for m in notices(s)};svc.requests();svc.rebrief()
    assert {m.id for m in notices(s)}==ids
