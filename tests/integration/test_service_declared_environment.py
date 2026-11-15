"""The service and the ship's own tmux server start from a declared environment, never the caller's whole one.

The caller is a seat's pane (its QD_SEAT) or the desktop's shell (its secrets). Here the caller's environment is
seeded with decoys, a QD_SEAT and a pool's declared key; the real service is started on a throwaway ship, and the
ship's own tmux server on its own socket. Names only: no value is printed or compared but the decoys' absence.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from quarterdeck import servicectl, ship as shipmod
from quarterdeck.tmux import Tmux

from .test_ship_server import ship  # noqa: F401  the drydock in a short folder, so its own socket fits

DECOYS = ('QD_DECOY_OPENAI_API_KEY', 'QD_DECOY_JMAP_TOKEN', 'DECOY_CLIENT_SECRET', 'DECOY_HARMLESS')
SESSION = ('DISPLAY', 'SSH_AUTH_SOCK', 'DBUS_SESSION_BUS_ADDRESS')  # the desktop's: kept for unjailed panes
POOL_KEY = 'DECOY_POOL_API_KEY'  # declared in the ship file as a pool's key: the service reads it


def environ_names(pid: int) -> set[str]:
    try:
        data = Path(f'/proc/{pid}/environ').read_bytes()
    except OSError:  # a process that has just ended
        return set()
    return {item.split(b'=', 1)[0].decode() for item in data.split(b'\0') if b'=' in item}


def seeded(monkeypatch) -> None:
    for name in (*DECOYS, *SESSION):
        monkeypatch.setenv(name, 'decoy-value')
    monkeypatch.setenv('QD_SEAT', 'una')  # the pane that ran qd sync
    monkeypatch.setenv(POOL_KEY, 'decoy-key')


def test_the_service_holds_only_its_declared_environment(drydock: Path, tmux, monkeypatch):
    file = drydock / 'ship.toml'
    text = file.read_text() + ('\n[pools.keyed]\nprovider = "local"\nendpoint = "http://127.0.0.1:9/v1"\n'
                               f'api_key = "env:{POOL_KEY}"\n')
    file.write_text(text)
    ship = shipmod.load(drydock)
    seeded(monkeypatch)
    proof = servicectl.start(ship, qd=Path(sys.executable).parent / 'qd')
    try:
        pid = servicectl.read_heartbeat(ship.runtime)[0]
        names = environ_names(pid)
        print('service:', ' '.join(sorted(names)))  # names only
        assert not names & set(DECOYS), sorted(names & set(DECOYS))
        assert 'QD_SEAT' not in names, 'the service carries no seat'
        assert 'service started' in proof and ('in its own scope qd-service-' in proof
                                               or "in the caller's scope: systemd-run" in proof), proof
        assert POOL_KEY in names, 'a pool key the ship file names is what the service needs'
        assert {'PATH', 'HOME', 'QD_SHIP', *SESSION} <= names, 'it may start the server unjailed panes inherit'
        shipped = Path(f'/proc/{pid}/environ').read_bytes().split(b'\0')
        assert f'QD_SHIP={ship.folder}'.encode() in shipped, 'its own ship, whatever the caller held'
    finally:
        servicectl.stop(ship.runtime)


def test_the_ships_own_tmux_server_starts_from_the_declared_environment(ship, monkeypatch):  # noqa: F811
    seeded(monkeypatch)
    server = Tmux.for_ship(ship)
    assert server.ship and not server.legacy and Path(server.socket).is_relative_to(ship.runtime)
    try:
        server.new_session('Declared environment')
        shown = subprocess.run(['tmux', '-S', server.socket, 'show-environment', '-g'],
                               capture_output=True, text=True, check=True).stdout.splitlines()
        session = subprocess.run(['tmux', '-S', server.socket, 'show-environment', '-t', 'Declared environment'],
                                 capture_output=True, text=True, check=True).stdout.splitlines()
        names = {line.split('=', 1)[0] for line in shown if '=' in line}
        copied = {line.split('=', 1)[0] for line in session if '=' in line}  # tmux's update-environment copies
        print('server:', ' '.join(sorted(names)), '| session:', ' '.join(sorted(copied)))  # names only
        everything = set(DECOYS) | {'QD_SEAT', POOL_KEY}
        assert not names & everything, sorted(names & everything)
        assert not copied & everything, sorted(copied & everything)
        assert {'PATH', *SESSION} <= names, 'the desktop names an unjailed pane keeps'
    finally:
        subprocess.run(['tmux', '-S', server.socket, 'kill-server'], capture_output=True)


DESKTOP = {'DISPLAY': ':0', 'WAYLAND_DISPLAY': 'wayland-0', 'XDG_SESSION_TYPE': 'wayland', 'XDG_SESSION_ID': '2',
           'XDG_CURRENT_DESKTOP': 'GNOME', 'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/0/bus',
           'SSH_AUTH_SOCK': '/run/user/0/keyring/ssh'}
SECRET_DECOYS = ('QD_DECOY_OPENAI_API_KEY', 'QD_DECOY_JMAP_TOKEN', 'DECOY_CLIENT_SECRET', 'DECOY_HARMLESS')


def descendants(root: int) -> list[int]:
    children: dict[int, list[int]] = {}
    for entry in Path('/proc').iterdir():
        if entry.name.isdigit():
            try:
                children.setdefault(int((entry / 'stat').read_text().rsplit(')', 1)[1].split()[1]), []).append(
                    int(entry.name))
            except OSError:
                continue
    found, queue = [], [root]
    while queue:
        for child in children.get(queue.pop(), []):
            found.append(child)
            queue.append(child)
    return found


def test_an_unjailed_pane_has_the_desktop_names_and_no_decoy_and_a_jailed_one_has_none(ship, monkeypatch):  # noqa: F811
    import time
    from quarterdeck import sandbox, seat as seatmod
    if not sandbox.available():
        import pytest
        pytest.skip('bubblewrap is not installed')
    workspace = ship.folder.parent / 'work'  # a jailed seat works outside the ship folder
    workspace.mkdir()
    text = shipmod.edit(ship.file.read_text(), 'seats.worker-1', 'sandbox', True)
    text = shipmod.edit(text, 'ship', 'workspace', str(workspace))
    ship.file.write_text(text)
    ship = shipmod.load(ship.folder)
    for name, value in DESKTOP.items():
        monkeypatch.setenv(name, value)
    for name in SECRET_DECOYS:
        monkeypatch.setenv(name, 'decoy-value')
    server = Tmux.for_ship(ship)
    open_seat = seatmod.Seat(ship, ship.seat('captain'), server)
    jailed_seat = seatmod.Seat(ship, ship.seat('worker-1'), server)
    try:
        open_seat.up(resume=False)
        jailed_seat.up(resume=False)
        unjailed = environ_names(open_seat.pane().pid)
        deadline = time.monotonic() + 10
        while 'QD_SEAT' not in unjailed and time.monotonic() < deadline:  # launch.sh hands over to the program
            time.sleep(0.05)
            unjailed = environ_names(open_seat.pane().pid)
        print('unjailed:', ' '.join(sorted(unjailed)))  # names only
        assert set(DESKTOP) <= unjailed, sorted(set(DESKTOP) - unjailed)
        assert not unjailed & set(SECRET_DECOYS), sorted(unjailed & set(SECRET_DECOYS))
        assert sandbox.seats_hidden(jailed_seat.launched().argv) is True
        inside = set()
        while not inside and time.monotonic() < deadline + 10:
            inside = {name for pid in descendants(jailed_seat.pane().pid) for name in environ_names(pid)
                      if 'QD_SEAT' in environ_names(pid)}
            time.sleep(0.05)
        print('jailed:', ' '.join(sorted(inside)))  # names only
        assert inside and not inside & set(DESKTOP), sorted(inside & set(DESKTOP))
        assert not inside & set(SECRET_DECOYS)
    finally:
        seatmod.bring_down(ship, server)
