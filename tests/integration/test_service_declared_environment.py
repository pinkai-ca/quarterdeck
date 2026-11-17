"""The service and the ship's own tmux server start from a declared environment, never the caller's whole one.

The caller is a seat's pane (its QD_SEAT) or the desktop's shell (its secrets). Here the caller's environment is
seeded with decoys, a QD_SEAT and a pool's declared key; the real service is started on a throwaway ship, and the
ship's own tmux server on its own socket. Names only: no value is printed or compared but the decoys' absence.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from quarterdeck import servicectl, ship as shipmod
from quarterdeck.tmux import Tmux

from .test_ship_server import ship  # noqa: F401  the drydock in a short folder, so its own socket fits

DECOYS = ('QD_DECOY_OPENAI_API_KEY', 'QD_DECOY_JMAP_TOKEN', 'DECOY_CLIENT_SECRET', 'DECOY_HARMLESS')
SESSION = ('DISPLAY', 'SSH_AUTH_SOCK', 'DBUS_SESSION_BUS_ADDRESS', 'XAUTHORITY')  # the desktop's: kept for unjailed panes
ASKPASS = 'SUDO_ASKPASS'  # reaches no seat, no server and no service, whatever list names it
POOL_KEY = 'DECOY_POOL_API_KEY'  # declared in the ship file as a keyed endpoint's key: the service never holds it
KEY_POOL_KEY = 'DECOY_KEY_POOL_API_KEY'  # declared as a key pool's key: nor that


def environ_names(pid: int) -> set[str]:
    try:
        data = Path(f'/proc/{pid}/environ').read_bytes()
    except OSError:  # a process that has just ended
        return set()
    return {item.split(b'=', 1)[0].decode() for item in data.split(b'\0') if b'=' in item}


def seeded(monkeypatch) -> None:
    for name in (*DECOYS, *SESSION):
        monkeypatch.setenv(name, 'decoy-value')
    monkeypatch.setenv('QD_SEAT', 'signer')  # the pane that ran qd sync
    monkeypatch.setenv(POOL_KEY, 'decoy-key')
    monkeypatch.setenv(KEY_POOL_KEY, 'decoy-key')
    monkeypatch.setenv(ASKPASS, '/usr/bin/ssh-askpass')


def test_the_service_holds_only_its_declared_environment(drydock: Path, tmux, monkeypatch):
    file = drydock / 'ship.toml'
    text = file.read_text() + ('\n[pools.keyed]\nprovider = "local"\nendpoint = "http://127.0.0.1:9/v1"\n'
                               f'api_key = "env:{POOL_KEY}"\n'
                               f'\n[pools.paid]\nprovider = "synthetic"\napi_key = "env:{KEY_POOL_KEY}"\n')
    file.write_text(text)
    ship = shipmod.load(drydock)
    seeded(monkeypatch)
    monkeypatch.setenv('QD_TMUX_SOCKET', str(drydock / 'elsewhere' / 's'))  # the caller's, not declared
    monkeypatch.setenv('GIT_DIR', str(drydock / 'elsewhere' / '.git'))
    proof = servicectl.start(ship, qd=Path(sys.executable).parent / 'qd')
    try:
        pid = servicectl.read_heartbeat(ship.runtime)[0]
        names = environ_names(pid)
        print('service:', ' '.join(sorted(names)))  # names only
        assert not names & set(DECOYS), sorted(names & set(DECOYS))
        assert ASKPASS not in names, 'the service never holds the sudo helper'
        assert 'QD_SEAT' not in names, 'the service carries no seat'
        assert not names & {'QD_TMUX_SOCKET', 'GIT_DIR'}, 'a caller\'s socket and repository stay with the caller'
        assert 'service started' in proof and ('in its own scope qd-service-' in proof
                                               or "in the caller's scope: systemd-run" in proof), proof
        assert not names & {POOL_KEY, KEY_POOL_KEY}, 'no seat runs on a key, so the service holds none'
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
        everything = set(DECOYS) | {'QD_SEAT', POOL_KEY, KEY_POOL_KEY, ASKPASS}
        assert not names & everything, sorted(names & everything)
        assert not copied & everything, sorted(copied & everything)
        assert {'PATH', *SESSION} <= names, 'the desktop names an unjailed pane keeps'
    finally:
        subprocess.run(['tmux', '-S', server.socket, 'kill-server'], capture_output=True)


DESKTOP = {'DISPLAY': ':0', 'WAYLAND_DISPLAY': 'wayland-0', 'XDG_SESSION_TYPE': 'wayland', 'XDG_SESSION_ID': '2',
           'XDG_CURRENT_DESKTOP': 'GNOME', 'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/0/bus',
           'SSH_AUTH_SOCK': '/run/user/0/keyring/ssh', 'XAUTHORITY': '/run/user/0/.mutter-Xwaylandauth.QD'}
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
    monkeypatch.setenv(ASKPASS, '/usr/bin/ssh-askpass')
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
        assert ASKPASS not in unjailed, 'no unjailed seat holds the sudo helper'
        assert sandbox.seats_hidden(jailed_seat.launched().argv) is True
        inside = set()
        while not inside and time.monotonic() < deadline + 10:
            inside = {name for pid in descendants(jailed_seat.pane().pid) for name in environ_names(pid)
                      if 'QD_SEAT' in environ_names(pid)}
            time.sleep(0.05)
        print('jailed:', ' '.join(sorted(inside)))  # names only
        assert inside and not inside & set(DESKTOP), sorted(inside & set(DESKTOP))
        assert not inside & set(SECRET_DECOYS)
        assert ASKPASS not in inside, 'nor a jailed one'
    finally:
        seatmod.bring_down(ship, server)


def test_a_declared_test_socket_reaches_the_service(drydock: Path, tmux, monkeypatch):
    """A test's private namespace is declared: the service runs on that server, as before."""
    monkeypatch.setenv('QD_TMUX_SOCKET', tmux.socket)
    monkeypatch.setenv('GIT_DIR', str(drydock / 'elsewhere' / '.git'))
    ship = shipmod.load(drydock)
    servicectl.start(ship, qd=Path(sys.executable).parent / 'qd')
    try:
        names = environ_names(servicectl.read_heartbeat(ship.runtime)[0])
        assert 'QD_TMUX_SOCKET' in names and 'GIT_DIR' not in names
    finally:
        servicectl.stop(ship.runtime)


def test_a_running_servers_environment_is_cleaned_at_the_service_start(ship, monkeypatch):  # noqa: F811
    """A server started before the declared lists keeps a desktop's secrets and gives them to every new window."""
    import json
    import time
    from quarterdeck import seat as seatmod
    from quarterdeck.tmux import ship_socket
    socket = ship_socket(ship)
    socket.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    old = dict(os.environ, **DESKTOP, QD_DECOY_OPENAI_API_KEY='decoy', DECOY_CLIENT_SECRET='decoy', QD_SEAT='signer',
               SUDO_ASKPASS='/usr/bin/ssh-askpass')
    from tests.own_tmux_servers import OwnServers
    servers = OwnServers()  # the test's own server, ended in the end by the pid it ran as (tests/own_tmux_servers.py)
    servers.start(socket, '-s', 'Old', 'sleep 600', env=old)
    subprocess.run(['tmux', '-S', str(socket), 'set-environment', '-g', 'DECOY_JMAP_TOKEN', 'decoy'], check=True)
    for name, value in DESKTOP.items():
        subprocess.run(['tmux', '-S', str(socket), 'set-environment', '-g', name, value], check=True)
    # The caller is the operator's shell: the desktop's names, and secrets that must reach no one. (tmux marks a name of
    # its update-environment list that the creating client lacks as removed in the new session, so the desktop names
    # come from the caller as they do in use.)
    for name, value in DESKTOP.items():
        monkeypatch.setenv(name, value)
    for name in SECRET_DECOYS:
        monkeypatch.setenv(name, 'decoy-value')
    servicectl.start(ship, qd=Path(sys.executable).parent / 'qd')
    server = Tmux.for_ship(ship)
    try:
        log = ship.runtime / 'log' / 'service.jsonl'
        deadline = time.monotonic() + 15
        cleaned = None
        while cleaned is None and time.monotonic() < deadline:
            if log.exists():
                cleaned = next((json.loads(line) for line in log.read_text().splitlines()
                                if '"server-environment-cleaned"' in line), None)
            time.sleep(0.1)
        assert cleaned is not None, 'the service cleaned the running server'
        assert {'QD_DECOY_OPENAI_API_KEY', 'DECOY_CLIENT_SECRET', 'DECOY_JMAP_TOKEN', 'QD_SEAT',
                ASKPASS} <= set(cleaned['names'])
        assert all('decoy' not in str(value) for value in cleaned.values()), 'names only'
        captain = seatmod.Seat(ship, ship.seat('captain'), server)
        captain.up(resume=False)
        names = environ_names(captain.pane().pid)
        deadline = time.monotonic() + 10
        while 'QD_SEAT' not in names and time.monotonic() < deadline:
            time.sleep(0.05)
            names = environ_names(captain.pane().pid)
        print('unjailed after the clean:', ' '.join(sorted(names)))  # names only
        from quarterdeck import jail_environment
        assert not [name for name in names if jail_environment.looks_secret(name)], 'none secret'
        assert ASKPASS not in names, 'the sudo helper was cleaned too'
        assert set(DESKTOP) <= names, 'the desktop names are still there'
    finally:
        servicectl.stop(ship.runtime)
        seatmod.bring_down(ship, server)
        servers.close()  # its 'Old' session is no seat's: bring_down leaves it, and so its server


def test_a_server_or_session_that_holds_askpass_still_gives_it_to_no_seat(ship, monkeypatch):  # noqa: F811
    """Whatever the running server or the seat's session came to hold, the launch unsets the sudo helper by name."""
    import time
    from quarterdeck import seat as seatmod
    for name, value in DESKTOP.items():
        monkeypatch.setenv(name, value)
    server = Tmux.for_ship(ship)
    captain = seatmod.Seat(ship, ship.seat('captain'), server)
    try:
        server.new_session(captain.session.title, env={'QD_SHIP': str(ship.folder)})
        subprocess.run(['tmux', '-S', server.socket, 'set-environment', '-g', ASKPASS, '/usr/bin/ssh-askpass'], check=True)
        subprocess.run(['tmux', '-S', server.socket, 'set-environment', '-t', captain.session.title, ASKPASS,
                        '/usr/bin/ssh-askpass'], check=True)
        captain.up(resume=False)
        names = environ_names(captain.pane().pid)
        deadline = time.monotonic() + 10
        while 'QD_SEAT' not in names and time.monotonic() < deadline:
            time.sleep(0.05)
            names = environ_names(captain.pane().pid)
        print('unjailed under a server holding the helper:', ' '.join(sorted(names)))  # names only
        assert 'QD_SEAT' in names, 'the program started'
        assert ASKPASS not in names, 'the launch unset it'
        assert 'XAUTHORITY' in names, 'and the cookie path is kept'
    finally:
        seatmod.bring_down(ship, server)
