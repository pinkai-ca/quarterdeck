"""A seat's home through the seat code's own launches: made 0700, a widened one mended, a link refused, an absent
one refused where a resume expects it. On the fake-only drydock, with a private tmux server."""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

import pytest

from quarterdeck import seat as seatmod, ship as shipmod
from quarterdeck.harness import resume_target
from quarterdeck.ship import ShipError
from quarterdeck.tmux import Tmux

from .waiting import wait_for


@pytest.fixture(autouse=True)
def ship_umask():
    """The live ship's umask (0002): without the step, every home it made was 0775."""
    previous = os.umask(0o002)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.fixture
def ship(drydock: Path) -> shipmod.Ship:
    text = (drydock / "ship.toml").read_text() + '\n[seats.captain.settings]\nstep_seconds = "0.05"\n'
    (drydock / "ship.toml").write_text(text)
    return shipmod.load(drydock)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def behind(elsewhere: Path) -> dict:
    return {str(item.relative_to(elsewhere)): (stat.S_IMODE(item.lstat().st_mode),
                                               item.read_bytes() if item.is_file() else None)
            for item in (elsewhere, *elsewhere.rglob('*'))}


def joined(seat: seatmod.Seat, tmux: Tmux) -> Path:
    """The seat's record, after one turn: the fake program writes none before it is given one."""
    wait_for(lambda: seat.harness.screen(tmux.capture(seat.pane().id)).box is not None)
    tmux.paste(seat.pane().id, "hello")
    tmux.keys(seat.pane().id, "Enter")
    record = wait_for(seat.record)
    wait_for(lambda: seat.harness.state(record).turns == 1)
    return record


def test_a_first_launch_makes_the_home_owner_only(ship: shipmod.Ship, tmux: Tmux):
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    captain.up()
    assert mode(captain.home) == 0o700
    assert (captain.home / "settings.json").is_file()


@pytest.mark.parametrize("widened", [0o755, 0o777])
@pytest.mark.parametrize("how", ["up", "fresh", "restart"])
def test_a_widened_home_is_mended_at_the_next_launch_and_the_launch_goes_ahead(ship: shipmod.Ship, tmux: Tmux,
                                                                              widened, how):
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    captain.up()
    if how == "restart":
        record = joined(captain, tmux)
    os.chmod(captain.home, widened)
    if how == "up":
        captain.down()
        proof = captain.up()
    elif how == "fresh":
        proof = captain.fresh()
    else:
        proof = captain.restart()
        assert " · resumed " in proof and captain.record() == record
    assert mode(captain.home) == 0o700
    assert captain.pane() is not None and "captain up in %" in proof


@pytest.mark.parametrize("resume", [True, False])
def test_a_link_in_place_of_the_home_is_refused_at_launch_and_nothing_behind_it_changes(ship: shipmod.Ship,
                                                                                        tmux: Tmux, tmp_path, resume):
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    captain.up()
    captain.down()
    elsewhere = tmp_path / "elsewhere"
    shutil.move(captain.home, elsewhere)
    os.chmod(elsewhere, 0o755)
    captain.home.symlink_to(elsewhere, target_is_directory=True)
    before = behind(elsewhere)
    with pytest.raises(ShipError, match="not an owned real directory"):
        captain.up(resume=resume)
    assert behind(elsewhere) == before
    assert captain.home.is_symlink() and captain.pane() is None


def test_a_restart_with_its_home_gone_is_refused_in_a_line_that_names_the_home(ship: shipmod.Ship, tmux: Tmux,
                                                                               tmp_path):
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    captain.up()
    joined(captain, tmux)
    pane = captain.pane().id
    captain.home.rename(tmp_path / "moved-home")
    with pytest.raises(resume_target.Unproved) as raised:
        captain.restart()
    message = str(raised.value)
    assert str(captain.home) in message and "absent" in message and "\n" not in message
    assert not captain.home.exists(), "nothing is made where the conversation's home should have been"
    assert captain.pane() is not None and captain.pane().id == pane, "the running launch is left as it was"
