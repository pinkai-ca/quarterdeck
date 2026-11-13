"""qd's default ship: the mark in ships/.default, the only-ship fallback, the refusals, and how it is said.

Every case runs in a synthetic project folder; nothing reads or writes the real project's ships/.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from quarterdeck import cli, doctor, install, ship as shipmod, standing
from quarterdeck.ship import ShipError

DRYDOCK = Path(__file__).resolve().parents[2] / "ships" / "drydock" / "ship.toml"


class Project(type(Path())):
    """The synthetic project folder, carrying what the stubbed handoff was given."""


@pytest.fixture
def project(tmp_path, monkeypatch):
    for name in ("QD_SHIP", "QD_SEAT", cli.HANDED):
        monkeypatch.delenv(name, raising=False)
    root = Project(tmp_path / "project")
    (root / "ships").mkdir(parents=True)
    monkeypatch.setattr(standing, "PROJECT_DIR", root)
    handed = []
    monkeypatch.setattr(install, "handoff", lambda folder, argv: handed.append(
        (Path(folder), list(argv), __import__("os").environ.get(cli.HANDED))))
    root.handed = handed
    return root


def ship(root: Path, name: str) -> Path:
    folder = root / "ships" / name
    folder.mkdir()
    shutil.copy(DRYDOCK, folder / "ship.toml")
    return folder


def templates(root: Path) -> None:
    for name in sorted(shipmod.TEMPLATES):
        ship(root, name)


def mark(root: Path, text: str) -> None:
    (root / "ships" / ".default").write_text(text)


# -- the choice -------------------------------------------------------------------------


def test_the_mark_chooses_the_ship(project):
    templates(project)
    ship(project, "endeavour")
    ship(project, "defiant")
    mark(project, "defiant\n")
    folder, name = shipmod.choose(None)
    assert folder == (project / "ships" / "defiant").resolve() and name == "defiant"
    assert shipmod.default_ship()[1] == "mark"
    assert shipmod.find() == folder


def test_the_mark_may_name_a_template(project):
    templates(project)
    mark(project, "drydock")
    assert shipmod.choose(None) == ((project / "ships" / "drydock").resolve(), "drydock")


def test_with_no_mark_the_only_ship_that_is_no_template_is_the_default(project):
    templates(project)
    ship(project, "endeavour")
    (project / "ships" / "notes").mkdir()  # a folder with no ship.toml is no ship
    (project / "ships" / ".hidden").mkdir()
    shutil.copy(DRYDOCK, project / "ships" / ".hidden" / "ship.toml")
    assert shipmod.candidates() == ["endeavour"]
    assert shipmod.default_ship() == ((project / "ships" / "endeavour").resolve(), "only ship")
    assert shipmod.choose(None)[1] == "endeavour"


def test_several_ships_and_no_mark_are_refused_by_name(project):
    templates(project)
    ship(project, "endeavour")
    ship(project, "defiant")
    with pytest.raises(ShipError) as refused:
        shipmod.choose(None)
    assert "defiant, endeavour" in str(refused.value) and "qd default <ship>" in str(refused.value)


@pytest.mark.parametrize("with_templates", [True, False])
def test_no_ship_at_all_is_refused_as_before(project, with_templates):
    if with_templates:
        templates(project)
    with pytest.raises(ShipError, match="no ship named: pass --ship <folder>"):
        shipmod.choose(None)


def test_no_ships_folder_is_refused(tmp_path, monkeypatch):
    for name in ("QD_SHIP", "QD_SEAT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(standing, "PROJECT_DIR", tmp_path / "nowhere")
    with pytest.raises(ShipError, match="no ship named"):
        shipmod.choose(None)


def test_a_mark_naming_a_missing_folder_is_refused_and_never_falls_back(project):
    ship(project, "endeavour")  # the one ship the fallback would choose
    mark(project, "voyager")
    with pytest.raises(ShipError) as refused:
        shipmod.choose(None)
    assert "voyager" in str(refused.value) and "missing" in str(refused.value)
    (project / "ships" / "voyager").mkdir()  # a folder with no ship.toml is as invalid
    with pytest.raises(ShipError, match="voyager"):
        shipmod.choose(None)


@pytest.mark.parametrize("text", ["", "\n", "../endeavour", "ships/endeavour", ".endeavour", "endeavour\n\n",
                                  " endeavour", "endeavour\ndefiant\n", "/abs/endeavour"])
def test_a_mark_that_is_no_single_folder_name_is_refused(project, text):
    ship(project, "endeavour")
    mark(project, text)
    with pytest.raises(ShipError, match="does not hold one ship folder name"):
        shipmod.choose(None)


def test_named_ship_and_qd_ship_win_over_the_mark(project, monkeypatch):
    ship(project, "endeavour")
    other = ship(project, "defiant")
    mark(project, "endeavour")
    assert shipmod.choose(str(other)) == (other.resolve(), None)
    monkeypatch.setenv("QD_SHIP", str(other))
    assert shipmod.choose(None) == (other.resolve(), None)


def test_a_seat_never_reads_the_mark(project, monkeypatch):
    ship(project, "endeavour")
    mark(project, "endeavour")
    monkeypatch.setenv("QD_SEAT", "worker-1")
    with pytest.raises(ShipError, match="a seat runs with its own QD_SHIP"):
        shipmod.choose(None)
    mark(project, "../broken")  # not even a broken mark is read
    with pytest.raises(ShipError, match="a seat runs with its own QD_SHIP"):
        shipmod.choose(None)
    own = ship(project, "defiant")
    monkeypatch.setenv("QD_SHIP", str(own))
    assert shipmod.choose(None) == (own.resolve(), None)


def test_mark_default_writes_only_the_mark(project):
    ship(project, "endeavour")
    before = sorted(p.relative_to(project) for p in project.rglob("*"))
    assert shipmod.mark_default("endeavour") == (project / "ships" / "endeavour").resolve()
    assert (project / "ships" / ".default").read_text() == "endeavour\n"
    after = sorted(p.relative_to(project) for p in project.rglob("*"))
    assert set(after) - set(before) == {Path("ships/.default")}  # law: one-folder
    for bad in ("voyager", "../endeavour", ".default", ""):
        with pytest.raises(ShipError):
            shipmod.mark_default(bad)
    assert (project / "ships" / ".default").read_text() == "endeavour\n"


# -- the command line -------------------------------------------------------------------


def run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_a_verb_on_the_default_ship_says_so_and_hands_over_the_same_folder(project, capsys):
    folder = ship(project, "endeavour").resolve()
    code, out, _ = run(capsys, "ledger")
    assert code == 0
    assert out.splitlines()[0].endswith(" (default ship: endeavour)")
    assert project.handed == [(folder, ["--ship", str(folder), "ledger"], str(folder))]
    assert cli.HANDED not in __import__("os").environ, "nothing launched afterwards inherits it"


def test_the_json_reading_carries_the_default_ship_as_a_field(project, capsys):
    ship(project, "endeavour")
    code, out, _ = run(capsys, "--json", "ledger")
    data = json.loads(out)
    assert code == 0 and data["default_ship"] == "endeavour"


def test_a_named_ship_is_not_called_the_default(project, capsys, monkeypatch):
    folder = ship(project, "endeavour")
    mark(project, "endeavour")
    for argv in (("--ship", str(folder), "ledger"), ("--json", "--ship", str(folder), "ledger")):
        code, out, _ = run(capsys, *argv)
        assert code == 0 and "default ship" not in out and "default_ship" not in out
    monkeypatch.setenv("QD_SHIP", str(folder))
    code, out, _ = run(capsys, "ledger")
    assert code == 0 and "default ship" not in out
    assert all(handed[2] is None for handed in project.handed)


def test_the_ships_own_copy_names_the_default_it_was_handed(project, capsys, monkeypatch):
    folder = ship(project, "endeavour").resolve()
    other = ship(project, "defiant").resolve()
    monkeypatch.setenv(cli.HANDED, str(folder))
    code, out, _ = run(capsys, "--ship", str(folder), "ledger")
    assert code == 0 and out.splitlines()[0].endswith("(default ship: endeavour)")
    assert cli.HANDED not in __import__("os").environ
    monkeypatch.setenv(cli.HANDED, str(other))  # handed a different folder: never said
    code, out, _ = run(capsys, "--ship", str(folder), "ledger")
    assert code == 0 and "default ship" not in out


def test_a_refusal_on_the_default_ship_names_it(project, capsys):
    ship(project, "endeavour")
    code, _, err = run(capsys, "brief", "no-such-seat")
    assert code == 2 and err.rstrip().endswith("(default ship: endeavour)")


def test_several_ships_refuse_a_plain_verb_by_name(project, capsys):
    ship(project, "endeavour")
    ship(project, "defiant")
    code, _, err = run(capsys, "ledger")
    assert code == 2 and "defiant, endeavour" in err and "qd default <ship>" in err
    assert project.handed == []


def test_a_seat_without_its_own_ship_is_refused_and_never_handed_the_default(project, capsys, monkeypatch):
    ship(project, "endeavour")
    mark(project, "endeavour")
    monkeypatch.setenv("QD_SEAT", "worker-1")
    code, _, err = run(capsys, "ledger")
    assert code == 2 and "a seat runs with its own QD_SHIP" in err and "default ship:" not in err
    assert project.handed == []


def test_verbs_that_act_on_no_ship_never_take_the_default(project, capsys):
    ship(project, "endeavour")
    code, out, _ = run(capsys, "laws")
    assert code == 0 and "(default ship:" not in out and project.handed == []
    parser = cli.build_parser()
    for argv in (["laws"], ["drill"], ["default"], ["controls", "--harness", "claude"]):
        assert not cli.needs_ship(parser.parse_args(argv)), argv
    for argv in (["status"], ["up"], ["sync"], ["doctor"], ["check", "claude"], ["post", "order", "x"]):
        assert cli.needs_ship(parser.parse_args(argv)), argv


# -- qd default -------------------------------------------------------------------------


def test_qd_default_prints_the_default_and_how_it_was_chosen(project, capsys):
    ship(project, "endeavour")
    code, out, _ = run(capsys, "default")
    assert code == 0 and out.startswith("default ship: endeavour · the only ship under ships/, with no mark")
    ship(project, "defiant")
    code, out, _ = run(capsys, "default")
    assert code == 1 and out.startswith("no default ship · ") and "defiant, endeavour" in out
    code, out, _ = run(capsys, "default", "defiant")
    assert code == 0 and out.startswith("default ship: defiant · now marked in ships/.default")
    code, out, _ = run(capsys, "--json", "default")
    data = json.loads(out)
    assert code == 0 and (data["ship"], data["how"], data["marked"]) == ("defiant", "mark", False)
    assert project.handed == []


def test_qd_default_takes_the_folder_by_path_too_and_refuses_a_missing_one(project, capsys):
    ship(project, "endeavour")
    code, out, _ = run(capsys, "default", str(project / "ships" / "endeavour"))
    assert code == 0 and (project / "ships" / ".default").read_text() == "endeavour\n"
    code, _, err = run(capsys, "default", "voyager")
    assert code == 2 and "voyager" in err
    assert (project / "ships" / ".default").read_text() == "endeavour\n"


def test_qd_default_says_when_the_shell_does_not_use_it(project, capsys, monkeypatch):
    folder = ship(project, "endeavour")
    monkeypatch.setenv("QD_SHIP", str(folder))
    _, out, _ = run(capsys, "default")
    assert "QD_SHIP is set in this shell and wins over the default ship" in out
    monkeypatch.setenv("QD_SEAT", "worker-1")
    _, out, _ = run(capsys, "default")
    assert "this shell is a seat" in out


# -- doctor -----------------------------------------------------------------------------


def test_doctor_warns_of_qd_ship_in_a_shell_that_is_no_seat(project, monkeypatch):
    folder = ship(project, "endeavour")
    assert doctor.shell_check() is None
    monkeypatch.setenv("QD_SHIP", str(folder))
    warning = doctor.shell_check()
    assert warning.passed, "a warning, never a failure"
    assert warning.detail.startswith("warning: QD_SHIP is set in a shell that is no seat")
    assert "default ship replaces it" in warning.detail and "start-up file" in warning.detail
    monkeypatch.setenv("QD_SEAT", "worker-1")
    assert doctor.shell_check() is None


def test_doctor_lists_the_warning_among_its_checks(project, monkeypatch):
    folder = ship(project, "endeavour")
    monkeypatch.setenv("QD_SHIP", str(folder))
    found = doctor.checks(shipmod.load(folder))
    assert [c.name for c in found][:2] == ["ship file", "QD_SHIP in this shell"]


def test_git_never_tracks_the_mark():
    """The mark lies inside the project folder, where the framework's repository ignores it."""  # law: one-folder
    root = Path(__file__).resolve().parents[2]
    done = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", "--no-index", "ships/.default"])
    assert done.returncode == 0, "ships/.default must be ignored by the framework repository"
