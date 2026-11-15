"""Eliminated query/import work and presentation scheduling, with no timing thresholds."""
from datetime import datetime,timezone
import json,os,subprocess,sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import presentation, service as servicemod, ship as shipmod, watch
from quarterdeck.door import Door
from quarterdeck.harness.types import Screen
from quarterdeck.seat import Seat,Launched
from quarterdeck.service import Service
from quarterdeck.tmux import Tmux, TmuxError
from .test_status_presentation import live


def metadata(ident='%1',pid=101,width=80):
    return f'{ident}\tcrew\tworker\t{pid}\t{width}\t24\t0\t0\t\n'


def test_pane_identity_queries_are_targeted_and_never_reuse_pid_or_width(monkeypatch):
    tmux=Tmux(socket='synthetic')
    calls=[];current=[metadata()]
    def run(*args,**kwargs):
        calls.append(args)
        return 'crew\n' if args[0]=='list-sessions' else current[0]
    monkeypatch.setattr(tmux,'run',run)
    assert tmux.pane('%1').pid==101
    assert len(calls)==1,'a single pane lookup must not enumerate the server twice'
    current[0]=metadata(pid=202,width=121)
    assert tmux.pane('%1').pid==202 and tmux.pane('%1').width==121
    current[0]=metadata(ident='%2')
    assert tmux.pane('%1') is None,'a target fallback is not the requested pane'


def test_door_reads_one_fresh_identity_and_capture_packet(tmp_path,monkeypatch):
    (tmp_path/'ship.toml').write_text('[ship]\ncode="checkout"\n[pools.p]\nprovider="fake"\n[sessions.crew]\n[seats.worker]\nsession="crew"\nharness="fake"\npool="p"\n')
    ship=shipmod.load(tmp_path);tmux=Tmux(socket='synthetic');calls=[]
    seat=Seat(ship,ship.seat('worker'),tmux);seat.folder.mkdir(parents=True)
    Launched('%1',101,datetime.now(timezone.utc).isoformat(),'p',None,['synthetic'],'synthetic','fake').write(seat.trusted/'launch.json')
    def run(*args,**kwargs):
        calls.append(args)
        if args[0]=='list-sessions':return 'crew\n'
        if args[0]=='capture-pane':return 'synthetic frame\n'
        return metadata()+('synthetic frame\n' if ';' in args else '')
    monkeypatch.setattr(tmux,'run',run)
    monkeypatch.setattr(seat.harness,'screen',lambda text:Screen(text,''))
    assert Door(seat).screen().text=='synthetic frame\n'
    assert len(calls)==1,'screen reads must not repeat whole-server enumeration and a separate capture'


def empty_service(tmp_path,monkeypatch,clients):
    (tmp_path/'ship.toml').write_text('[ship]\ncode="checkout"\nname="synthetic"\n[sessions.synthetic]\n')
    monkeypatch.setattr(servicemod.machine,'processes',lambda:{})
    return Service(shipmod.load(tmp_path),SimpleNamespace(clients_attached=lambda sessions:clients[0]))


def test_known_no_clients_skips_only_presentation_and_unknown_collects(tmp_path,monkeypatch):
    clock=[0.];clients=[False];collected=[];duties=[]
    monkeypatch.setattr(servicemod.time,'monotonic',lambda:clock[0])
    svc=empty_service(tmp_path,monkeypatch,clients)
    monkeypatch.setattr(svc,'account',lambda:duties.append('account'))
    monkeypatch.setattr(svc,'publish_status',lambda:collected.append(clock[0]))
    svc.tick()
    assert not collected,'no viewer must not trigger a complete status collection every tick'
    clients[0]=True;clock[0]=.8;svc.tick()
    clients[0]=None;clock[0]=6.0;svc.tick()
    assert collected==[.8] and duties==['account']*3, 'unchanged inputs wait thirty seconds'
    clock[0]=31.0;svc.tick()
    assert collected==[.8,31.0] and duties==['account']*4, 'unknown demand still permits a due refresh'
    svc.ledger.close()


def test_presentation_deadlines_do_not_move_actor_tick_cadence(tmp_path,monkeypatch):
    clock=[0.];clients=[True];published=[];actors=[]
    monkeypatch.setattr(servicemod.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(servicemod.signal,'signal',lambda *a:None)
    monkeypatch.setattr(servicemod.service_wait, 'Arrivals', lambda runtime: SimpleNamespace(
        refresh=lambda names: None, wait=lambda delay: servicemod.time.sleep(delay), close=lambda: None))
    svc=empty_service(tmp_path,monkeypatch,clients)
    monkeypatch.setattr(svc,'heartbeat',lambda:None)
    def account():
        actors.append(clock[0]);clock[0]+=.2
    monkeypatch.setattr(svc,'account',account)
    monkeypatch.setattr(svc,'publish_status',lambda:published.append(clock[0]))
    class Stop(BaseException):pass
    def sleep(delay):
        clock[0]+=delay
        if clock[0]>=2.9:raise Stop
    monkeypatch.setattr(servicemod.time,'sleep',sleep)
    with pytest.raises(Stop):svc.run()
    assert actors==pytest.approx([0.,1.2,2.4]),'record/actor duties keep sleep-after-work cadence'
    assert published,'the first demanded presentation is collected'
    assert all(b-a>=.7 for a,b in zip(published,published[1:])), 'short duties must not force more frequent collection'
    assert len(published)<=5,'fresh presentation must not require eight full collections in three actor ticks'
    svc.lock.close();svc.ledger.close()


def test_cold_canonical_status_keeps_fresh_facts_without_unrelated_cli_imports(live,tmp_path):
    ship,svc,lock=live
    (ship.runtime/'status-line.json').unlink()
    probe=tmp_path/'probe';probe.mkdir();modules=tmp_path/'modules.json'
    (probe/'sitecustomize.py').write_text("import atexit,json,os,sys\n@atexit.register\ndef finish():\n with open(os.environ['MODULE_LOG'],'w') as f:json.dump(sorted(sys.modules),f)\n")
    env={k:v for k,v in os.environ.items() if k not in ('QD_SHIP','QD_SEAT','QD_TMUX_SOCKET','TMUX','TMUX_PANE')}
    env.update(PYTHONNOUSERSITE='1',MODULE_LOG=str(modules),PYTHONPATH=str(probe)+os.pathsep+str(Path(__file__).parents[2]/'src'))
    done=subprocess.run([str(Path(sys.executable).parent/'qd'),'--ship',str(ship.folder),'status','--line'],env=env,capture_output=True,text=True)
    assert done.returncode==0 and done.stdout.startswith('service ok')
    loaded=json.loads(modules.read_text())
    assert 'quarterdeck.watch' in loaded,'a miss still obtains the ordinary independent current facts'
    assert 'quarterdeck.cli' not in loaded,'one text command must not build/import all verbs and their parser'


@pytest.mark.parametrize('answer,expected', [('',False),('client\tcrew\n',True),
    ('client\telsewhere\n',False),('client\tcrew\nmalformed\n',None),
    ('client\t\n',None),(None,None)])
def test_presentation_demand_requires_complete_native_client_evidence(monkeypatch,answer,expected):
    tmux=Tmux(socket='synthetic')
    def run(*args):
        if answer is None:raise TmuxError('synthetic unreadable client evidence')
        return answer
    monkeypatch.setattr(tmux,'run',run)
    assert tmux.clients_attached(['crew']) is expected
    assert tmux.clients_attached(['crew\nelsewhere']) is None


@pytest.mark.parametrize('changed',['pid','pane','launch','dead'])
def test_combined_screen_refuses_replaced_identity(tmp_path,monkeypatch,changed):
    from dataclasses import replace
    from quarterdeck.tmux import Pane
    (tmp_path/'ship.toml').write_text('[ship]\ncode="checkout"\n[pools.p]\nprovider="fake"\n[sessions.crew]\n[seats.worker]\nsession="crew"\nharness="fake"\npool="p"\n')
    seat=Seat(shipmod.load(tmp_path),shipmod.load(tmp_path).seat('worker'),Tmux(socket='synthetic'))
    seat.folder.mkdir(parents=True)
    seat.trusted.mkdir(parents=True, exist_ok=True)
    launch=Launched('%1',101,datetime.now(timezone.utc).isoformat(),'p',None,['synthetic'],'synthetic','fake')
    launch.write(seat.trusted/'launch.json')
    def read(*args):
        if changed=='launch':replace(launch,pid=202).write(seat.trusted/'launch.json')
        # Exercise the native returned-id check as well as the seat's PID binding.
        return metadata(ident='%2' if changed=='pane' else '%1',pid=202 if changed=='pid' else 101).replace('\t0\t0\t','\t0\t1\t' if changed=='dead' else '\t0\t0\t')+'frame\n'
    monkeypatch.setattr(seat.tmux,'run',read)
    assert Door(seat).screen() is None


def test_failed_due_collection_does_not_renew_success_or_retry_each_duty(live,monkeypatch):
    ship,svc,lock=live
    clock=[10.];calls=[]
    monkeypatch.setattr(servicemod.time,'monotonic',lambda:clock[0])
    before=(ship.runtime/'status-line.json').read_bytes()
    def failed():
        calls.append(clock[0]);raise OSError('synthetic read failed')
    monkeypatch.setattr(svc,'publish_status',failed)
    svc.status_due();svc.status_due()
    assert calls==[10.] and (ship.runtime/'status-line.json').read_bytes()==before
    clock[0]=9. # a backward clock forces a new collection, never renews the old one
    svc.status_due()
    assert calls==[10.,9.] and (ship.runtime/'status-line.json').read_bytes()==before


@pytest.mark.parametrize('native',[False,True])
def test_cold_text_failure_cannot_reuse_health_or_omit_the_native_failure_path(live,monkeypatch,native):
    ship,svc,lock=live
    path=ship.runtime/presentation.NAME
    path.write_text('{ corrupt')
    def failed(*args,**kwargs):
        raise TmuxError('synthetic native failure') if native else PermissionError('synthetic unreadable facts')
    monkeypatch.setattr(watch,'facts',failed)
    line=presentation.command_line(['--ship',str(ship.folder),'status','--line'])
    if native:
        assert line is None # ordinary CLI retains its error exit and alarm
    else:
        assert 'status unknown' in line and '0 waiting' not in line
    assert path.read_text()=='{ corrupt'


def test_idle_delivery_uses_its_fresh_owned_frame_without_a_second_liveness_query(tmp_path,monkeypatch):
    from quarterdeck.tmux import Pane
    (tmp_path/'ship.toml').write_text('[ship]\ncode="checkout"\n[pools.p]\nprovider="fake"\n[sessions.crew]\n[seats.worker]\nsession="crew"\nharness="fake"\npool="p"\n')
    tmux=Tmux(socket='synthetic');svc=Service(shipmod.load(tmp_path),tmux)
    seat=svc.seat('worker');seat.folder.mkdir(parents=True)
    Launched('%1',101,datetime.now(timezone.utc).isoformat(),'p',None,['synthetic'],'synthetic','fake').write(seat.trusted/'launch.json')
    calls=[];pid=[101]
    def packet(*args):
        calls.append(args)
        return Pane('%1','crew','worker',pid[0],80,24,False,False,None),'synthetic frame'
    monkeypatch.setattr(tmux,'read_pane',packet)
    def redundant(*args):raise AssertionError('redundant liveness query before the owned frame')
    monkeypatch.setattr(tmux,'pane',redundant)
    monkeypatch.setattr(type(seat.harness),'screen',lambda self,text:Screen(text,'human draft'))
    try:
        svc.deliveries()
        assert len(calls)==1 and svc.told['worker']=='text in the box'
        svc.told.clear();pid[0]=202
        svc.deliveries()
        assert len(calls)==2 and not svc.told,'a replacement cannot supply the prior seat obstruction'
    finally:svc.ledger.close()


def test_facts_application_refusal_keeps_bootstrap_error_exit_and_alarm(live,monkeypatch,capsys):
    from quarterdeck import main
    ship,svc,lock=live
    lock.close()
    def refused(*args,**kwargs):raise shipmod.ShipError('synthetic application refusal')
    monkeypatch.setattr(watch,'facts',refused)
    monkeypatch.setattr(sys,'argv',['qd','--ship',str(ship.folder),'status','--line'])
    assert main()==2
    captured=capsys.readouterr()
    assert 'synthetic application refusal' in captured.err
    assert captured.out.startswith('SERVICE DOWN') and 'status unknown' in captured.out
    assert '0 waiting' not in captured.out


@pytest.mark.parametrize('error',[PermissionError,UnicodeError])
def test_unreadable_native_client_query_collects_unknown_demand(tmp_path,monkeypatch,error):
    tmux=Tmux(socket='synthetic')
    def failed(*args,**kwargs):raise error('synthetic unreadable client query')
    monkeypatch.setattr(tmux,'run',failed)
    assert tmux.clients_attached(['crew']) is None
    (tmp_path/'ship.toml').write_text('[ship]\ncode="checkout"\nname="synthetic"\n[sessions.crew]\n')
    svc=Service(shipmod.load(tmp_path),tmux);collected=[]
    monkeypatch.setattr(svc,'publish_status',lambda:collected.append(True))
    try:
        svc.status_due()
        assert collected==[True],'unknown demand cannot suppress collection'
    finally:svc.ledger.close()
