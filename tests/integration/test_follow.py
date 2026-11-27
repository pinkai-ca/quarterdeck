"""Follow geometry and terminal lifetime on a private server with synthetic seats."""

from contextlib import contextmanager
from dataclasses import replace
import errno
import fcntl
import json
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import termios

import pytest

from quarterdeck import cli, follow_geometry, lifecycle, post, seat, servicectl, ship as shipmod, shutdown, trusted_files

from .waiting import wait_for


@pytest.fixture
def rig(drydock, tmux, monkeypatch):
    monkeypatch.setenv('QD_TMUX_SOCKET', tmux.socket)
    monkeypatch.delenv('QD_SEAT', raising=False)
    ship = shipmod.load(drydock)
    panes = {}
    for name in ('worker-1', 'desk'):
        spec = ship.seats[name]
        title = ship.session_of(spec).title
        tmux.new_session(title, env={'QD_SHIP': str(ship.folder)})
        command = ['sh', '-c', f'printf "\\033[42;1H%s" "synthetic screen {name}"; exec cat']
        pane = tmux.new_window(title, name, ship.folder, command)
        stamp = post.now().isoformat()
        launch = seat.Launched(pane.id, pane.pid, stamp, 'fake', None, ['cat'], None,
                               harness='fake', start=shutdown.process_start(pane.pid))
        launch.write(trusted_files.folder(ship.runtime, name) / trusted_files.LAUNCH)
        tmux.run('resize-window', '-t', pane.id, '-x', '130', '-y', '42')
        tmux.run('set-option', '-wu', '-t', pane.id, 'window-size')
        panes[name] = pane
    return ship, tmux, panes


def mark(rig):
    ship, tmux, _ = rig
    ship.folder.parent.chmod(0o700)
    follow_geometry.mark_disposable(ship, tmux)
    assert follow_geometry.disposable(ship, tmux)


@contextmanager
def running_service(ship):
    command = [sys.executable, '-c',
               'import sys; from pathlib import Path; from quarterdeck import service, ship; '
               'from quarterdeck.tmux import Tmux; s = ship.load(Path(sys.argv[1])); '
               'service.Service(s, Tmux.for_ship(s)).run()', str(ship.folder)]
    log = ship.folder / 'synthetic-service.log'
    with log.open('w') as output, subprocess.Popen(command, stdout=output, stderr=output,
                                                    start_new_session=True) as process:
        try:
            def ready():
                assert process.poll() is None, log.read_text()
                beat = servicectl.read_heartbeat(ship.runtime)
                return beat is not None and beat[0] == process.pid

            wait_for(ready)
            yield
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()


@pytest.fixture
def disposable_rig(rig):
    ship, _, _ = rig
    mark(rig)
    with running_service(ship):
        yield rig


@contextmanager
def viewer(ship, *args):
    command = (sys.executable, '-c', 'from quarterdeck.cli import main; raise SystemExit(main())',
               '--ship', str(ship.folder), 'attach', '--follow', *map(str, args))
    child, terminal = pty.fork()
    if child == 0:
        fcntl.ioctl(1, termios.TIOCSWINSZ, struct.pack('HHHH', 30, 81, 0, 0))
        os.execv(sys.executable, command)
    output = bytearray()
    result = None

    def poll():
        nonlocal result
        while select.select([terminal], [], [], 0)[0]:
            try:
                text = os.read(terminal, 65536)
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
                break
            if not text:
                break
            output.extend(text)
        if result is None:
            pid, status = os.waitpid(child, os.WNOHANG)
            if pid == child:
                result = os.waitstatus_to_exitcode(status)
        return result

    def waited():
        wait_for(lambda: poll() is not None)
        return result, output.decode('utf-8', errors='replace')

    try:
        yield child, terminal, poll, waited, output
    finally:
        if poll() is None:
            os.kill(child, signal.SIGKILL)
            os.waitpid(child, 0)
        os.close(terminal)


def size(tmux, pane):
    latest = tmux.pane(pane.id)
    return (latest.width, latest.height) if latest is not None else None


@pytest.mark.parametrize('key', [b'q', b'\x03'])
def test_clip_other_sessions_and_restore_on_q_or_ctrl_c(disposable_rig, key):
    ship, tmux, panes = disposable_rig
    crew, human = panes['worker-1'], panes['desk']
    cast = ship.folder / 'screen.cast'
    journal = follow_geometry.journal_path(ship.runtime)
    with viewer(ship, '--record', cast, 'crew') as (child, terminal, poll, waited, output):
        def narrowed():
            assert poll() is None, output.decode(errors='replace')
            return size(tmux, crew) == (81, 42)

        wait_for(narrowed)
        assert follow_geometry.Intent.read(ship.runtime).pid == child
        with lifecycle.claim(ship.runtime, 'worker-1', wait=1):
            assert size(tmux, crew) == (81, 42), 'an open viewer owns no seat claim'
        post.Store(ship.runtime).new('order', 'operator', 'worker-1', 'synthetic arrival')
        wait_for(lambda: poll() is None and b'worker-1' in output and b'incoming' in output)
        assert size(tmux, human) == (130, 42)
        assert tmux.clients_attached(['Crew', 'Office']) is False
        assert set(tmux.sessions()) == {'Crew', 'Office'}
        os.write(terminal, key)
        result, output = waited()
        assert result == 0, output
        assert '\x1b[?1049l' in output
    assert not journal.exists() and not follow_geometry.intent_path(ship.runtime).exists()
    assert size(tmux, crew) == (130, 42)
    assert tmux.window_geometry(crew).sizing == ''
    header, *events = map(json.loads, cast.read_text().splitlines())
    assert (header['width'], header['height'], header['version']) == (81, 30, 2)
    emitted = ''.join(event[2] for event in events)
    assert 'worker-1' in emitted and 'Maeve' not in emitted and 'synthetic screen desk' not in emitted


def test_resize_and_restore_share_delivery_claim_and_keep_exact_option(rig):
    mark(rig)
    ship, tmux, panes = rig
    crew, human = panes['worker-1'], panes['desk']
    tmux.run('set-option', '-w', '-t', crew.id, 'window-size', 'largest')
    with follow_geometry.lease(ship.runtime):
        resize = follow_geometry.Resize(ship, tmux)
        with lifecycle.claim(ship.runtime, 'worker-1'):
            resize.apply({'worker-1': (50, None)})
            assert size(tmux, crew) == (130, 42) and not resize.original

        store = post.Store(ship.runtime)
        message = store.new('order', 'operator', 'worker-1', 'synthetic held payload')
        reader = seat.Seat(ship, ship.seats['worker-1'], tmux)
        launch = reader.launched()
        receipts = post.Attempts(ship.runtime)
        attempt = receipts.open(message, 'worker-1', crew.id, crew.pid, launch.at)
        resize.apply({'worker-1': (50, None)})
        assert size(tmux, crew) == (130, 42) and not resize.original
        receipts.close(attempt, 'delivered', 'synthetic record confirmed')
        resize.apply({'worker-1': (50, None), 'desk': (50, None)})
        assert size(tmux, crew) == (50, 42) and size(tmux, human) == (130, 42)
        with lifecycle.claim(ship.runtime, 'worker-1'):
            assert resize.restore() == ('worker-1',)
            assert size(tmux, crew) == (50, 42)
        assert resize.restore() == ()
        assert size(tmux, crew) == (130, 42)
        assert tmux.window_geometry(crew).sizing == 'largest'
        assert not tmux.resize_owned_window(replace(tmux.window_geometry(crew), pid=crew.pid + 1), 65)


def test_live_shaped_view_crops_every_window_and_creates_no_geometry_intent(rig):
    ship, tmux, panes = rig
    assert cli.main(['--ship', str(ship.folder), 'attach', '--follow', '--fit']) == 2
    cast = ship.folder / 'live-shaped.cast'
    with viewer(ship, '--record', cast) as (_, terminal, poll, waited, output):
        wait_for(lambda: poll() is None and b'Maeve' in output and b'worker-1' in output and b'Picard' in output)
        store = post.Store(ship.runtime)
        store.new('order', 'operator', 'worker-1', 'synthetic crew arrival')
        store.new('order', 'operator', 'desk', 'synthetic office arrival')
        wait_for(lambda: poll() is None and b'synthetic screen worker-1' in output and b'synthetic screen desk' in output)
        assert all(size(tmux, pane) == (130, 42) for pane in panes.values())
        assert not follow_geometry.intent_path(ship.runtime).exists()
        assert not follow_geometry.journal_path(ship.runtime).exists()
        os.write(terminal, b'q')
        result, output = waited()
        assert result == 0, output
    assert all(size(tmux, pane) == (130, 42) for pane in panes.values())


def test_fit_changes_all_selected_disposable_windows(disposable_rig):
    ship, tmux, panes = disposable_rig
    with viewer(ship, '--fit', 'crew', 'office') as (_, terminal, poll, waited, _):
        wait_for(lambda: poll() is None and all(size(tmux, pane) == (40, 29) for pane in panes.values()))
        os.write(terminal, b'q')
        result, output = waited()
        assert result == 0, output
    assert all(size(tmux, pane) == (130, 42) for pane in panes.values())


def test_restore_a_retained_window_after_its_program_exits(rig):
    mark(rig)
    ship, tmux, panes = rig
    crew = panes['worker-1']
    tmux.run('set-option', '-w', '-t', crew.id, 'remain-on-exit', 'on')
    with follow_geometry.lease(ship.runtime):
        resize = follow_geometry.Resize(ship, tmux)
        resize.apply({'worker-1': (50, None)})
        assert size(tmux, crew) == (50, 42)
        os.kill(crew.pid, signal.SIGTERM)
        wait_for(lambda: tmux.pane(crew.id).dead)
        assert resize.restore() == ()
        assert size(tmux, crew) == (130, 42)


def test_crash_recovery_and_down_do_not_require_the_viewer(disposable_rig):
    ship, tmux, panes = disposable_rig
    crew = panes['worker-1']
    journal = follow_geometry.journal_path(ship.runtime)
    with viewer(ship, 'crew') as (child, _, poll, waited, _):
        wait_for(lambda: poll() is None and size(tmux, crew) == (81, 42))
        os.kill(child, signal.SIGKILL)
        assert waited()[0] == -signal.SIGKILL
    wait_for(lambda: not journal.exists() and size(tmux, crew) == (130, 42))
    wait_for(lambda: not follow_geometry.intent_path(ship.runtime).exists())
    with viewer(ship, 'crew') as (_, _, poll, waited, output):
        # The new view restores the journal before taking a fresh width.
        wait_for(lambda: poll() is None and b'worker-1' in output and size(tmux, crew) == (81, 42))
        result = cli.main(['--ship', str(ship.folder), 'down'])
        assert result == 0
        result, output = waited()
        assert result == 0, output
    assert tmux.sessions() == []
    with running_service(ship):
        wait_for(lambda: not journal.exists() and not follow_geometry.intent_path(ship.runtime).exists())
