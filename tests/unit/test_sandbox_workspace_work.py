"""sandbox.check_own_work under the workspace's work/: a hand's folder (the framework's work/hands/<name>, where the
framework's folder is the ship's workspace) walked as declared from the workspace down, never through a link."""

from __future__ import annotations

import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from quarterdeck import sandbox, setup, ship as shipmod


def ship_with_hand(tmp_path: Path, cwd: Path | None = None, workspace: str | None = None) -> shipmod.Ship:
    """An office whose workspace is <tmp>/framework and whose desk is declared as a hand: work/hands/hand there."""
    folder = tmp_path / 'office'
    folder.mkdir(exist_ok=True)
    (tmp_path / 'framework').mkdir(exist_ok=True)
    text = setup.template('office')[0]
    text = shipmod.edit(text, 'ship', 'workspace', workspace or str(tmp_path / 'framework'))
    text = shipmod.edit(text, 'seats.desk', 'sandbox', True)
    text = shipmod.edit(text, 'seats.desk', 'cwd', str(cwd or hand(tmp_path)))
    return shipmod._from_text(text, folder)


def hand(tmp_path: Path) -> Path:
    return tmp_path / 'framework' / 'work' / 'hands' / 'hand'


def check(ship: shipmod.Ship, **kwargs) -> None:
    sandbox.check_own_work(ship, 'desk', ship.seat('desk'), **kwargs)


def test_the_roots_are_the_ship_folder_then_the_workspace_as_declared(tmp_path):
    ship = ship_with_hand(tmp_path)
    assert sandbox.work_roots(ship) == (tmp_path / 'office', tmp_path / 'framework')
    assert sandbox.declared_work(ship, ship.seat('desk')) == (tmp_path / 'framework', hand(tmp_path))
    assert sandbox.own_work_folder(ship, ship.seat('desk')) is None, 'not the ship\'s own work/: qd init makes none'


def test_a_relative_workspace_is_kept_as_declared(tmp_path):
    (tmp_path / 'real').mkdir()
    (tmp_path / 'framework').symlink_to(tmp_path / 'real')
    ship = ship_with_hand(tmp_path, workspace='../framework')
    assert ship.workspace == tmp_path / 'real', 'loading resolves the workspace'
    assert sandbox.work_roots(ship)[1] == tmp_path / 'framework', 'the walk starts where the declaration says'


def test_a_workspace_that_is_the_ship_folder_is_one_root(tmp_path):
    ship = ship_with_hand(tmp_path, workspace='.')
    assert sandbox.work_roots(ship) == (tmp_path / 'office',)


def test_a_folder_under_both_roots_stays_the_ships_own(tmp_path):
    """A ship folder inside its workspace's work/: its desk's work/desk is its own, made at qd init as before."""
    folder = tmp_path / 'framework' / 'work' / 'office'
    folder.mkdir(parents=True)
    text = setup.template('office')[0]
    text = shipmod.edit(text, 'ship', 'workspace', str(tmp_path / 'framework'))
    text = shipmod.edit(text, 'seats.desk', 'sandbox', True)
    text = shipmod.edit(text, 'seats.desk', 'cwd', 'work/desk')
    ship = shipmod._from_text(text, folder)
    assert sandbox.own_work_folder(ship, ship.seat('desk')) == folder / 'work' / 'desk'
    check(ship, make=True)
    assert stat.S_IMODE((folder / 'work' / 'desk').lstat().st_mode) == 0o700


def test_a_workspace_replaced_after_load_is_taken_as_it_is(tmp_path):
    ship = replace(ship_with_hand(tmp_path), workspace=tmp_path / 'other')
    assert sandbox.work_roots(ship)[1] == tmp_path / 'other'


def test_a_real_folder_at_0755_is_refused_and_its_mode_untouched(tmp_path):
    """law: one-folder. A workspace may lie outside the project folder, so its folder's mode is never changed."""
    folder = hand(tmp_path)
    folder.mkdir(parents=True, mode=0o755)
    folder.chmod(0o755)
    (folder / 'copied-in').write_text('x')
    for make in (False, True):
        with pytest.raises(shipmod.ShipError, match=f'{folder} has mode 0755.*chmod 700 {folder}, then start again'):
            check(ship_with_hand(tmp_path), make=make)
    assert stat.S_IMODE(folder.lstat().st_mode) == 0o755 and (folder / 'copied-in').read_text() == 'x'


def test_the_steps_above_keep_their_modes(tmp_path):
    folder = hand(tmp_path)
    folder.mkdir(parents=True, mode=0o700)
    for step in (folder.parent, folder.parent.parent):
        step.chmod(0o755)
    check(ship_with_hand(tmp_path))
    assert all(stat.S_IMODE(step.lstat().st_mode) == 0o755 for step in (folder.parent, folder.parent.parent))


@pytest.mark.parametrize('where', ['work/hands/hand', 'work/hands', 'work'])
def test_a_link_at_any_step_is_refused_and_its_target_untouched(tmp_path, where):
    """The link stands for the step named; what it points at holds the rest of the path, a real folder at 0755."""
    rest = Path('work/hands/hand').relative_to(where)
    elsewhere = tmp_path / 'elsewhere'
    target = elsewhere / rest
    target.mkdir(parents=True, mode=0o755)
    target.chmod(0o755)
    link = tmp_path / 'framework' / where
    link.parent.mkdir(parents=True)
    link.symlink_to(elsewhere)
    ship = ship_with_hand(tmp_path)
    for make in (False, True):
        with pytest.raises(shipmod.ShipError, match=f'{link} is a link.*its workspace.*Remove the link'):
            check(ship, make=make)
    assert stat.S_IMODE(target.stat().st_mode) == 0o755 and not any(target.iterdir())


def test_a_missing_folder_is_refused_and_never_made_even_at_qd_init(tmp_path):
    ship = ship_with_hand(tmp_path)
    for make in (False, True):
        with pytest.raises(shipmod.ShipError, match='does not exist.*makes nothing in its workspace.*mkdir -p -m 700'):
            check(ship, make=make)
    assert not (tmp_path / 'framework' / 'work').exists()


def test_a_file_or_another_users_folder_is_refused(tmp_path, monkeypatch):
    folder = hand(tmp_path)
    folder.parent.mkdir(parents=True)
    folder.write_text('not a folder')
    with pytest.raises(shipmod.ShipError, match='not this user\'s own folder.*its workspace'):
        check(ship_with_hand(tmp_path))
    folder.unlink()
    folder.mkdir(mode=0o755)
    monkeypatch.setattr(os, 'getuid', lambda: folder.stat().st_uid + 1)
    with pytest.raises(shipmod.ShipError, match='not this user\'s own folder'):
        check(ship_with_hand(tmp_path))


def test_a_folder_the_ship_found_elsewhere_when_read_is_refused(tmp_path):
    hand(tmp_path).mkdir(parents=True, mode=0o700)
    ship = ship_with_hand(tmp_path)
    moved = replace(ship.seat('desk'), cwd=tmp_path / 'elsewhere')
    with pytest.raises(shipmod.ShipError, match='was found at'):
        sandbox.check_own_work(ship, 'desk', moved)


def test_an_absolute_cwd_reaching_the_workspaces_work_through_a_link_is_refused(tmp_path):
    hand(tmp_path).mkdir(parents=True, mode=0o700)
    alias = tmp_path / 'alias'
    alias.symlink_to(tmp_path / 'framework')
    ship = ship_with_hand(tmp_path, cwd=alias / 'work' / 'hands' / 'hand')
    with pytest.raises(shipmod.ShipError, match='its workspace\'s work folder, through a link.*Declare it as '
                       + str(hand(tmp_path))):
        check(ship)


def test_a_cwd_at_the_workspaces_work_itself_or_elsewhere_is_not_this_checks(tmp_path):
    work = tmp_path / 'framework' / 'work'
    work.mkdir(parents=True, mode=0o755)
    work.chmod(0o755)
    (tmp_path / 'project').mkdir()
    for cwd in (work, tmp_path / 'project'):
        ship = ship_with_hand(tmp_path, cwd=cwd)
        assert sandbox.declared_work(ship, ship.seat('desk')) is None
        check(ship)
    assert stat.S_IMODE(work.stat().st_mode) == 0o755
