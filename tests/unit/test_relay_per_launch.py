"""A jailed seat's relay folder is its launch's own: an older relay that exits after a relaunch removes only its own
folder, and the new launch's folder and connection stay whole. Stand-in relays, a stand-in endpoint and stand-in
jails, in a short folder of the test's own (a unix socket's path is limited to about a hundred bytes)."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from quarterdeck import endpoint_relay, sandbox, ship as shipmod

SRC = Path(__file__).resolve().parents[2] / 'src'
JAIL = 'import os, sys, time\nwhile not os.path.exists(sys.argv[1]): time.sleep(0.05)\n'  # ends when told to


@pytest.fixture
def short(monkeypatch):
    folder = Path(tempfile.mkdtemp(prefix='qdrl-', dir='/tmp'))
    monkeypatch.setattr(endpoint_relay, 'RELAY_ROOT', folder, raising=False)
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


def echo_endpoint():
    """A stand-in model endpoint on the loopback: it answers each connection with what it was sent."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(('127.0.0.1', 0))
    listener.listen(8)
    def serve():
        while True:
            try:
                client, _ = listener.accept()
            except OSError:
                return
            with client:
                client.sendall(client.recv(64))
    threading.Thread(target=serve, daemon=True).start()
    return listener


def relay(folder: Path, port: int, gate: Path) -> subprocess.Popen:
    """A stand-in launch: the real relay module, serving a stand-in jail that ends when its gate file appears."""
    return subprocess.Popen(['env', f'PYTHONPATH={SRC}', sys.executable, '-m', 'quarterdeck.endpoint_relay', str(folder),
                             '127.0.0.1', str(port), '', '--', sys.executable, '-c', JAIL, str(gate)])


def wait_for(path: Path, seconds=10.0):
    until = time.monotonic() + seconds
    while not path.exists():
        assert time.monotonic() < until, f'{path.name} never appeared'
        time.sleep(0.02)


def planned(tmp_path: Path, monkeypatch):
    """Two launches of the same seat on an endpoint pool, planned as a relaunch plans them."""
    home = tmp_path / 'home'
    folder = home / 'dev' / 'ship'
    folder.mkdir(parents=True)
    (home / 'work').mkdir()
    ship = shipmod.Ship(folder, 'synthetic', 'operator', home / 'work', {}, {}, {})
    own = ship.runtime / 'seats' / 'worker'
    monkeypatch.setattr(sandbox, 'available', lambda: True)
    plan = lambda: sandbox.plan(ship, 'worker', own, own / 'home', home / 'work', '/usr/bin/python3', '/usr/bin',  # noqa: E731
                                home=home, endpoint='http://127.0.0.1:8000/v1')
    return plan(), plan()


def test_each_launch_names_its_own_relay_folder_and_its_launch_record_holds_it(tmp_path, monkeypatch, short):
    old, new = planned(tmp_path, monkeypatch)
    assert sandbox.relay_folder(old) != sandbox.relay_folder(new), 'a relaunch never shares its relay folder'
    assert old.launch and new.launch and old.launch != new.launch
    for jail in (old, new):
        argv = sandbox.command(jail, ['true'])  # what the launch record keeps as its argv
        assert str(sandbox.relay_folder(jail)) in argv, 'the launch record holds its relay folder by name'
        assert endpoint_relay.of_seat(sandbox.relay_folder(jail), jail.seat_folder), "the seat's relay, of that launch"


def test_an_old_relay_that_exits_after_the_new_one_starts_leaves_the_new_folder_and_connection_whole(tmp_path,
                                                                                                    monkeypatch, short):
    old, new = planned(tmp_path, monkeypatch)
    old_folder, new_folder = sandbox.relay_folder(old), sandbox.relay_folder(new)
    endpoint = echo_endpoint()
    port = endpoint.getsockname()[1]
    first = relay(old_folder, port, short / 'end-old')
    second = None
    try:
        wait_for(old_folder / endpoint_relay.SOCKET)
        second = relay(new_folder, port, short / 'end-new')  # the relaunch: the old relay is still alive
        wait_for(new_folder / endpoint_relay.SOCKET)
        (short / 'end-old').touch()  # the old launch's jail ends after the new one started: its relay exits
        assert first.wait(timeout=20) == 0
        assert (new_folder / endpoint_relay.SOCKET).exists(), "the old relay's exit never removes the new one's folder"
        assert old_folder == new_folder or not old_folder.exists(), 'the old relay removed its own folder'
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(10)
            client.connect(str(new_folder / endpoint_relay.SOCKET))
            client.sendall(b'ordinary')
            assert client.recv(64) == b'ordinary', "the new launch's connection to its endpoint is whole"
    finally:
        (short / 'end-old').touch()
        (short / 'end-new').touch()
        for process in (first, second):
            if process is not None:
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
        endpoint.close()
    assert not new_folder.exists(), 'each relay removes its own folder when its jail ends'
