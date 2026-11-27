"""Private tmux status commands see a stand-in's exit from every session."""

from datetime import datetime
import os
import shlex
import subprocess

from quarterdeck import seat as seatmod, ship as shipmod

from .waiting import wait_for


def test_every_status_command_names_unexpected_death_until_the_seat_returns(drydock, tmux):
    ship = shipmod.load(drydock)
    seatmod.bring_up(ship, tmux)
    desk = seatmod.Seat(ship, ship.seat('desk'), tmux)
    assert desk.session.human
    env = dict(os.environ, QD_TMUX_SOCKET=tmux.socket)
    env.pop('QD_SHIP', None)
    env.pop('QD_SEAT', None)

    def status_of(session):
        setting = tmux.run('show-option', '-v', '-t', f'={session.title}:', 'status-right').strip()
        assert setting.startswith('#(') and setting.endswith(')')
        command = shlex.split(setting[2:-1])
        assert command[1:] == ['--ship', str(ship.folder), 'status', '--line']
        return subprocess.run(command, env=env, capture_output=True, text=True, check=True).stdout

    old = desk.launched()
    tmux.keys(old.pane, 'C-c')  # the fake harness exits normally on this key

    def reaped_desk():
        pane = desk.pane()
        return pane if pane is not None and pane.dead and pane.exit_status is not None else None

    dead = wait_for(reaped_desk)
    assert dead.exit_status == 0 and dead.dead_time is not None
    when = datetime.fromtimestamp(dead.dead_time).astimezone().isoformat(timespec='seconds')
    alert = f'desk: pane died at {when}; exit status 0'
    for session in ship.sessions.values():
        assert alert in status_of(session)

    subprocess.run([str(seatmod.qd_bin() / 'qd'), '--ship', str(ship.folder), 'down', 'worker-1'],
                   env=env, capture_output=True, text=True, check=True)
    line = status_of(ship.sessions['bridge'])
    assert alert in line and 'worker-1: pane died' not in line
    assert seatmod.Seat(ship, ship.seat('worker-1'), tmux).launched() is None

    desk.up(resume=False)
    assert desk.is_up and desk.launched().pane != old.pane
    for name in ('bridge', 'office'):
        assert 'pane died' not in status_of(ship.sessions[name])
