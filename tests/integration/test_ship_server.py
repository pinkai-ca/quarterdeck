"""A ship on its own tmux server, and a ship moved there from tmux's default server, on private servers only.

The ship's own server here is a real tmux server on the ship's socket, in a short folder of this
session's private root; tmux's default server is stood in for by the private ``tmux`` fixture.
Every cleanup names its socket with -S.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from quarterdeck import cli, install, seat as seatmod, ship as shipmod, tmux as tmuxmod, watch
from quarterdeck.ship import ShipError
from quarterdeck.tmux import Tmux, ship_socket

from ..conftest import ROOT, TEST_TICK


@pytest.fixture
def ship(monkeypatch):
    """The drydock in a short folder, so that its own socket fits; its server is ended by its path."""
    folder = Path(tempfile.mkdtemp(prefix="h", dir=os.environ["TMUX_TMPDIR"])) / "dd"
    shutil.copytree(ROOT / "ships" / "drydock", folder)
    file = folder / "ship.toml"
    file.write_text(shipmod.edit(file.read_text(), "ship", "tick", TEST_TICK))
    monkeypatch.delenv("QD_TMUX_SOCKET", raising=False)
    monkeypatch.delenv("QD_SEAT", raising=False)
    monkeypatch.setenv("QD_SHIP", str(folder))
    monkeypatch.setattr(install, "handoff", lambda folder, argv: None)
    loaded = shipmod.load(folder)
    try:
        yield loaded
    finally:
        socket = ship_socket(loaded)
        if socket.exists():
            subprocess.run(["tmux", "-S", str(socket), "kill-server"], capture_output=True, timeout=10)
        shutil.rmtree(folder.parent)


def program_environment(pid: int, seconds: float = 10) -> dict[str, str]:
    """The pane's process environment once launch.sh has handed over to the program."""
    deadline = time.monotonic() + seconds
    while True:
        raw = Path(f"/proc/{pid}/environ").read_bytes().decode(errors="replace")
        found = dict(item.split("=", 1) for item in raw.split("\0") if "=" in item)
        if "QD_SEAT" in found or time.monotonic() >= deadline:
            return found
        time.sleep(0.05)


def test_a_seat_runs_on_the_ships_own_server_and_cannot_reach_it_by_TMUX(ship, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_CHILD_SESSION", "1")  # what a server started from a Claude seat would carry
    server = Tmux.for_ship(ship)
    socket = ship_socket(ship)
    assert server.ship and not server.legacy and server.socket == str(socket)
    captain = seatmod.Seat(ship, ship.seat("captain"), server)
    try:
        captain.up(resume=False)
        assert socket.is_socket() and (socket.parent.stat().st_mode & 0o777) == 0o700
        listed = subprocess.run(["tmux", "-S", str(socket), "list-sessions", "-F", "#{session_name}"],
                                capture_output=True, text=True, timeout=10).stdout.split()
        assert "Bridge" in listed
        env = program_environment(captain.pane().pid)
        assert "TMUX" not in env and "TMUX_PANE" not in env and "CLAUDE_CODE_CHILD_SESSION" not in env
        first = Path(env["TMUX_TMPDIR"])
        assert first.parent == captain.folder / "tmux" and not list(first.iterdir())
        assert (first.stat().st_mode & 0o777) == 0o700 and (first.parent.stat().st_mode & 0o777) == 0o700
        assert env["QD_TMUX_SOCKET"] == str(socket) and env["QD_SEAT"] == "captain"
        monkeypatch.setenv("QD_TMUX_SOCKET", env["QD_TMUX_SOCKET"])
        assert Tmux.for_ship(ship).socket == str(socket)  # qd inside the seat finds the same server
        monkeypatch.delenv("QD_TMUX_SOCKET")

        captain.down()
        captain.up(resume=False)  # each launch its own folder; the earlier, empty, is gone
        again = Path(program_environment(captain.pane().pid)["TMUX_TMPDIR"])
        assert again != first and again.parent == first.parent and not first.exists()
    finally:
        seatmod.bring_down(ship, server)


def test_a_private_tmux_folder_open_to_others_refuses_the_launch(ship):
    server = Tmux.for_ship(ship)
    captain = seatmod.Seat(ship, ship.seat("captain"), server)
    folder = captain.folder / "tmux"
    folder.mkdir(parents=True)
    folder.chmod(0o755)
    try:
        with pytest.raises(tmuxmod.TmuxError, match="mode 755"):
            captain.up(resume=False)
        assert (folder.stat().st_mode & 0o777) == 0o755 and not list(folder.iterdir())
    finally:
        seatmod.bring_down(ship, server)


def test_a_ship_in_two_places_starts_nothing_more(ship, tmux, monkeypatch):
    server = Tmux.for_ship(ship)
    captain = seatmod.Seat(ship, ship.seat("captain"), server)
    try:
        captain.up(resume=False)
        tmux.new_session("Crew", env={"QD_SHIP": str(ship.folder)})  # the stand-in default server holds another
        monkeypatch.setattr(tmuxmod, "_default_sockets", lambda: [Path(tmux.socket)])
        resolved = Tmux.for_ship(ship)
        assert resolved.socket == str(ship_socket(ship))  # its own server holds a session: that one is used
        worker = seatmod.Seat(ship, ship.seat("worker-1"), resolved)
        with pytest.raises(ShipError, match="runs in two places"):
            worker.up(resume=False)
        assert not worker.is_up and "Crew" not in resolved.sessions()
    finally:
        seatmod.bring_down(ship, server)


def test_a_ship_on_the_default_server_keeps_running_there_until_one_down_and_up(ship, tmux, monkeypatch, capsys):
    monkeypatch.setattr(tmuxmod, "_default_sockets", lambda: [Path(tmux.socket)])
    assert not Tmux.for_ship(ship).legacy  # nothing of this ship there yet: its own server
    tmux.new_session("Bridge", env={"QD_SHIP": str(ship.folder)})
    tmux.new_session("Bridge of another ship", env={"QD_SHIP": "/elsewhere"})
    found = Tmux.for_ship(ship)
    assert found.legacy and found.socket == tmux.socket

    captain = seatmod.Seat(ship, ship.seat("captain"), found)
    captain.up(resume=False)  # a launch before the move goes where the ship is, with the clean environment
    assert captain.pane() is not None and not Path(ship_socket(ship)).exists()
    env = program_environment(captain.pane().pid)
    assert "TMUX" not in env and env["QD_TMUX_SOCKET"] == tmux.socket  # told the server it runs on, explicitly

    facts = watch.facts(ship, found)
    assert facts.cutover == f"tmux: still on the default server {tmux.socket}; qd down, then qd up, moves it"

    from quarterdeck.service import Service
    service = Service(ship, found)  # a service started before the move

    assert cli.main(["down"]) == 0
    out = capsys.readouterr().out
    assert f"brought down on tmux's default server {tmux.socket}, which is left running" in out
    assert f"qd up now starts this ship on its own server {ship_socket(ship)}" in out
    assert tmux.running() and "Bridge of another ship" in tmux.sessions() and "Bridge" not in tmux.sessions()
    moved = Tmux.for_ship(ship)
    assert moved.ship and not moved.legacy and moved.socket == str(ship_socket(ship))
    service.rebind()
    assert service.tmux.socket == str(ship_socket(ship)) and not service.tmux.legacy


@pytest.fixture
def plain_default(ship):
    """A stand-in for tmux's default server that is no private namespace: a plain socket in a short folder."""
    folder = Path(tempfile.mkdtemp(prefix="p", dir=os.environ["TMUX_TMPDIR"]))
    socket = folder / "default"
    try:
        subprocess.run(["tmux", "-S", str(socket), "-f", "/dev/null", "new-session", "-d", "-s", "Bridge",
                        "-e", f"QD_SHIP={ship.folder}", "sleep 600"], check=True, capture_output=True, timeout=10)
        yield socket
    finally:
        subprocess.run(["tmux", "-S", str(socket), "kill-server"], capture_output=True, timeout=10)
        shutil.rmtree(folder)


def test_a_seat_told_the_default_server_is_held_to_it_only_while_the_ship_is_there(ship, plain_default, monkeypatch):
    monkeypatch.setenv("QD_TMUX_SOCKET", str(plain_default))
    told = Tmux.for_ship(ship)
    assert told.legacy and told.socket == str(plain_default)
    subprocess.run(["tmux", "-S", str(plain_default), "new-session", "-d", "-s", "Other",
                    "-e", "QD_SHIP=/elsewhere", "sleep 600"], check=True, capture_output=True, timeout=10)
    subprocess.run(["tmux", "-S", str(plain_default), "kill-session", "-t", "=Bridge:"], check=True, timeout=10)
    subprocess.run(["tmux", "-S", str(plain_default), "rename-session", "-t", "=Other:", "Bridge"], check=True, timeout=10)
    with pytest.raises(tmuxmod.TmuxError, match="will not run this ship there"):
        Tmux.for_ship(ship)  # another ship's session of the same name is not this ship's
