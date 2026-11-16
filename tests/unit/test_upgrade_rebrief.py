"""Upgrade carries old brief baselines across a new service's initial card stamp. A changed brief wakes no seat:
the sync posts nothing, and each change rides at the head of its seat's next message (brief_change)."""

from io import BytesIO
from itertools import count
from dataclasses import replace
import json
from pathlib import Path
import shutil
import tarfile
from types import SimpleNamespace

import pytest

from quarterdeck import brief_change, durable, install, post, service, standing
from quarterdeck.ship import ShipError
from quarterdeck.service import Service
from quarterdeck.ship import load
from quarterdeck.input_receipt import BRIEF_HEAD  # noqa: F401
from quarterdeck import input_receipt
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
        folder=ship.runtime/'trusted'/name;folder.mkdir(parents=True)
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
        # The synthetic service has already published or declined its response.
        # Scheduling must not expire this short fixture deadline before its first read.
        elapsed=[0.0]
        def sleep(seconds):elapsed[0]+=seconds
        with monkeypatch.context() as clock:
            clock.setattr(install,'time',SimpleNamespace(monotonic=lambda:elapsed[0],sleep=sleep))
            return original_result(path,data,timeout=0.01)
    monkeypatch.setattr(install,'rebrief_result',result)
    store=post.Store(ship.runtime)
    order=store.new('order','operator','captain','still open')
    state=SimpleNamespace(ship=ship,tmux=tmux,framework=framework,venv=venv,archive=archive,started=started,
                          active=active,start=start,stop=stop,stops=stops,panes=panes,store=store,order=order)
    yield state
    for svc in started:svc.ledger.close()


def notices(state):return [m for m in state.store.all() if m.sender==post.SHIP]


def carried(state, seat):
    """The change the seat's next message would carry at its head, or None."""
    found = brief_change.head(state.ship.runtime, seat)
    return found[0] if found else None


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
    launch_files=list((s.ship.runtime/'trusted').glob('*/launch.json'))
    identities={p:p.read_bytes() for p in launch_files}
    proofs=install.upgrade(s.ship,'changed',s.tmux)
    assert 'Changed framework instruction.' in (s.ship.runtime/'brief/captain.md').read_text()
    assert notices(s)==[], 'a changed brief wakes no seat'
    assert '+Changed framework instruction.' in carried(s,'captain')
    assert carried(s,'desk') is None
    assert any('changed briefs: captain' in line and 'no notice posted' in line for line in proofs)
    assert {p:p.read_bytes() for p in launch_files}==identities
    assert s.store.open_for('captain')==[s.order]
    svc=s.started[-1];svc.requests();svc.rebrief()
    assert notices(s)==[]


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
    assert carried(s,'captain') is None


def test_a_sync_posts_nothing_even_where_a_post_would_fail(upgraded,monkeypatch):
    """Replaces the partial-notice and notice-conflict cases: no notice is written, so none can fail or conflict."""
    s=upgraded;change_archive(s)
    monkeypatch.setattr(post.Store,'write',lambda self,message:(_ for _ in ()).throw(AssertionError('nothing is posted')))
    proofs=install.upgrade(s.ship,'changed',s.tmux)
    assert any('rebrief complete' in line for line in proofs)
    assert not (s.ship.runtime/'requests'/f'upgrade-rebrief-{NEW}.json').exists()
    for seat in ('captain','desk','worker-1'):
        assert '+Changed framework instruction.' in carried(s,seat), seat


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
    assert any('changed briefs' in line for line in proofs)
    s.start(s.ship)
    assert notices(s)==[]
    assert '+Changed framework instruction.' in carried(s,'captain') and '+Changed framework instruction.' in carried(s,'desk')


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


def test_replaced_launch_gets_no_notice_and_down_seat_brief_is_current(upgraded,monkeypatch):
    s=upgraded;change_archive(s)
    start=s.start
    def replaced(ship,qd=None,**kwargs):
        path=ship.runtime/'trusted/desk/launch.json'
        data=json.loads(path.read_text());data['resume']='different-conversation'
        path.write_text(json.dumps(data))
        return start(ship,qd)
    monkeypatch.setattr(install.servicectl,'start',replaced)
    install.upgrade(s.ship,'changed',s.tmux)
    assert notices(s)==[]
    assert '+Changed framework instruction.' in carried(s,'captain')
    assert 'Changed framework instruction.' in (s.ship.runtime/'brief/worker-1.md').read_text()
    assert 'Changed framework instruction.' in standing.brief(s.ship,s.ship.seat('worker-1')).text


def test_older_service_unknown_kind_is_not_completion_and_force_can_recover(upgraded,monkeypatch):
    s=upgraded;change_archive(s)
    original=Service.answer
    monkeypatch.setattr(Service,'answer',lambda self,data:"unknown request 'rebrief'" if data['kind']=='rebrief' else original(self,data))
    proofs=install.upgrade(s.ship,'changed',s.tmux)
    assert any('rebrief incomplete' in line for line in proofs) and notices(s)==[]
    monkeypatch.setattr(Service,'answer',original)
    proofs=install.upgrade(s.ship,'changed',s.tmux,force=True)
    assert notices(s)==[] and any('changed briefs' in line for line in proofs)
    assert '+Changed framework instruction.' in carried(s,'captain') and '+Changed framework instruction.' in carried(s,'desk')


def test_return_to_old_commit_is_new_event_but_only_content_changes_count(upgraded,monkeypatch):
    s=upgraded;change_archive(s)
    git=install.git
    assert any('changed briefs' in line for line in install.upgrade(s.ship,'first',s.tmux))
    # Different framework revision with identical instructions: no brief changed.
    monkeypatch.setattr(install,'git',lambda repo,*args:(OLD+'\n').encode() if args[0]=='rev-parse' else git(repo,*args))
    assert any('no brief changed' in line for line in install.upgrade(s.ship,'return',s.tmux))
    # Returning to NEW after OLD must replace its old done state and compare actual content.
    change_archive(s)
    monkeypatch.setattr(install,'git',git)
    assert any('changed briefs' in line for line in install.upgrade(s.ship,'again',s.tmux))
    assert notices(s)==[]
    assert carried(s,'captain').count('+Changed framework instruction.')==2, 'both changes fold into one head'


def test_an_interrupted_request_answer_completes_once_and_posts_nothing(upgraded,monkeypatch):
    s=upgraded;change_archive(s)
    original=durable.replace_file;failed=[]
    def write(path,*args,**kwargs):
        if path.suffix=='.done' and path.name.startswith('upgrade-rebrief-') and not failed:
            failed.append(True);raise OSError('synthetic answer interruption')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(durable,'replace_file',write)
    proofs=install.upgrade(s.ship,'changed',s.tmux)
    assert any('rebrief confirmation pending' in line for line in proofs)
    assert any('nothing to do' in line for line in install.upgrade(s.ship,'changed',s.tmux))
    poll_rebrief(s)
    assert not (s.ship.runtime/'requests'/f'upgrade-rebrief-{NEW}.json').exists()
    assert notices(s)==[] and carried(s,'captain').count(input_receipt.BRIEF_HEAD)==1


def test_missing_running_baseline_holds_without_rendering_a_new_baseline(upgraded):
    s=upgraded
    (s.ship.runtime/'brief/captain.md').unlink()
    path,data=install.queue_rebrief(s.ship,OLD,None,install.brief_snapshot(s.ship))
    svc=Service(s.ship,s.tmux);s.started.append(svc);svc.requests()
    assert path.exists() and not (s.ship.runtime/'brief/captain.md').exists()
    assert notices(s)==[]


def test_an_edited_card_is_carried_only_for_the_seats_whose_brief_changed(upgraded):
    s=upgraded
    svc=Service(s.ship,s.tmux);s.started.append(svc)
    (s.framework/'roles/captain.md').write_text('Changed role instruction.\n')
    svc.rebrief();svc.rebrief()
    card=s.framework/'roles/captain.md';card.write_text(card.read_text())
    svc.rebrief()
    assert notices(s)==[]
    assert '+Changed role instruction.' in carried(s,'captain')
    assert carried(s,'desk') is None and carried(s,'worker-1') is None


@pytest.mark.parametrize('preempted',[False,True])
def test_brief_updated_by_old_service_during_install_is_the_upgrade_baseline(upgraded,monkeypatch,preempted):
    s=upgraded;change_archive(s)
    if preempted:
        ticks=count(0.0,0.02)
        monkeypatch.setattr(install,'time',SimpleNamespace(monotonic=lambda:next(ticks),sleep=lambda seconds:None))
    old=Service(s.ship,s.tmux);s.started.append(old)
    stop=s.stop
    def stop_after_card_event(runtime):
        # A genuine card edit just before stop must become the comparison baseline.
        card=s.framework/'laws.md';card.write_text(card.read_text()+'\nChanged framework instruction.\n')
        old.rebrief()
        assert notices(s)==[]
        return stop(runtime)
    monkeypatch.setattr(install.servicectl,'stop',stop_after_card_event)
    proofs=install.upgrade(s.ship,'changed',s.tmux)
    assert any('no brief changed' in line for line in proofs)
    assert notices(s)==[]
    assert carried(s,'captain').count('+Changed framework instruction.')==1, 'the same content is carried once'


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


def test_a_card_edit_after_the_sync_folds_into_the_one_head(upgraded):
    """Replaces the cases of edits during a pending notice request: there is no notice to hold a request, and every
    change since the brief last told folds into the seat's one head."""
    s=upgraded;change_archive(s)
    install.upgrade(s.ship,'changed',s.tmux)
    svc=s.started[-1]
    card=standing.FRAMEWORK_DIR/'roles/captain.md'
    card.write_text(card.read_text()+'\nLater independent instruction.\n')
    svc.requests();svc.rebrief()
    head=carried(s,'captain')
    assert head.count(input_receipt.BRIEF_HEAD)==1
    assert '+Changed framework instruction.' in head and '+Later independent instruction.' in head
    assert '+Later independent instruction.' not in carried(s,'desk')
    restarted=Service(s.ship,s.tmux);s.started.append(restarted)
    restarted.requests();restarted.rebrief()
    assert notices(s)==[] and carried(s,'captain')==head


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
    svc=s.started[-1];svc.requests();svc.rebrief()
    assert 'Edit after captain rendering.' in (s.ship.runtime/'brief/captain.md').read_text()
    assert notices(s)==[] and '+Edit after captain rendering.' in carried(s,'captain')
