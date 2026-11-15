"""sandbox.check_own_work: a ship's common workspace at its own work/ (the forum's shape), walked as declared."""

from __future__ import annotations

import stat
from dataclasses import replace
from pathlib import Path

import pytest

from quarterdeck import sandbox, ship as shipmod

DECLARATION = """
[ship]
name = "forum"
workspace = "{workspace}"

[pools.local]
provider = "local"
endpoint = "http://127.0.0.1:8000/v1"

[sessions.forum]
title = "Forum"

[seats.reviewer]
session = "forum"
role = "reviewer"
harness = "pi"
pool = "local"
model = "local-model"
"""


def forum(folder: Path, workspace: str = "work", cwd: str | None = None) -> shipmod.Ship:
    folder.mkdir(exist_ok=True)
    text = DECLARATION.format(workspace=workspace)
    if cwd is not None:
        text = shipmod.edit(text, "seats.reviewer", "cwd", cwd)
    return shipmod._from_text(text, folder)


def check(ship: shipmod.Ship, **kwargs) -> None:
    sandbox.check_own_work(ship, "reviewer", ship.seat("reviewer"), **kwargs)


def test_the_seat_is_jailed_and_works_in_the_ships_work(tmp_path):
    (tmp_path / "forum" / "work").mkdir(parents=True)
    ship = forum(tmp_path / "forum")
    spec = ship.seat("reviewer")
    assert ship.jailed(spec) and spec.declared_cwd is None
    assert sandbox.shared_work(ship, spec) == tmp_path / "forum" / "work"


def test_a_real_work_folder_passes_and_a_loose_mode_is_set_private(tmp_path):
    work = tmp_path / "forum" / "work"
    work.mkdir(parents=True, mode=0o755)
    work.chmod(0o755)
    (work / "kept").write_text("x")
    check(forum(tmp_path / "forum"))
    assert stat.S_IMODE(work.lstat().st_mode) == 0o700 and (work / "kept").read_text() == "x"


@pytest.mark.parametrize("make", [False, True])
def test_a_link_at_work_is_refused_and_its_target_untouched(tmp_path, make):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o755)
    elsewhere.chmod(0o755)
    (tmp_path / "forum").mkdir()
    (tmp_path / "forum" / "work").symlink_to(elsewhere)
    ship = forum(tmp_path / "forum")
    with pytest.raises(shipmod.ShipError, match=f"{tmp_path / 'forum' / 'work'} is a link.*Remove the link"):
        check(ship, make=make)
    assert stat.S_IMODE(elsewhere.stat().st_mode) == 0o755 and not any(elsewhere.iterdir())


def test_a_link_at_a_step_above_a_workspace_below_work_is_refused(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "project").mkdir(parents=True)
    (tmp_path / "forum").mkdir()
    (tmp_path / "forum" / "work").symlink_to(elsewhere)
    with pytest.raises(shipmod.ShipError, match=f"{tmp_path / 'forum' / 'work'} is a link"):
        check(forum(tmp_path / "forum", workspace="work/project"))


def test_a_file_or_another_users_folder_at_work_is_refused(tmp_path, monkeypatch):
    (tmp_path / "forum").mkdir()
    (tmp_path / "forum" / "work").write_text("not a folder")
    with pytest.raises(shipmod.ShipError, match="is not this user's own folder.*Remove it"):
        check(forum(tmp_path / "forum"))
    (tmp_path / "forum" / "work").unlink()
    (tmp_path / "forum" / "work").mkdir(mode=0o700)
    ship = forum(tmp_path / "forum")
    monkeypatch.setattr(sandbox.os, "getuid", lambda: (tmp_path / "forum" / "work").lstat().st_uid + 1)
    with pytest.raises(shipmod.ShipError, match="is not this user's own folder"):
        check(ship)


def test_a_missing_work_folder_refuses_a_launch_and_makes_nothing(tmp_path):
    ship = forum(tmp_path / "forum")
    with pytest.raises(shipmod.ShipError, match="does not exist"):
        check(ship)
    assert not (tmp_path / "forum" / "work").exists()


def test_a_work_folder_the_ship_found_elsewhere_when_read_is_refused(tmp_path):
    """A link at load, since replaced by a real folder: the launch would otherwise bind the old target."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "forum").mkdir()
    work = tmp_path / "forum" / "work"
    work.symlink_to(elsewhere)
    ship = forum(tmp_path / "forum")
    assert ship.workspace == elsewhere, "loading resolves the workspace"
    work.unlink()
    work.mkdir(mode=0o700)
    with pytest.raises(shipmod.ShipError, match=f"found at {elsewhere} when the ship was read"):
        check(ship)


def test_a_workspace_through_a_link_to_work_is_refused(tmp_path):
    work = tmp_path / "forum" / "work"
    work.mkdir(parents=True, mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(work)
    with pytest.raises(shipmod.ShipError, match=f"workspace {alias}, reaches {work}.*Declare the workspace as {work}"):
        check(forum(tmp_path / "forum", workspace=str(alias)))


def test_a_cwd_that_is_work_itself_is_walked(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "forum").mkdir()
    (tmp_path / "forum" / "work").symlink_to(elsewhere)
    with pytest.raises(shipmod.ShipError, match="is a link"):
        check(forum(tmp_path / "forum", workspace=".", cwd="work"))


def test_a_cwd_elsewhere_reaching_work_itself_through_a_link_is_refused(tmp_path):
    work = tmp_path / "forum" / "work"
    work.mkdir(parents=True, mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(work)
    with pytest.raises(shipmod.ShipError, match="through a link.*Declare it as"):
        check(forum(tmp_path / "forum", workspace=".", cwd=str(alias)))


def test_a_workspace_elsewhere_is_not_walked(tmp_path):
    project = tmp_path / "project"
    project.mkdir(mode=0o755)
    project.chmod(0o755)
    ship = forum(tmp_path / "forum", workspace=str(project))
    assert sandbox.shared_work(ship, ship.seat("reviewer")) is None
    check(ship)
    assert stat.S_IMODE(project.lstat().st_mode) == 0o755


def test_a_workspace_replaced_after_load_is_taken_as_it_is(tmp_path):
    work = tmp_path / "forum" / "work"
    work.mkdir(parents=True, mode=0o755)
    ship = forum(tmp_path / "forum", workspace=".")
    ship = replace(ship, workspace=work)
    assert sandbox.shared_work(ship, ship.seat("reviewer")) == work
