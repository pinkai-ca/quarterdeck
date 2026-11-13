"""Presentation reuse stays optional and cannot certify a dead, busy or different service."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from quarterdeck import presentation, servicectl, ship as shipmod, watch
from quarterdeck.service import Service


@pytest.fixture
def live(tmp_path):
    (tmp_path/'ship.toml').write_text('[ship]\ncode="checkout"\nname="synthetic"\n')
    ship=shipmod.load(tmp_path);ship.runtime.mkdir(parents=True)
    lock=servicectl.acquire_lock(ship.runtime);svc=Service(ship,SimpleNamespace())
    svc.tick()
    assert (ship.runtime/presentation.NAME).exists()
    yield ship,svc,lock
    lock.close();svc.ledger.close()


def test_snapshot_keeps_every_rendered_alarm_and_unknown_marker(live,monkeypatch):
    ship,svc,lock=live
    facts=watch.Facts('ok',3,2,1,needs_human=['worker'],unarmed=['operator'],
                      walls=['synthetic unknown reset'],disks=[('synthetic',94)],login=[('synthetic','unknown')],
                      queued=1,oldest_queued=2,pending=['worker: prior launch unconfirmed'],routes=['worker on fake/other'],
                      held=['worker'],usage=[dict(pool='unused-provider',reading=None,reason='unknown')])
    monkeypatch.setattr(watch,'facts',lambda *args,**kwargs:facts)
    assert presentation.publish(ship,svc.tmux,svc.status_framework,svc.store,svc.attempts)
    assert presentation.cached_line(ship.folder)==facts.line()
    assert 'usage' not in facts.line() and 'reset' not in facts.line()
    (ship.runtime / presentation.NAME).unlink()
    assert presentation.command_line(['--ship',str(ship.folder),'status','--line']) == facts.line()


@pytest.mark.parametrize('change',['missing','corrupt','version','declaration','source','pid','start','heartbeat','busy','death','handoff'])
def test_invalid_or_unowned_snapshot_requires_the_ordinary_independent_reader(live,monkeypatch,change):
    ship,svc,lock=live;path=ship.runtime/presentation.NAME;data=json.loads(path.read_text())
    assert presentation.cached_line(ship.folder) is not None
    if change=='missing':path.unlink()
    elif change=='corrupt':path.write_text('{ unparsed')
    elif change=='declaration':ship.file.write_text(ship.file.read_text()+'\n# changed\n')
    elif change=='source':monkeypatch.setattr(presentation,'framework_stamp',lambda:['different code'])
    elif change=='heartbeat':servicectl.heartbeat_path(ship.runtime).write_text('malformed')
    elif change=='busy':
        stamp=(datetime.now(timezone.utc)-timedelta(seconds=20)).isoformat()
        servicectl.heartbeat_path(ship.runtime).write_text(f'{os.getpid()} {stamp}\n')
    elif change=='death':lock.close()
    elif change=='handoff':
        installed=ship.runtime/'venv/bin/qd';installed.parent.mkdir(parents=True);installed.write_text('synthetic')
    else:
        data[{'version':'format','pid':'pid','start':'process_start'}[change]]={'version':True,'pid':-1,'start':'different'}[change]
        path.write_text(json.dumps(data))
    assert presentation.cached_line(ship.folder) is None
    if change=='death':assert watch.facts(ship,SimpleNamespace()).line().startswith('SERVICE DOWN')  # law: nothing-waits-silently
    if change=='heartbeat':assert 'heartbeat unknown' in watch.service_state(ship.runtime)
    if change=='busy':assert watch.service_state(ship.runtime).startswith('busy 20')


@pytest.mark.parametrize('wall_delta,mono_delta,usable',[(0,0,True),(1_000_000_000,1_000_000_000,True),
    (3_000_000_000,3_000_000_000,True),(3_000_000_001,3_000_000_001,False),
    (-1,-1,False),(100_000_000,0,False)])
def test_cache_lifetime_has_no_clock_or_aging_shortcut(live,monkeypatch,wall_delta,mono_delta,usable):
    ship,svc,lock=live;data=json.loads((ship.runtime/presentation.NAME).read_text())
    monkeypatch.setattr(presentation.time,'time_ns',lambda:data['wall']+wall_delta)
    monkeypatch.setattr(presentation.time,'monotonic_ns',lambda:data['monotonic']+mono_delta)
    assert (presentation.cached_line(ship.folder) is not None) is usable


def test_failed_collection_does_not_renew_an_old_success(live,monkeypatch):
    ship,svc,lock=live;path=ship.runtime/presentation.NAME;before=path.read_bytes();data=json.loads(before)
    def unavailable(*args,**kwargs):raise PermissionError('synthetic unavailable evidence')
    monkeypatch.setattr(watch,'facts',unavailable)
    with pytest.raises(PermissionError):svc.publish_status()
    assert presentation.cached_line(ship.folder) is None, 'failed facts cannot hide behind a still-young rendering'
    assert not path.exists(), 'discard the optional rendering instead of renewing prior success'
    monkeypatch.setattr(presentation.time,'time_ns',lambda:data['wall']+3_000_000_001)
    monkeypatch.setattr(presentation.time,'monotonic_ns',lambda:data['monotonic']+3_000_000_001)
    assert presentation.cached_line(ship.folder) is None


@pytest.mark.parametrize('change',['death','busy','unknown'])
def test_long_tick_rendering_still_requires_current_independent_owner_evidence(live,monkeypatch,change):
    ship,svc,lock=live;path=ship.runtime/presentation.NAME;before=path.read_bytes();data=json.loads(before)
    monkeypatch.setattr(presentation.time,'time_ns',lambda:data['wall']+2_500_000_000)
    monkeypatch.setattr(presentation.time,'monotonic_ns',lambda:data['monotonic']+2_500_000_000)
    assert presentation.cached_line(ship.folder)==data['line'],'a live rendering covers the measured long tick'
    if change=='death':lock.close()
    elif change=='busy':
        stamp=(datetime.now(timezone.utc)-timedelta(seconds=20)).isoformat()
        servicectl.heartbeat_path(ship.runtime).write_text(f'{os.getpid()} {stamp}\n')
    else:servicectl.heartbeat_path(ship.runtime).write_text('unparsed heartbeat\n')
    assert presentation.cached_line(ship.folder) is None  # law: nothing-waits-silently
    assert path.read_bytes()==before,'consumer and owner refusal never renew success'


def test_cold_console_avoids_full_reading_imports_but_falls_back_after_death(live,tmp_path):
    ship,svc,lock=live
    probe=tmp_path/'probe';probe.mkdir();modules=tmp_path/'modules.json'
    (probe/'sitecustomize.py').write_text("import atexit,json,os,sys\n@atexit.register\ndef finish():\n with open(os.environ['MODULE_LOG'],'w') as f:json.dump(sorted(sys.modules),f)\n")
    env={k:v for k,v in os.environ.items() if k not in ('QD_SHIP','QD_SEAT','QD_TMUX_SOCKET','TMUX','TMUX_PANE')}
    env.update(PYTHONNOUSERSITE='1',MODULE_LOG=str(modules),PYTHONPATH=str(probe)+os.pathsep+str(Path(__file__).parents[2]/'src'))
    command=[str(Path(sys.executable).parent/'qd'),'--ship',str(ship.folder),'status','--line']
    expected=presentation.cached_line(ship.folder)
    done=subprocess.run(command,env=env,capture_output=True,text=True,check=True)
    assert done.stdout.strip()==expected
    loaded=json.loads(modules.read_text())
    assert not {'quarterdeck.cli','quarterdeck.watch','quarterdeck.service'} & set(loaded), 'a fresh cached status job must not construct the full reader'
    lock.close()
    done=subprocess.run(command,env=env,capture_output=True,text=True,check=True)
    assert done.stdout.startswith('SERVICE DOWN')
    assert 'quarterdeck.watch' in json.loads(modules.read_text())


def test_json_and_unrecognized_arguments_never_take_the_text_shortcut(live):
    ship,svc,lock=live
    assert presentation.command_line(['--json','--ship',str(ship.folder),'status','--line']) is None
    assert presentation.command_line(['--ship','--json','status','--line']) is None
    assert presentation.command_line(['--ship',str(ship.folder),'status','--line','--help']) is None


def test_unreadable_fallback_is_an_unknown_alarm_never_an_empty_cached_success(live,capsys):
    from quarterdeck import cli
    ship,svc,lock=live
    attempts=ship.runtime/'post/attempts';attempts.mkdir(parents=True,exist_ok=True)
    (attempts/'synthetic.json').write_text('{ malformed')
    assert cli.main(['--ship',str(ship.folder),'status','--line'])==0
    text=capsys.readouterr().out
    assert 'status unknown' in text and '0 waiting' not in text
    assert cli.main(['--json','--ship',str(ship.folder),'status','--line'])==0
    data=json.loads(capsys.readouterr().out)
    assert data['facts'] is None and data['error']=='status evidence unavailable'


def test_dead_service_alarm_survives_a_declared_ship_that_the_full_reader_refuses(live):
    ship,svc,lock=live
    ship.file.write_text('[ship\nmalformed')
    lock.close()
    env={k:v for k,v in os.environ.items() if k not in ('QD_SHIP','QD_SEAT','QD_TMUX_SOCKET','TMUX','TMUX_PANE')}
    env.update(PYTHONNOUSERSITE='1',PYTHONPATH=str(Path(__file__).parents[2]/'src'))
    done=subprocess.run([str(Path(sys.executable).parent/'qd'),'--ship',str(ship.folder),'status','--line'],env=env,capture_output=True,text=True)
    assert done.returncode==2 and done.stderr
    assert done.stdout.startswith('SERVICE DOWN') and 'status unknown' in done.stdout


def test_framework_identity_is_content_bound_even_with_matching_file_metadata(tmp_path,monkeypatch):
    package=tmp_path/'package';package.mkdir();source=package/'presentation.py';source.write_text('first')
    metadata=source.stat();stat=Path.stat
    monkeypatch.setattr(presentation,'__file__',str(source))
    monkeypatch.setattr(Path,'stat',lambda path,*a,**kw:metadata if path==source else stat(path,*a,**kw))
    before=presentation.framework_stamp()
    source.write_text('other')
    assert presentation.framework_stamp()!=before
