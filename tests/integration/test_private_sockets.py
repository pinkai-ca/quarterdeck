"""Private fixture cleanup is observable on real tmux, including its dead socket file."""
import os
from pathlib import Path
import tempfile
import threading
import sys
import time
import subprocess

import pytest

from tests import conftest
from quarterdeck import tmux_socket
from quarterdeck.tmux import Tmux, TmuxError, PrivateTmuxError


def test_suite_fixture_removes_its_dead_socket_in_an_isolated_root(monkeypatch):
    root = Path(tempfile.mkdtemp(prefix='qs-', dir=os.environ.get('TMUX_TMPDIR', tempfile.gettempdir())))
    monkeypatch.setenv('TMUX_TMPDIR', str(root))
    fixture = conftest.tmux.__wrapped__()
    server = next(fixture)
    server.new_session('Synthetic')
    socket = Path(server.run('display-message', '-p', '#{socket_path}').strip())
    assert socket.is_relative_to(root) and socket.exists()
    with pytest.raises(StopIteration):
        next(fixture)  # the actual suite fixture's teardown, not a replica
    assert not server.running()
    assert not socket.exists(), 'fixture teardown left its private dead tmux socket'
    assert not any(root.iterdir()), 'fixture left private namespace artifacts'
    root.rmdir()


def test_observer_can_reach_but_cannot_own_stop_or_bootstrap(tmux):
    tmux.new_session('Synthetic')
    observer = Tmux(socket=tmux.socket)
    assert observer.has_session('Synthetic')
    assert not observer.close() and 'authority' in observer.held
    with pytest.raises(TmuxError, match='authority'):
        observer.kill_server()
    delegated=observer.new_session('Delegated')
    observer.kill_session_id(delegated)
    assert not observer.has_session('Delegated')
    assert tmux.has_session('Synthetic')
    tmux.kill_server()
    with pytest.raises(TmuxError):
        observer.new_session('MustNotStart')
    assert not Path(tmux.socket).exists()
    tmux.new_session('RestartedByCreator')
    assert observer.has_session('RestartedByCreator')


@pytest.mark.parametrize('restart',[False,True])
def test_last_session_exit_leaves_only_the_creators_proved_dead_endpoint(tmux,restart):
    tmux.new_session('Synthetic')
    tmux.run('set-option','-s','exit-empty','on')
    tmux.kill_session('Synthetic')
    deadline=time.monotonic()+3
    while not tmux._owned.ended() and time.monotonic()<deadline:time.sleep(.01)
    assert tmux._owned.ended() and Path(tmux.socket).exists()
    if restart:
        tmux.new_session('Restarted')
        assert Tmux(socket=tmux.socket).has_session('Restarted')
    assert tmux.close(),tmux.held
    assert not Path(tmux.socket).parent.exists()


def test_child_binding_reaches_the_same_private_server_without_creator_authority(tmux,tmp_path):
    tmux.new_session('Synthetic')
    script = ('import time\nfrom quarterdeck.tmux import Tmux\n'
              'client=Tmux.default()\n'
              'print(client.run("display-message","-p","#{socket_path}").strip(), flush=True)\n'
              'print("owns="+str(client.close()),flush=True)\ntime.sleep(20)\n')
    pane=tmux.new_window('Synthetic','Child',tmp_path,[sys.executable,'-c',script],env={'QD_TMUX_SOCKET':tmux.socket})
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        text=tmux.capture(pane.id)
        if tmux.socket in text and 'owns=False' in text:break
        time.sleep(.02)
    else:pytest.fail('child did not use the existing private binding without ownership')


@pytest.mark.parametrize('live',[True,False])
def test_a_replacement_endpoint_is_not_the_previous_creators_socket(tmux,live):
    tmux.new_session('Original')
    tmux.kill_server()
    replacement = tmux.private_peer('')
    replacement.new_session('Replacement')
    if not live:
        replacement.run('kill-server') # stop, deliberately leave its recorded socket for the other creator
    before = Path(replacement.socket).lstat()
    try:
        assert not tmux.close() and 'replaced' in tmux.held
        after = Path(replacement.socket).lstat()
        assert (before.st_dev,before.st_ino)==(after.st_dev,after.st_ino)
        if live:assert replacement.has_session('Replacement')
    finally:
        assert replacement.close(), replacement.held


def test_live_peer_holds_the_namespace_without_losing_its_lock_or_reachability(tmux):
    tmux.new_session('Original')
    peer=tmux.private_peer()
    peer.new_session('Peer')
    try:
        with tmux_socket.locked(peer.socket,peer._owned.namespace) as held:
            with pytest.raises(tmux_socket.ShutdownPending,match='running'):
                peer._owned.remove_dead(held)
        assert not tmux.close() and 'peer' in tmux.held
        assert Tmux(socket=peer.socket).has_session('Peer')
        assert Path(peer.socket).exists()
    finally:
        assert peer.close(),peer.held


def test_unknown_lock_is_neither_repaired_nor_cleanup_authority(tmux):
    tmux.new_session('Synthetic')
    lock=Path(tmux.socket).parent/tmux_socket.LOCK
    lock.write_bytes(b'synthetic unfamiliar lock\n')
    try:
        observer=Tmux(socket=tmux.socket)
        with pytest.raises(TmuxError,match='unfamiliar'):
            observer.new_session('MustNotStart')
        assert not tmux.close() and 'unfamiliar' in tmux.held
        assert lock.read_bytes()==b'synthetic unfamiliar lock\n'
        assert Path(tmux.socket).exists()
    finally:
        lock.write_bytes(tmux_socket.HEADER) # restore only this fixture's deliberately modified owned header


def test_an_exclusive_lock_in_another_process_refuses_reads_and_cleanup_without_waiting_forever(tmux):
    tmux.new_session('Synthetic')
    endpoint=Path(tmux.socket);before=endpoint.lstat()
    code=('import fcntl,sys,time\n'
          'with open(sys.argv[1],"rb") as lock:\n'
          ' fcntl.flock(lock,fcntl.LOCK_EX)\n'
          ' print("held",flush=True)\n'
          ' time.sleep(10)\n')
    child=subprocess.Popen([sys.executable,'-c',code,str(endpoint.parent/tmux_socket.LOCK)],
                           stdout=subprocess.PIPE,text=True)
    try:
        assert child.stdout.readline().strip()=='held'
        probe=('from quarterdeck.tmux import Tmux,TmuxError\nimport sys\n'
               'try: Tmux(socket=sys.argv[1]).run("list-sessions")\n'
               'except TmuxError as error: print(error)\n'
               'else: raise AssertionError("query bypassed held exclusive lock")\n')
        result=subprocess.run([sys.executable,'-c',probe,tmux.socket],capture_output=True,text=True,timeout=2)
        assert result.returncode==0 and 'busy' in result.stdout
        with pytest.raises(PrivateTmuxError,match='busy'):
            tmux.running()
        with pytest.raises(PrivateTmuxError,match='busy'):
            tmux.pane('%0')
        with pytest.raises(PrivateTmuxError,match='busy'):
            tmux.read_pane('%0')
        started=time.monotonic()
        assert not tmux.close() and 'busy' in tmux.held
        with pytest.raises(TmuxError,match='busy'):
            tmux.run('kill-server') # generic commands must not bypass exclusive serialization
        with pytest.raises(TmuxError,match='busy'):
            tmux.run('list-sessions',';','kill-server')
        assert time.monotonic()-started<1.5 # three 250ms budgets, with no nested full-wait retry
        after=endpoint.lstat()
        assert (before.st_dev,before.st_ino)==(after.st_dev,after.st_ino)
    finally:
        child.terminate();child.wait(timeout=2)
    assert tmux.has_session('Synthetic')


def test_a_shared_transport_lease_allows_fresh_reads_and_typing_but_not_generic_shutdown(tmux,tmp_path):
    tmux.new_session('Synthetic')
    pane=tmux.new_window('Synthetic','Transport',tmp_path,['cat'])
    observer=Tmux(socket=tmux.socket)
    with tmux_socket.locked(tmux.socket,exclusive=False):
        assert observer.pane(pane.id).pid==pane.pid
        observer.paste(pane.id,'synthetic transport')
        observer.keys(pane.id,'Enter')
        assert 'synthetic transport' in observer.capture(pane.id)
        with pytest.raises(PrivateTmuxError,match='observer.*typed'):
            observer.run('list-sessions',';','kill-server')
        with pytest.raises(TmuxError,match='busy'):
            tmux.run('list-sessions',';','kill-server')
    assert tmux.has_session('Synthetic')


def test_replacement_at_the_final_removal_boundary_waits_and_revalidates(tmux,monkeypatch):
    tmux.new_session('Original')
    replacement=tmux.private_peer('')
    attempted=threading.Event();created=threading.Event();errors=[]
    real_unlink=tmux_socket.os.unlink;real_flock=tmux_socket.fcntl.flock
    worker=None;intercepted=[False]
    def replace():
        try:replacement.new_session('Replacement');created.set()
        except TmuxError as error:errors.append(str(error))
    def flock(fd,operation):
        if threading.current_thread() is worker and operation & tmux_socket.fcntl.LOCK_EX:
            attempted.set()
        return real_flock(fd,operation)
    def unlink(path,*args,**kwargs):
        nonlocal worker
        if path=='s' and not intercepted[0]:
            intercepted[0]=True
            worker=threading.Thread(target=replace,daemon=True);worker.start()
            assert attempted.wait(3)
            assert not created.is_set(),'a managed replacement bypassed the held removal lock'
        return real_unlink(path,*args,**kwargs)
    monkeypatch.setattr(tmux_socket.fcntl,'flock',flock)
    monkeypatch.setattr(tmux_socket.os,'unlink',unlink)
    closed=tmux.close()
    assert worker is not None
    worker.join(5);assert not worker.is_alive()
    if created.is_set():
        # A waiter may win the lock between endpoint shutdown and final namespace close.
        try:
            assert not closed and 'replaced' in tmux.held
            assert replacement.has_session('Replacement')
        finally:assert replacement.close(),replacement.held
    else:
        assert closed and errors,'a waiter through the removed namespace must be refused'
        assert not Path(tmux.socket).parent.exists()


@pytest.mark.parametrize('failure',[RuntimeError,KeyboardInterrupt])
def test_fixture_finally_cleans_known_ownership_on_failure_or_interruption(failure):
    fixture=conftest.tmux.__wrapped__();server=next(fixture)
    server.new_session('Synthetic')
    endpoint=Path(server.socket)
    with pytest.raises(failure):fixture.throw(failure('synthetic interrupted test'))
    assert not endpoint.exists() and not endpoint.parent.exists()


def test_interruption_during_exact_endpoint_removal_retains_original_creator_evidence(tmux,monkeypatch):
    tmux.new_session('Synthetic')
    endpoint=Path(tmux.socket);before=endpoint.lstat();unlink=tmux_socket.os.unlink
    def interrupted(path,*args,**kwargs):
        if path=='s':raise KeyboardInterrupt('synthetic cleanup interruption')
        return unlink(path,*args,**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(tmux_socket.os,'unlink',interrupted)
        with pytest.raises(KeyboardInterrupt) as raised:tmux.close()
        assert str(endpoint) in raised.value.__notes__[0]
        after=endpoint.lstat()
        assert (before.st_dev,before.st_ino)==(after.st_dev,after.st_ino)
        assert (endpoint.parent/tmux_socket.LOCK).exists()
    assert tmux.close(),tmux.held # the same creator, with its original descriptor and process evidence
    assert not endpoint.parent.exists()


def test_an_unfamiliar_fifo_lock_refuses_without_blocking_or_repair():
    root=Path(tempfile.mkdtemp(prefix='qs-fifo-',dir=os.environ['TMUX_TMPDIR']))
    path=root/tmux_socket.LOCK
    os.mkfifo(path,0o600);before=path.lstat()
    code=('import sys\nfrom quarterdeck.tmux_socket import locked,OwnershipError\n'
          'try:\n with locked(sys.argv[1]): pass\n'
          'except OwnershipError as error: print(error)\n'
          'else: raise AssertionError("unfamiliar special lock was accepted")\n')
    try:
        result=subprocess.run([sys.executable,'-c',code,str(root/'s')],capture_output=True,text=True,timeout=2)
        assert result.returncode==0 and 'unfamiliar' in result.stdout
        after=path.lstat()
        assert (before.st_dev,before.st_ino,before.st_mode)==(after.st_dev,after.st_ino,after.st_mode)
        assert list(root.iterdir())==[path]
    finally:
        # subprocess.run kills/waits a timed-out child before returning/raising.
        assert (path.lstat().st_dev,path.lstat().st_ino)==(before.st_dev,before.st_ino)
        path.unlink() # only this test-created FIFO, after the probe child ended
        root.rmdir()


@pytest.mark.parametrize('commands',[('kill-server',),('list-sessions',';','kill-server')])
def test_observer_generic_server_shutdown_is_refused_with_the_server_unchanged(tmux,commands):
    tmux.new_session('Synthetic')
    endpoint=Path(tmux.socket);before=endpoint.lstat()
    with pytest.raises(PrivateTmuxError,match='observer.*typed'):
        Tmux(socket=tmux.socket).run(*commands)
    after=endpoint.lstat()
    assert (before.st_dev,before.st_ino)==(after.st_dev,after.st_ino)
    assert tmux.has_session('Synthetic')


@pytest.mark.parametrize('commands',[
    ('start-server',),('source-file','synthetic-unused'),
    ('set-option','-s','exit-empty','on'),('if-shell','true','kill-server'),
    ('future-private-command',),('list-sessions',';','future-private-command')])
def test_other_observer_generic_mutations_refuse_before_dispatch(tmux,monkeypatch,commands):
    tmux.new_session('Synthetic')
    observer=Tmux(socket=tmux.socket)
    def forbidden(*args,**kwargs):
        raise AssertionError('refused generic mutation reached the native dispatcher')
    monkeypatch.setattr(observer,'_invoke',forbidden)
    with pytest.raises(PrivateTmuxError,match='observer.*typed'):
        observer.run(*commands)
    assert tmux.has_session('Synthetic')
