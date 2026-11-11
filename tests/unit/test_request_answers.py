"""A request client sees a complete service answer, never its writer's unfinished bytes."""
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from quarterdeck import door as doormod, durable, servicectl
from quarterdeck.service import Service
from quarterdeck.ship import ShipError
from .test_request_isolation import svc


@pytest.mark.parametrize('outcome',['success','refusal','failure'])
@pytest.mark.parametrize('phase',['created','partial'])
def test_client_waits_for_complete_success_or_failure_answer(svc,monkeypatch,outcome,phase):
    service,folder=svc
    paused,release=threading.Event(),threading.Event()
    request_ready,observed=threading.Event(),threading.Event()
    reader_release=threading.Event()
    result={};errors=[];called=[]
    original_text,original_write,original_replace=Path.write_text,os.write,os.replace
    original_exists=Path.exists

    def interrupt(self):
        called.append(self.seat.name)
        if outcome=='refusal':raise ShipError('synthetic refusal: no control available')
        if outcome=='failure':raise RuntimeError('synthetic action failure')
        return 'synthetic complete control proof: confirmed'

    def pause():
        paused.set()
        assert release.wait(5),'answer publisher was not released'

    def text_write(path,text,*args,**kwargs):
        if threading.current_thread() is not publisher or path.suffix!='.done':
            return original_text(path,text,*args,**kwargs)
        # Hold the main implementation's actual final-name writer too. Its open
        # descriptor survives the client's premature unlink, reproducing the loss.
        with path.open('w') as stream:
            split=len(text)//2 if phase=='partial' else 0
            stream.write(text[:split]);stream.flush()
            pause()
            stream.write(text[split:])
        return len(text)

    def fd_write(fd,data):
        if threading.current_thread() is publisher and not paused.is_set():
            path=Path(os.readlink(f'/proc/self/fd/{fd}'))
            if path.parent==folder and '.done.' in path.name and path.suffix=='.tmp':
                if phase=='partial':
                    count=original_write(fd,data[:len(data)//2])
                    pause()
                    return count
                pause()
        return original_write(fd,data)

    def replace(source,target,*args,**kwargs):
        value=original_replace(source,target,*args,**kwargs)
        if threading.current_thread() is client and Path(target).suffix=='.json':
            request_ready.set()
        return value

    def exists(path):
        if threading.current_thread() is client and path.suffix=='.done':
            assert paused.wait(5),'reader must check during the controlled publication pause'
        return original_exists(path)

    def waiting(_):
        observed.set()  # reader checked the paused writer and kept waiting
        assert reader_release.wait(5),'request reader was not released'

    def read():
        try:result['answer']=servicectl.request(service.runtime,'stop',seat='survivor')
        except BaseException as error:errors.append(error)
        finally:observed.set()  # old reader instead returns unfinished text here

    def publish():
        try:service.requests()
        except BaseException as error:errors.append(error)

    client=threading.Thread(target=read)
    publisher=threading.Thread(target=publish)
    monkeypatch.setattr(doormod.Door,'interrupt',interrupt)
    monkeypatch.setattr(servicectl,'alive',lambda _:True)
    monkeypatch.setattr(servicectl,'ANSWER_SECONDS',10)
    monkeypatch.setattr(servicectl,'time',SimpleNamespace(monotonic=time.monotonic,sleep=waiting))
    monkeypatch.setattr(Path,'write_text',text_write)
    monkeypatch.setattr(Path,'exists',exists)
    monkeypatch.setattr(os,'write',fd_write)
    monkeypatch.setattr(os,'replace',replace)
    client.start()
    try:
        assert request_ready.wait(2)
        publisher.start()
        assert paused.wait(2) and observed.wait(2)
        assert not errors
        assert called==['survivor']
        assert list(folder.glob('*.claimed')),'the replay guard survives pending publication'
        assert not result,f'client consumed an unfinished answer: {result!r}'
        assert not list(folder.glob('*.done')),'only a complete answer gets the final name'
        if outcome=='failure':
            assert len(list((folder/'failed').glob('*.json')))==1
            assert not list(folder.glob('*.json')),'failure archival precedes its answer'
    finally:
        release.set();reader_release.set()
        if publisher.ident is not None:publisher.join(5)
        client.join(5)
        assert not publisher.is_alive() and not client.is_alive(),'no controlled writer/reader survives'
    assert not errors
    if outcome=='success':assert result=={'answer':'synthetic complete control proof: confirmed'}
    elif outcome=='refusal':assert result=={'answer':'refused: synthetic refusal: no control available'}
    else:
        assert result['answer'].startswith("failed: RuntimeError('synthetic action failure');")
        assert result['answer'].endswith('and the service log has the trace')
    service.requests()
    assert called==['survivor'],'answer publication/consumption never repeats the action'
    assert not list(folder.glob('*.json')) and not list(folder.glob('*.claimed'))
    assert not list(folder.glob('*.done')) and not list(folder.glob('*.tmp'))


@pytest.mark.parametrize('stage',['rename','folder-sync'])
@pytest.mark.parametrize('archive',[True,False])
def test_publication_failure_preserves_replay_guard_and_raw_evidence(svc,monkeypatch,stage,archive):
    service,folder=svc;called=[]
    bad=folder/'a-stop.json';raw=json.dumps(dict(kind='stop',seat='survivor'))
    bad.write_text(raw)
    if not archive:(folder/'failed').write_text('synthetic archival obstruction')
    monkeypatch.setattr(doormod.Door,'interrupt',lambda self:called.append(self.seat.name) or 'synthetic complete proof')
    original_replace,original_sync=os.replace,durable.sync_folder
    after_rename=[False]

    def replace(source,target,*args,**kwargs):
        if Path(target)==bad.with_suffix('.done'):
            if stage=='rename':raise OSError('synthetic answer rename refusal')
            after_rename[0]=True
        return original_replace(source,target,*args,**kwargs)

    def sync(path):
        if after_rename[0]:
            after_rename[0]=False
            raise OSError('synthetic answer folder-sync refusal')
        return original_sync(path)

    monkeypatch.setattr(os,'replace',replace)
    monkeypatch.setattr(durable,'sync_folder',sync)
    service.requests()
    assert called==['survivor']
    assert bad.with_suffix('.claimed').read_text()==raw
    assert (folder/'failed'/bad.name if archive else bad).read_text()==raw
    answer=bad.with_suffix('.done')
    if stage=='rename':assert not answer.exists()
    else:
        assert answer.read_text().startswith("failed: OSError('synthetic answer folder-sync refusal');")
        assert answer.read_text().endswith('and the service log has the trace\n')
    assert not list(folder.glob('*.tmp'))
    healthy=folder/'b-stop.json';healthy.write_text(raw)
    restarted=Service(service.ship,None)
    try:
        service.requests();restarted.requests()
    finally:restarted.ledger.close()
    assert called==['survivor','survivor'],'claimed action stays held across ticks/restart; later request runs once'
    assert healthy.with_suffix('.done').read_text()=='synthetic complete proof\n'
    assert bad.with_suffix('.claimed').read_text()==raw
