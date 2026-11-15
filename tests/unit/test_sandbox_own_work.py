"""sandbox.check_own_work: a jailed seat's own folder under its ship's work/, walked as declared, never through a link."""

from __future__ import annotations

import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from quarterdeck import sandbox, setup, ship as shipmod


def office(folder: Path, cwd: str = setup.JAILED_DESK_WORK) -> shipmod.Ship:
    folder.mkdir(exist_ok=True)
    text = setup.template('office')[0]
    text = shipmod.edit(text, 'seats.desk', 'sandbox', True)
    text = shipmod.edit(text, 'seats.desk', 'cwd', cwd)
    return shipmod._from_text(text, folder)


def check(ship: shipmod.Ship, **kwargs) -> None:
    sandbox.check_own_work(ship, 'desk', ship.seat('desk'), **kwargs)


def test_the_declared_cwd_is_kept_as_written_while_cwd_is_resolved(tmp_path):
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    (tmp_path / 'office' / 'work').mkdir(parents=True)
    (tmp_path / 'office' / 'work' / 'desk').symlink_to(elsewhere)
    desk = office(tmp_path / 'office').seat('desk')
    assert desk.cwd == elsewhere, 'loading resolves the cwd'
    assert desk.declared_cwd == tmp_path / 'office' / 'work' / 'desk', 'the declaration\'s own path is kept'
    assert desk == replace(desk, declared_cwd=None), 'a seat compares as before'


def test_qd_init_makes_the_folder_empty_and_private(tmp_path):
    ship = office(tmp_path / 'office')
    check(ship, make=True)
    work = tmp_path / 'office' / 'work' / 'desk'
    assert work.is_dir() and not any(work.iterdir()) and stat.S_IMODE(work.lstat().st_mode) == 0o700


def test_a_launch_refuses_a_missing_folder_and_makes_nothing(tmp_path):
    ship = office(tmp_path / 'office')
    with pytest.raises(shipmod.ShipError, match='does not exist.*qd init'):
        check(ship)
    assert not (tmp_path / 'office' / 'work').exists()


def test_a_loose_mode_is_set_private_and_what_is_in_it_kept(tmp_path):
    work = tmp_path / 'office' / 'work' / 'desk'
    work.mkdir(parents=True, mode=0o755)
    work.chmod(0o755)
    (work / 'kept').write_text('x')
    check(office(tmp_path / 'office'))
    assert stat.S_IMODE(work.lstat().st_mode) == 0o700 and (work / 'kept').read_text() == 'x'


@pytest.mark.parametrize('where', ['work/desk', 'work'])
def test_a_link_at_any_step_is_refused_and_its_target_untouched(tmp_path, where):
    elsewhere = tmp_path / 'elsewhere'
    (elsewhere / 'desk').mkdir(parents=True, mode=0o755)
    for folder in (elsewhere, elsewhere / 'desk'):
        folder.chmod(0o755)
    root = tmp_path / 'office'
    if where == 'work':
        root.mkdir()
        (root / 'work').symlink_to(elsewhere)
    else:
        (root / 'work').mkdir(parents=True)
        (root / 'work' / 'desk').symlink_to(elsewhere / 'desk')
    ship = office(root)
    for make in (False, True):
        with pytest.raises(shipmod.ShipError, match=f'{root / where} is a link'):
            check(ship, make=make)
    assert stat.S_IMODE((elsewhere / 'desk').stat().st_mode) == 0o755 and not any((elsewhere / 'desk').iterdir())


def test_a_file_or_another_users_folder_is_refused(tmp_path, monkeypatch):
    work = tmp_path / 'office' / 'work' / 'desk'
    work.parent.mkdir(parents=True)
    work.write_text('not a folder')
    with pytest.raises(shipmod.ShipError, match='not this user\'s own folder'):
        check(office(tmp_path / 'office'))
    work.unlink()
    work.mkdir(mode=0o755)
    monkeypatch.setattr(os, 'getuid', lambda: work.stat().st_uid + 1)
    with pytest.raises(shipmod.ShipError, match='not this user\'s own folder'):
        check(office(tmp_path / 'office'))


def test_a_folder_the_ship_found_elsewhere_when_read_is_refused(tmp_path):
    """A link present when the ship was read and since replaced by a real folder: the launch would use the old target."""
    work = tmp_path / 'office' / 'work' / 'desk'
    work.mkdir(parents=True, mode=0o700)
    ship = office(tmp_path / 'office')
    moved = replace(ship.seat('desk'), cwd=tmp_path / 'elsewhere')
    with pytest.raises(shipmod.ShipError, match='was found at'):
        sandbox.check_own_work(ship, 'desk', moved)


def test_an_absolute_cwd_reaching_the_work_folder_through_a_link_is_refused(tmp_path):
    root = tmp_path / 'office'
    (root / 'work' / 'desk').mkdir(parents=True, mode=0o700)
    alias = tmp_path / 'alias'
    alias.symlink_to(root)
    with pytest.raises(shipmod.ShipError, match='through a link.*Declare it as ' + str(root / 'work' / 'desk')):
        check(office(root, str(alias / 'work' / 'desk')))


def test_a_cwd_outside_work_is_not_this_checks_and_work_itself_is_walked_as_shared(tmp_path):
    """Neither is a folder of the seat's own for setup; work/ itself is the ship's shared work folder, walked and made
    private before a jailed launch binds it (test_sandbox_shared_work.py), and a folder elsewhere is left alone."""
    root = tmp_path / 'office'
    (root / 'work').mkdir(parents=True, mode=0o755)
    (root / 'work').chmod(0o755)
    (tmp_path / 'project').mkdir(mode=0o755)
    (tmp_path / 'project').chmod(0o755)
    ship = office(root, str(tmp_path / 'project'))
    assert sandbox.own_work_folder(ship, ship.seat('desk')) is None
    check(ship)
    assert stat.S_IMODE((tmp_path / 'project').stat().st_mode) == 0o755
    assert stat.S_IMODE((root / 'work').stat().st_mode) == 0o755
    ship = office(root, 'work')
    assert sandbox.own_work_folder(ship, ship.seat('desk')) is None
    check(ship)
    assert stat.S_IMODE((root / 'work').stat().st_mode) == 0o700
