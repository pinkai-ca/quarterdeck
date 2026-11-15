"""A ship whose common workspace is its own work/ (the forum's shape) has that folder checked before a jailed launch.

A jailed seat with no cwd of its own works in the ship's common workspace, and the jail binds it writable. Where that is
the ship's own work/, it is walked as the declaration writes it, from the ship folder down, never through a link, a
real folder of this user, set to 0700, before anything of the launch is made. An unjailed seat is untouched.

Real tmux and real bubblewrap, on a drydock copy shaped like the forum: workspace = "work", a jailed seat with no cwd.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from quarterdeck import sandbox, seat as seatmod, ship as shipmod
from quarterdeck.tmux import Tmux

from .test_sandbox import namespaces_allowed

pytestmark = pytest.mark.skipif(not namespaces_allowed(), reason="bubblewrap is not installed, or cannot make a namespace here (inside a jail)")


def forum_shaped(drydock: Path, *, jailed: bool = True, workspace: str = "work") -> Path:
    """The drydock with its common workspace at its own work/ and worker-1 jailed (or not), with no cwd."""
    text = (drydock / "ship.toml").read_text()
    text = shipmod.edit(text, "ship", "workspace", workspace)
    text = shipmod.edit(text, "seats.worker-1", "sandbox", jailed)
    (drydock / "ship.toml").write_text(text + '\n[seats.worker-1.settings]\nstep_seconds = "0.05"\n')
    return drydock / "work"


def worker(drydock: Path, tmux: Tmux) -> seatmod.Seat:
    ship = shipmod.load(drydock)
    return seatmod.Seat(ship, ship.seat("worker-1"), tmux)


def bound_writable(argv: list[str], folder: Path) -> bool:
    return any(argv[i] == "--bind" and argv[i + 1] == str(folder) for i in range(len(argv) - 1))


def assert_nothing_launched(seat: seatmod.Seat) -> None:
    assert seat.launched() is None, "no launch was recorded"
    pane = seat.pane()
    assert pane is None or pane.dead, "no pane runs the seat"


def test_a_link_planted_at_the_ships_work_refuses_a_jailed_launch_and_launches_nothing(drydock: Path, tmux: Tmux, tmp_path: Path):
    work = forum_shaped(drydock)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o755)
    elsewhere.chmod(0o755)
    work.symlink_to(elsewhere, target_is_directory=True)
    seat = worker(drydock, tmux)
    try:
        with pytest.raises(shipmod.ShipError, match=f"{work} is a link.*Remove the link"):
            seat.up(resume=False)
        assert_nothing_launched(seat)
        assert stat.S_IMODE(elsewhere.stat().st_mode) == 0o755 and not any(elsewhere.iterdir()), "its target untouched"
    finally:
        seat.down()


def test_a_real_work_folder_launches_jailed_bound_and_private(drydock: Path, tmux: Tmux):
    work = forum_shaped(drydock)
    work.mkdir(mode=0o755)
    work.chmod(0o755)
    (work / "left-by-a-reviewer.txt").write_text("synthetic")
    seat = worker(drydock, tmux)
    try:
        proof = seat.up(resume=False)
        assert "jailed" in proof
        launched = seat.launched()
        assert sandbox.seats_hidden(launched.argv) is True and bound_writable(launched.argv, work)
        assert stat.S_IMODE(work.lstat().st_mode) == 0o700 and not work.is_symlink(), "set private, as the ship's own root sets it"
        assert (work / "left-by-a-reviewer.txt").read_text() == "synthetic", "what is in it is kept"
    finally:
        seat.down()


def test_a_workspace_declared_through_a_link_to_the_ships_work_refuses(drydock: Path, tmux: Tmux, tmp_path: Path):
    """A workspace written elsewhere that reaches the ship's own work/ by a link: the walk could not see it."""
    work = drydock / "work"
    work.mkdir(mode=0o700)
    alias = tmp_path / "work-alias"
    alias.symlink_to(work, target_is_directory=True)
    forum_shaped(drydock, workspace=str(alias))
    seat = worker(drydock, tmux)
    try:
        with pytest.raises(shipmod.ShipError, match=f"reaches {work}.*through a link.*Declare the workspace as {work}"):
            seat.up(resume=False)
        assert_nothing_launched(seat)
    finally:
        seat.down()


def test_an_unjailed_seat_is_untouched(drydock: Path, tmux: Tmux):
    """A seat the ship runs open (sandbox = false) is not checked: its work/ keeps its mode."""
    work = forum_shaped(drydock, jailed=False)
    work.mkdir(mode=0o755)
    work.chmod(0o755)
    seat = worker(drydock, tmux)
    try:
        seat.up(resume=False)
        assert not sandbox.seats_hidden(seat.launched().argv)
        assert stat.S_IMODE(work.lstat().st_mode) == 0o755, "untouched"
    finally:
        seat.down()
