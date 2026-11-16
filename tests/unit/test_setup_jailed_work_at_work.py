"""A jailed office seat whose cwd is the office's work/ itself is treated as one at a folder below it.

The declaration accepts such a seat (sandbox.plan lets a jailed seat work anywhere in its ship's own work/), and a jailed
seat there works in common with no other declared seat: plan refuses one whose folder holds another seat's. So qd init
makes work/ for it, private, as it makes work/desk, and the office's repository ignores work/, since nothing a jailed
seat writes there is the office's.
"""

from __future__ import annotations

import argparse
import os
import stat
from pathlib import Path

import pytest

from quarterdeck import sandbox, setup, ship as shipmod

OFFICE = """
[ship]
name = "office"
kind = "office"
operator = "operator"

[pools.local]
provider = "local"
endpoint = "http://127.0.0.1:8000/v1"

[sessions.office]
title = "Office"
human = true

[seats.desk]
session = "office"
role = "desk"
harness = "pi"
pool = "local"
model = "local-model"
cwd = "{cwd}"
"""

OTHER = """
[seats.second-synthetic]
session = "office"
role = "reviewer"
harness = "pi"
pool = "local"
model = "local-model"
cwd = "work/second-synthetic"
"""


def office(folder: Path, cwd: str, *, other: bool = False) -> shipmod.Ship:
    folder.mkdir(exist_ok=True)
    return shipmod._from_text(OFFICE.format(cwd=cwd) + (OTHER if other else ""), folder)


@pytest.mark.parametrize("cwd", ["work", "work/desk"])
def test_a_jailed_seat_at_work_or_below_it_is_listed_as_the_offices_jailed_work(tmp_path, cwd):
    current = office(tmp_path / "office", cwd)
    assert current.jailed(current.seat("desk"))
    assert setup.jailed_work_folders(current) == [tmp_path / "office" / cwd]


def test_an_unjailed_seat_at_work_is_not_listed(tmp_path):
    (tmp_path / "office").mkdir()
    text = shipmod.edit(OFFICE.format(cwd="work"), "seats.desk", "sandbox", False)
    current = shipmod._from_text(text, tmp_path / "office")
    assert not current.jailed(current.seat("desk")) and setup.jailed_work_folders(current) == []


def test_qd_init_makes_work_private_for_a_jailed_seat_at_work(tmp_path):
    current = office(tmp_path / "office", "work")
    setup.make_jailed_work(current)
    work = tmp_path / "office" / "work"
    info = work.lstat()
    assert stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700 and not any(work.iterdir())


def test_a_link_at_work_is_refused_for_a_jailed_seat_at_work_and_its_target_untouched(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o755)
    elsewhere.chmod(0o755)
    (tmp_path / "office").mkdir()
    (tmp_path / "office" / "work").symlink_to(elsewhere, target_is_directory=True)
    current = office(tmp_path / "office", "work")
    assert setup.jailed_work_folders(current) == [tmp_path / "office" / "work"], "listed as written, not its target"
    with pytest.raises(shipmod.ShipError, match="is a link"):
        setup.make_jailed_work(current)
    assert stat.S_IMODE(elsewhere.stat().st_mode) == 0o755 and not any(elsewhere.iterdir())


def test_a_jailed_seat_at_work_is_refused_where_another_seat_works_below_it(tmp_path):
    """What a seat at work/ could reach that one at work/<name> could not is every other seat's folder below it; the
    plan refuses that shape, so a seat at work/ works alone there."""
    current = office(tmp_path / "office", "work", other=True)
    with pytest.raises(shipmod.ShipError, match="holds seat second-synthetic's working directory"):
        sandbox.refuse_shared_workspace(current, "desk", tmp_path / "office" / "work")


def test_qd_init_ignores_work_for_an_office_whose_jailed_seat_is_at_work(tmp_path, monkeypatch):
    for name, value in {'GIT_AUTHOR_NAME': 'Setup Example', 'GIT_AUTHOR_EMAIL': 'setup@example.invalid',
                        'GIT_COMMITTER_NAME': 'Setup Example', 'GIT_COMMITTER_EMAIL': 'setup@example.invalid',
                        'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}.items():
        monkeypatch.setenv(name, value)
    for name in ('GIT_DIR', 'GIT_WORK_TREE'):
        monkeypatch.delenv(name, raising=False)
    project = tmp_path / "project"
    (project / "ships").mkdir(parents=True)

    class Operations(setup.Operations):
        def project_folder(self):
            return project

    setup.init_office(argparse.Namespace(ship=None), operations=Operations(), start=False,
                      declaration=OFFICE.format(cwd="work"))
    assert "/work/" in (project / "office" / ".gitignore").read_text().splitlines()
