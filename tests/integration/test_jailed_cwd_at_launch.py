"""A jailed seat's own working folder under its ship's work/ is checked again at every launch, as qd init checks it.

qd init makes the office desk's work/desk and checks it, but the folder can change after: a link planted there, or a
mode left loose. The jail binds the seat's working folder writable, so each launch walks it again as the declaration
writes it, never through a link, and refuses before anything of the launch is made. An unjailed seat is untouched.

Real setup, real tmux and real bubblewrap, on the office-desk gates' newcomer: a desk on pi with a stand-in program.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from quarterdeck import cli, ship as shipmod
from quarterdeck.seat import Seat

from .test_sandbox import namespaces_allowed
from .test_setup_jailed_office_desk import answers, bound, jailed, newcomer  # noqa: F401  the newcomer fixture

pytestmark = pytest.mark.skipif(not namespaces_allowed(), reason="bubblewrap is not installed, or cannot make a namespace here (inside a jail)")


def set_up(newcomer, capsys) -> tuple[Path, Path]:
    """Setup with a desk on pi, then the desk brought down: the office and the desk's own folder."""
    code = cli.main(['setup', '--answers', str(answers(newcomer, 'pi'))])
    out = capsys.readouterr()
    assert code == 0, out.err[-3000:] + out.out[-3000:]
    office = newcomer.project / 'office'
    assert qd(capsys, office, 'down', 'desk')[0] == 0
    return office, office / 'work' / 'desk'


def qd(capsys, office: Path, *argv: str) -> tuple[int, str]:
    code = cli.main(['--ship', str(office), *argv])
    out = capsys.readouterr()
    return code, out.err + out.out


def desk_of(office: Path, newcomer) -> Seat:
    ship = shipmod.load(office)
    return Seat(ship, ship.seat('desk'), newcomer.tmux)


def launch_file(office: Path) -> str | None:
    """The desk's launch record; qd down removes it, so a refused launch leaves none."""
    path = office / 'runtime' / 'seats' / 'desk' / 'launch.json'
    return path.read_text() if path.exists() else None


def assert_nothing_launched(office: Path, newcomer, before: str | None) -> None:
    assert before is None and launch_file(office) is None, 'no launch was recorded'
    pane = desk_of(office, newcomer).pane()
    assert pane is None or pane.dead, 'no pane runs the desk'


def test_a_link_planted_at_the_desks_folder_after_init_refuses_the_next_up_and_launches_nothing(newcomer, capsys):
    office, work = set_up(newcomer, capsys)
    before = launch_file(office)
    elsewhere = newcomer.tmp / 'elsewhere'
    elsewhere.mkdir(mode=0o755)
    elsewhere.chmod(0o755)
    work.rmdir()
    work.symlink_to(elsewhere, target_is_directory=True)
    code, said = qd(capsys, office, 'up', 'desk')
    assert code != 0
    assert f'{work} is a link' in said and 'Remove the link' in said, said[-2000:]
    assert_nothing_launched(office, newcomer, before)
    assert stat.S_IMODE(elsewhere.stat().st_mode) == 0o755 and not any(elsewhere.iterdir()), 'its target untouched'


def test_a_link_at_the_offices_work_folder_refuses_the_desk(newcomer, capsys):
    """Each step from the ship folder down is walked, not only the last."""
    office, work = set_up(newcomer, capsys)
    before = launch_file(office)
    elsewhere = newcomer.tmp / 'elsewhere-work'
    (elsewhere / 'desk').mkdir(parents=True, mode=0o700)
    work.rmdir()
    work.parent.rmdir()
    work.parent.symlink_to(elsewhere, target_is_directory=True)
    code, said = qd(capsys, office, 'up', 'desk')
    assert code != 0 and f'{work.parent} is a link' in said, said[-2000:]
    assert_nothing_launched(office, newcomer, before)


def test_a_desks_folder_left_at_0755_is_launched_at_0700(newcomer, capsys):
    office, work = set_up(newcomer, capsys)
    work.chmod(0o755)
    (work / 'left-by-the-desk.txt').write_text('synthetic')
    code, said = qd(capsys, office, 'up', 'desk')
    assert code == 0, said[-3000:]
    assert stat.S_IMODE(work.lstat().st_mode) == 0o700 and not work.is_symlink()
    assert (work / 'left-by-the-desk.txt').read_text() == 'synthetic', 'what the desk left is kept'
    launched = desk_of(office, newcomer).launched()
    assert jailed(launched.argv) and bound(launched.argv, work)


def test_a_missing_desks_folder_refuses_the_launch_and_qd_init_makes_it(newcomer, capsys):
    office, work = set_up(newcomer, capsys)
    before = launch_file(office)
    work.rmdir()
    code, said = qd(capsys, office, 'up', 'desk')
    assert code != 0 and f'{work} does not exist' in said and 'qd init' in said, said[-2000:]
    assert not work.exists(), 'a launch makes nothing outside its folder'
    assert_nothing_launched(office, newcomer, before)


def test_an_unjailed_seat_is_untouched(newcomer, capsys):
    """A desk the ship runs open (sandbox = false) is not checked: its folder keeps its mode."""
    office, work = set_up(newcomer, capsys)
    text = shipmod.edit((office / 'ship.toml').read_text(), 'seats.desk', 'sandbox', False)
    (office / 'ship.toml').write_text(text)
    work.chmod(0o755)
    code, said = qd(capsys, office, 'up', 'desk')
    assert code == 0, said[-3000:]
    assert stat.S_IMODE(work.lstat().st_mode) == 0o755, 'untouched'
    assert not jailed(desk_of(office, newcomer).launched().argv)


def test_an_absolute_cwd_through_a_link_to_the_office_keeps_failing_closed(newcomer, capsys):
    office, work = set_up(newcomer, capsys)
    before = launch_file(office)
    alias = newcomer.tmp / 'office-alias'
    alias.symlink_to(office, target_is_directory=True)
    text = shipmod.edit((office / 'ship.toml').read_text(), 'seats.desk', 'cwd', str(alias / 'work' / 'desk'))
    (office / 'ship.toml').write_text(text)
    code, said = qd(capsys, office, 'up', 'desk')
    assert code != 0, said[-3000:]
    assert_nothing_launched(office, newcomer, before)
    assert os.path.realpath(work) == str(work), 'the desk\'s own folder is unchanged'
