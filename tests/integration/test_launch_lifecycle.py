"""Launch retirement counts actual private windows and owned process identities."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from quarterdeck import seat as seatmod
from quarterdeck.harness.base import Launch
from quarterdeck.ship import ShipError
from .test_sandbox import namespaces_allowed
from .waiting import wait_for


def process_identity(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().split(') ', 1)[1].split()
    except FileNotFoundError:
        return None
    return int(fields[19]), fields[0]


def process_tree(pid):
    """Only traverse descendants of the exact fresh private pane, never host inventory."""
    found = {}
    todo = [pid]
    while todo:
        current = todo.pop()
        identity = process_identity(current)
        if identity is None:
            continue
        found[current] = identity[0]
        try:
            todo.extend(map(int, Path(f'/proc/{current}/task/{current}/children').read_text().split()))
        except FileNotFoundError:
            pass
    return found


def survivors(identities):
    return {pid: current for pid, start in identities.items()
            if (current := process_identity(pid)) is not None and current[0] == start}


def windows(tmux):
    return tmux.run('list-windows', '-a', '-F', '#{window_id}').splitlines() if tmux.sessions() else []


@pytest.mark.skipif(not namespaces_allowed(), reason='bubblewrap namespace unavailable')
def test_repeated_fresh_and_down_end_program_jail_descendants_and_windows(drydock, tmux, tmp_path, monkeypatch):
    from quarterdeck import ship as shipmod
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    program = workspace / 'owned-tree.py'
    program.write_text('import subprocess,sys,time\n'
                       'child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(300)"])\n'
                       'print("owned tree ready",flush=True)\ntime.sleep(300)\n')
    ship = shipmod.load(drydock)
    spec = replace(ship.seat('worker-1'), cwd=workspace, sandbox=True)
    seat = seatmod.Seat(ship, spec, tmux)
    monkeypatch.setattr(seat.harness, 'launch', lambda *a, **kw: Launch((__import__('sys').executable, str(program))))
    previous = {}
    counts = []
    for index in range(3):
        seat.up(False) if index == 0 else seat.fresh()
        launch = seat.launched()
        pane = tmux.pane(launch.pane)
        assert pane.pid == launch.pid
        wait_for(lambda: 'owned tree ready' in tmux.capture(pane.id))
        current = process_tree(pane.pid)
        assert len(current) >= 4, 'jail supervisor/init, program and child are all measured'
        wait_for(lambda: not survivors(previous))
        assert len(windows(tmux)) == 1
        counts.append({'stage': index, 'windows': len(windows(tmux)), 'current_processes': len(current),
                       'previous_processes': len(survivors(previous))})
        previous.update(current)
    seat.down()
    wait_for(lambda: not survivors(previous))
    assert windows(tmux) == []
    assert seat.launched() is None
    counts.append({'stage': 'down', 'windows': 0, 'previous_processes': 0})
    print('LIFECYCLE_COUNTS=' + json.dumps(counts))


@pytest.mark.parametrize('operation', ['down', 'fresh', 'up'])
def test_unmatched_present_pane_keeps_launch_and_refuses_new_window(drydock, tmux, operation):
    """A synthetic stale launch PID exercises refusal; it does not prove native root cause."""
    from quarterdeck import ship as shipmod
    ship = shipmod.load(drydock)
    seat = seatmod.Seat(ship, ship.seat('worker-1'), tmux)
    seat.up(False)
    launch = seat.launched()
    path = seat.folder / 'launch.json'
    data = json.loads(path.read_text())
    data['pid'] = launch.pid + 1  # mismatching evidence grants no ownership of the live pane
    path.write_text(json.dumps(data))
    retained = path.read_bytes()
    current = process_identity(launch.pid)
    before = windows(tmux)
    with pytest.raises(ShipError, match='identity|ownership|launch|pane'):
        getattr(seat, operation)()
    assert path.read_bytes() == retained
    assert windows(tmux) == before
    assert process_identity(launch.pid)[0] == current[0]
    assert tmux.pane(launch.pane).pid == launch.pid


def test_failed_native_retirement_retains_launch_and_running_program(drydock, tmux, monkeypatch):
    from quarterdeck import ship as shipmod
    from quarterdeck.tmux import TmuxError
    ship = shipmod.load(drydock)
    seat = seatmod.Seat(ship, ship.seat('worker-1'), tmux)
    seat.up(False)
    launch = seat.launched()
    path = seat.folder / 'launch.json'
    retained = path.read_bytes()
    original = tmux._invoke

    def refuse(*args, **kwargs):
        if args[0] == 'if-shell':
            raise TmuxError('synthetic native retirement refusal')
        return original(*args, **kwargs)

    monkeypatch.setattr(tmux, '_invoke', refuse)
    with pytest.raises(TmuxError, match='synthetic native retirement refusal'):
        seat.down()
    assert path.read_bytes() == retained
    assert tmux.pane(launch.pane).pid == launch.pid
    assert len(windows(tmux)) == 1


def test_replacement_after_python_check_is_refused_by_native_pid_guard(drydock, tmux, monkeypatch):
    from quarterdeck import ship as shipmod
    from quarterdeck.tmux import TmuxError
    ship = shipmod.load(drydock)
    seat = seatmod.Seat(ship, ship.seat('worker-1'), tmux)
    seat.up(False)
    launch = seat.launched()
    path = seat.folder / 'launch.json'
    retained = path.read_bytes()
    original = tmux.kill_window
    replacements = []

    def replace_before_close(ident, *, expected_pid=None):
        assert ident == launch.pane and expected_pid == launch.pid
        tmux._invoke('respawn-pane', '-k', '-t', ident, 'cat')
        replacement = tmux.pane(ident)
        assert replacement.pid != launch.pid
        replacements.append(replacement)
        return original(ident, expected_pid=expected_pid)

    monkeypatch.setattr(tmux, 'kill_window', replace_before_close)
    with pytest.raises(TmuxError, match='changed process identity'):
        seat.down()
    assert path.read_bytes() == retained
    assert len(replacements) == 1
    assert tmux.pane(launch.pane).pid == replacements[0].pid
    assert process_identity(replacements[0].pid) is not None
    assert len(windows(tmux)) == 1


@pytest.mark.parametrize('phase', ['before-close', 'after-close'])
def test_intervening_launch_snapshot_is_not_unlinked(drydock, tmux, monkeypatch, phase):
    from quarterdeck import ship as shipmod
    ship = shipmod.load(drydock)
    seat = seatmod.Seat(ship, ship.seat('worker-1'), tmux)
    seat.up(False)
    launch = seat.launched()
    path = seat.folder / 'launch.json'
    replacement = replace(launch, at='2030-01-01T00:00:00+00:00')
    if phase == 'before-close':
        monkeypatch.setattr(seat, 'record', lambda: replacement.write(path))
    else:
        original = tmux.kill_window

        def changed(ident, *, expected_pid=None):
            original(ident, expected_pid=expected_pid)
            replacement.write(path)
        monkeypatch.setattr(tmux, 'kill_window', changed)
    with pytest.raises(ShipError, match='launch identity changed'):
        seat.down()
    assert seat.launched() == replacement
    assert len(windows(tmux)) == (1 if phase == 'before-close' else 0)
