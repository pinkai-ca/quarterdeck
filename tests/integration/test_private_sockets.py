"""Private fixture cleanup is observable on real tmux, including its dead socket file."""
import os
from pathlib import Path
import tempfile
import threading
import sys
import time
import subprocess
import json

import pytest

from tests import conftest
from quarterdeck import tmux_socket
from quarterdeck.tmux import Tmux, TmuxError, PrivateTmuxError


def test_suite_fixture_removes_its_dead_socket_in_an_isolated_root(monkeypatch):
    with conftest.socket_container(os.environ['TMUX_TMPDIR'], 'qs-') as root:
        monkeypatch.setenv('TMUX_TMPDIR', str(root))
        fixture = conftest.tmux.__wrapped__()
        try:
            server = next(fixture)
            server.new_session('Synthetic')
            socket = Path(server.run('display-message', '-p', '#{socket_path}').strip())
            assert socket.is_relative_to(root) and socket.exists()
            with pytest.raises(StopIteration):
                next(fixture)  # the actual suite fixture's teardown, not a replica
            assert not server.running()
            assert not socket.exists(), 'fixture teardown left its private dead tmux socket'
            assert not any(root.iterdir()), 'fixture left private namespace artifacts'
        finally:
            fixture.close()


def test_unset_outer_root_uses_the_real_checkout_anchor_and_restores_environment():
    code = '''
import json, os
from pathlib import Path
import pytest
from tests import conftest
assert 'TMUX_TMPDIR' not in os.environ
old = conftest.ROOT/'work/qd-suite-12345678/qs-12345678/qd-pt-12345678/s-replacement'
anchor = conftest.socket_parent()
sample = anchor/(conftest.SESSION_PREFIX+'12345678')/'qs-12345678/qd-pt-12345678/s-replacement'
if len(os.fsencode(sample)) + conftest.SOCKET_MARGIN > conftest.SOCKET_LIMIT:
    # A checkout as long as a hosted runner's: the unset fallback must refuse, create nothing, change nothing.
    before = sorted(anchor.iterdir())
    scope = conftest.private_socket_root.__wrapped__()
    try:
        next(scope)
    except pytest.UsageError as error:
        assert str(anchor) in str(error) and 'too long' in str(error), error
    else:
        raise AssertionError('an overlong checkout anchor was accepted')
    assert 'TMUX_TMPDIR' not in os.environ and sorted(anchor.iterdir()) == before
    print(json.dumps(dict(checkout=str(conftest.ROOT), anchor=str(anchor), fits=False, residue=0)))
    raise SystemExit(0)
scope = conftest.private_socket_root.__wrapped__()
try:
    root = next(scope)
    assert root.parent == conftest.socket_parent()
    with conftest.socket_container(root, 'qs-') as inner:
        os.environ['TMUX_TMPDIR'] = str(inner)
        fixture = conftest.tmux.__wrapped__()
        try:
            server = next(fixture)
            server.new_session('Synthetic')
            endpoint = Path(server.socket)
            assert endpoint.is_relative_to(inner) and endpoint.exists()
            assert len(os.fsencode(endpoint.parent/'s-replacement')) < 108
            assert server.close(), server.held
            assert not endpoint.exists() and not any(inner.iterdir())
        finally:
            fixture.close()
            os.environ['TMUX_TMPDIR'] = str(root)
    assert not any(root.iterdir())
finally:
    scope.close()
assert 'TMUX_TMPDIR' not in os.environ and not root.exists()
print(json.dumps(dict(checkout=str(conftest.ROOT), anchor=str(root.parent), fits=True, old_nested_bytes=len(os.fsencode(old)), residue=0)))
'''
    env = {key: value for key, value in os.environ.items()
           if key not in ('TMUX_TMPDIR','TMUX','TMUX_PANE','QD_SHIP','QD_SEAT','QD_TMUX_SOCKET')}
    result = subprocess.run([sys.executable,'-c',code],env=env,cwd=conftest.ROOT,
                            capture_output=True,text=True,timeout=10)
    assert result.returncode == 0,result.stderr
    observed = json.loads(result.stdout)
    anchor = conftest.socket_parent()
    sample = anchor/(conftest.SESSION_PREFIX+'12345678')/'qs-12345678/qd-pt-12345678/s-replacement'
    assert observed['checkout'] == str(conftest.ROOT)
    assert observed['fits'] is (len(os.fsencode(sample)) + conftest.SOCKET_MARGIN <= conftest.SOCKET_LIMIT)
    assert Path(observed['anchor']) == anchor and observed['residue'] == 0


def test_failed_real_namespace_setup_removes_only_its_owned_empty_container(monkeypatch):
    container = None
    with pytest.raises(TmuxError,match='private tmux path too long'):
        with conftest.socket_container(os.environ['TMUX_TMPDIR'], 'qs-'+'x'*100) as container:
            monkeypatch.setenv('TMUX_TMPDIR',str(container))
            fixture = conftest.tmux.__wrapped__()
            try:
                next(fixture)
            finally:
                fixture.close()
    assert container is not None and not container.exists()


def test_unset_fallback_refuses_a_symlink_outside_its_verified_project(tmp_path,monkeypatch):
    project=tmp_path/'synthetic-project';project.mkdir()
    conftest.git(project,'init','--quiet')
    (project/'.gitignore').write_text('.testruns/\n')
    outside=tmp_path/'synthetic-outside';outside.mkdir()
    (project/'.testruns').symlink_to(outside,target_is_directory=True)
    monkeypatch.setattr(conftest,'ROOT',project)
    with pytest.raises(pytest.UsageError,match='cannot verify the project-owned socket anchor'):
        conftest.socket_parent()
    assert not list(outside.iterdir())


def test_the_anchor_is_the_projects_fixed_testruns_folder_with_a_margin(tmp_path,monkeypatch,private_socket_root):
    real=conftest.socket_parent()  # the real project's anchor, measured before ROOT is replaced
    effective=private_socket_root.parent  # the anchor this session's sockets actually use
    deepest=lambda anchor:len(os.fsencode(anchor/(conftest.SESSION_PREFIX+'12345678')/'qs-12345678/qd-pt-12345678/s-replacement'))
    assert deepest(effective)+conftest.SOCKET_MARGIN<=conftest.SOCKET_LIMIT  # wherever the suite runs, the margin holds
    if deepest(real)+conftest.SOCKET_MARGIN>conftest.SOCKET_LIMIT:
        assert effective!=real  # a checkout too long for its own anchor ran only on a shorter outer root
    project=tmp_path/'synthetic-project';project.mkdir()
    conftest.git(project,'init','--quiet')
    monkeypatch.setattr(conftest,'ROOT',project)
    anchor=conftest.socket_parent()
    assert anchor==project/'.testruns'/'s' and anchor.is_dir() and not anchor.is_symlink()
    assert (project/'.testruns'/'.gitignore').read_text()=='*\n'  # ignored even before .gitignore names it
    assert conftest.git(project,'status','--porcelain')==''


@pytest.mark.parametrize('replace',[False,True])
def test_failed_setup_retains_a_nonempty_or_replaced_container_with_its_reason(replace):
    container = None
    descriptor = peer = None
    try:
        with pytest.raises(RuntimeError,match='synthetic setup failure') as raised:
            with conftest.socket_container(os.environ['TMUX_TMPDIR'], 'qs-retain-') as container:
                if replace:
                    container.rmdir() # replace only this test's still-empty original directory
                    container.mkdir(mode=0o700)
                else:
                    peer = (container/'test-owned-peer').open('w+')
                    peer.write('synthetic peer');peer.flush()
                descriptor = os.open(container,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
                raise RuntimeError('synthetic setup failure')
        assert container.exists() and 'test socket container retained' in raised.value.__notes__[0]
        if replace:
            assert 'identity changed' in raised.value.__notes__[0]
        else:
            peer.seek(0)
            assert peer.read()=='synthetic peer'
    finally:
        if descriptor is not None:
            actual,created = container.lstat(),os.fstat(descriptor)
            assert (actual.st_dev,actual.st_ino)==(created.st_dev,created.st_ino)
            if peer is not None:
                path=container/'test-owned-peer';actual=path.lstat();created=os.fstat(peer.fileno())
                assert (actual.st_dev,actual.st_ino)==(created.st_dev,created.st_ino)
                path.unlink();peer.close() # this test's file, never an unfamiliar socket
            container.rmdir() # the original nonempty container or this test's replacement, now empty
            os.close(descriptor)


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


# An observer's shared lock makes one nonblocking attempt by design (DESIGN: status is never blocked by a
# holder), so a busy namespace is reported as uncertainty and the caller retries it within its own deadline.
# The child retries only that uncertainty; any other refusal still ends it, and it never gains ownership.
CHILD = ('import os,time\nfrom quarterdeck.tmux import Tmux, PrivateTmuxError\n'
         'client=Tmux(os.environ["QD_TMUX_SOCKET"])\n'
         'held,wait=os.environ.get("QD_TEST_HELD"),time.monotonic()+5\n'
         'while held and not os.path.exists(held) and time.monotonic()<wait:time.sleep(.01)\n'
         'if os.environ.get("QD_TEST_MARK"):open(os.environ["QD_TEST_MARK"],"w").close()\n'
         'deadline=time.monotonic()+4\n'
         'while True:\n'
         '    try:\n'
         '        path=client.run("display-message","-p","#{socket_path}").strip()\n'
         '        break\n'
         '    except PrivateTmuxError as error:\n'
         '        if "private namespace busy" not in str(error) or time.monotonic()>=deadline:raise\n'
         '        print("busy, retrying", flush=True)\n'
         '        time.sleep(.05)\n'
         'print(path, flush=True)\n'
         'print("owns="+str(client.close()),flush=True)\ntime.sleep(20)\n')


def child_reaches(tmux, tmp_path, env=None):
    tmux.new_session('Synthetic')
    pane=tmux.new_window('Synthetic','Child',tmp_path,[sys.executable,'-c',CHILD],env={'QD_TMUX_SOCKET':tmux.socket,**(env or {})})
    deadline=time.monotonic()+5
    text=''
    while time.monotonic()<deadline:
        text=tmux.capture(pane.id)
        if tmux.socket in text and 'owns=False' in text:return text
        time.sleep(.02)
    pytest.fail(f'child did not use the existing private binding without ownership; pane: {text.strip()[-400:]!r}')


def test_child_binding_reaches_the_same_private_server_without_creator_authority(tmux,tmp_path):
    child_reaches(tmux, tmp_path)


def test_child_binding_reaches_the_server_after_its_first_query_meets_the_creators_exclusive_lock(tmux,tmp_path,monkeypatch):
    """new_window drops its placeholder window under the exclusive lock after the child has started;
    an observer's shared lock makes one nonblocking attempt, so the child's first query can meet it.
    A handshake makes it meet it: the child first waits until the creator says it holds the lock."""
    mark,locked=tmp_path/'querying',tmp_path/'locked'
    original=tmux._drop_placeholder
    def held(session):
        with tmux_socket.locked(tmux.socket, tmux._owned.namespace):  # exclusive, as kill-window takes it
            locked.touch()
            deadline=time.monotonic()+5
            while not mark.exists() and time.monotonic()<deadline:time.sleep(.01)
            assert mark.exists(), 'the child never reached its first query'
            time.sleep(.5)  # across the child's first query
        original(session)
    monkeypatch.setattr(tmux,'_drop_placeholder',held)
    text=child_reaches(tmux, tmp_path, {'QD_TEST_MARK':str(mark),'QD_TEST_HELD':str(locked)})
    assert 'busy, retrying' in text, 'the first query met the exclusive lock'


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
        assert time.monotonic()-started<3*tmux_socket.EXCLUSIVE_WAIT+1 # three budgets, with no nested full-wait retry
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
