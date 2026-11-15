"""A jailed seat's declared cwd under its workspace's work/ is walked at every launch, as one under its ship's own.

The hands work in folders of the framework's work/hands/<name>, where the framework's folder is the ship's workspace,
outside the ship's own work/. The jail binds a seat's working folder writable, so a link planted on the way there
would be bound unseen. Each launch walks the cwd as the declaration writes it, from the workspace down, never through
a link, and refuses before anything of the launch is made.

Real setup, real tmux and real bubblewrap, on the office-desk gates' newcomer: its desk, declared as a hand is, stands
in for a hand.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from quarterdeck import ship as shipmod

from .test_jailed_cwd_at_launch import assert_nothing_launched, desk_of, launch_file, qd, set_up
from .test_sandbox import namespaces_allowed
from .test_setup_jailed_office_desk import bound, jailed, newcomer  # noqa: F401  the newcomer fixture

pytestmark = pytest.mark.skipif(not namespaces_allowed(), reason="bubblewrap is not installed, or cannot make a namespace here (inside a jail)")


def hand_like(newcomer, capsys) -> tuple[Path, Path]:
    """The office set up, its desk down and declared as a hand: cwd = <workspace>/work/hands/bert, a real folder."""
    office, _ = set_up(newcomer, capsys)
    workspace = newcomer.workspace
    folder = workspace / 'work' / 'hands' / 'bert'
    folder.mkdir(parents=True, mode=0o700)
    text = (office / 'ship.toml').read_text()
    text = shipmod.edit(text, 'ship', 'workspace', str(workspace))
    text = shipmod.edit(text, 'seats.desk', 'cwd', str(folder))
    (office / 'ship.toml').write_text(text)
    assert shipmod.load(office).seat('desk').declared_cwd == folder
    return office, folder


def test_a_link_planted_at_the_hands_folder_refuses_the_next_up_and_launches_nothing(newcomer, capsys):
    office, folder = hand_like(newcomer, capsys)
    before = launch_file(office)
    elsewhere = newcomer.tmp / 'elsewhere-bert'
    elsewhere.mkdir(mode=0o755)
    elsewhere.chmod(0o755)
    folder.rmdir()
    folder.symlink_to(elsewhere, target_is_directory=True)
    code, said = qd(capsys, office, 'up', 'desk')
    assert code != 0
    assert f'{folder} is a link' in said and 'Remove the link' in said, said[-2000:]
    assert_nothing_launched(office, newcomer, before)
    assert stat.S_IMODE(elsewhere.stat().st_mode) == 0o755 and not any(elsewhere.iterdir()), 'its target untouched'


def test_a_link_planted_at_work_hands_refuses_the_next_up_and_launches_nothing(newcomer, capsys):
    """Each step from the workspace down is walked, not only the last."""
    office, folder = hand_like(newcomer, capsys)
    before = launch_file(office)
    elsewhere = newcomer.tmp / 'elsewhere-hands'
    (elsewhere / 'bert').mkdir(parents=True, mode=0o700)
    folder.rmdir()
    folder.parent.rmdir()
    folder.parent.symlink_to(elsewhere, target_is_directory=True)
    code, said = qd(capsys, office, 'up', 'desk')
    assert code != 0 and f'{folder.parent} is a link' in said, said[-2000:]
    assert_nothing_launched(office, newcomer, before)


def test_a_real_hands_folder_at_0700_launches(newcomer, capsys):
    office, folder = hand_like(newcomer, capsys)
    (folder / 'copied-in.txt').write_text('synthetic')
    code, said = qd(capsys, office, 'up', 'desk')
    assert code == 0, said[-3000:]
    assert stat.S_IMODE(folder.lstat().st_mode) == 0o700 and not folder.is_symlink()
    assert (folder / 'copied-in.txt').read_text() == 'synthetic', 'what was copied in is kept'
    launched = desk_of(office, newcomer).launched()
    assert jailed(launched.argv) and bound(launched.argv, folder)


def test_a_hands_folder_at_0755_refuses_the_launch_and_its_mode_is_untouched(newcomer, capsys):
    """law: one-folder. The workspace may lie outside the project folder, so qd changes no mode there."""
    office, folder = hand_like(newcomer, capsys)
    before = launch_file(office)
    folder.chmod(0o755)
    code, said = qd(capsys, office, 'up', 'desk')
    assert code != 0 and f'chmod 700 {folder}, then start again' in said, said[-2000:]
    assert stat.S_IMODE(folder.lstat().st_mode) == 0o755, 'untouched'
    assert_nothing_launched(office, newcomer, before)


def test_a_missing_hands_folder_refuses_the_launch_and_makes_nothing(newcomer, capsys):
    office, folder = hand_like(newcomer, capsys)
    before = launch_file(office)
    folder.rmdir()
    code, said = qd(capsys, office, 'up', 'desk')
    assert code != 0 and f'{folder} does not exist' in said and 'mkdir -p -m 700' in said, said[-2000:]
    assert not folder.exists(), 'a launch makes nothing in the workspace'
    assert_nothing_launched(office, newcomer, before)


def test_an_absolute_cwd_through_a_link_to_the_workspace_fails_closed(newcomer, capsys):
    office, folder = hand_like(newcomer, capsys)
    before = launch_file(office)
    alias = newcomer.tmp / 'workspace-alias'
    alias.symlink_to(newcomer.workspace, target_is_directory=True)
    text = shipmod.edit((office / 'ship.toml').read_text(), 'seats.desk', 'cwd', str(alias / 'work' / 'hands' / 'bert'))
    (office / 'ship.toml').write_text(text)
    code, said = qd(capsys, office, 'up', 'desk')
    assert code != 0 and 'through a link' in said and f'Declare it as {folder}' in said, said[-3000:]
    assert_nothing_launched(office, newcomer, before)
    assert os.path.realpath(folder) == str(folder), 'the hand\'s folder is unchanged'
