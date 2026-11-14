"""Office creation and the first setup step, with synthetic launch operations."""
import argparse
import os
from pathlib import Path

import pytest

from quarterdeck import cli, install, setup, ship, standing


@pytest.fixture
def office_project(tmp_path, monkeypatch):
    monkeypatch.setattr(setup.Operations, "project_folder", lambda _: tmp_path)
    for name, value in {
        "GIT_AUTHOR_NAME": "Office Example", "GIT_AUTHOR_EMAIL": "office@example.invalid",
        "GIT_COMMITTER_NAME": "Office Example", "GIT_COMMITTER_EMAIL": "office@example.invalid",
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("GIT_DIR", raising=False)
    monkeypatch.delenv("GIT_WORK_TREE", raising=False)
    calls = []
    monkeypatch.setattr(setup.Operations, "up_desk", lambda _, office: calls.append(office) or ["synthetic desk up", "synthetic service started"])
    monkeypatch.setattr(setup.Operations, "access", lambda _, office: "example desk access")
    return tmp_path, calls


def initialize():
    return setup.init_office(argparse.Namespace(ship=None))


@pytest.mark.parametrize('empty_office', [False, True])
def test_init_copies_and_commits_the_office_then_starts_only_its_desk(office_project, empty_office):
    # law: one-folder
    project, calls = office_project
    if empty_office:
        (project / 'office').mkdir()
    template = standing.project_dir() / "ships" / "office"
    before = {p.relative_to(template): p.read_bytes() for p in template.rglob("*") if p.is_file()}
    lines = initialize()
    folder = project / "office"
    current = ship.load(folder)
    assert current.kind == "office" and list(current.seats) == ["desk"]
    assert calls == [current]
    assert "synthetic service started" in lines
    assert any("Desk access:" in line for line in lines)
    assert ship._git(folder, "log", "-1", "--format=%s") == "qd init: declare office"
    assert ship._git(folder, "status", "--porcelain") == ""
    assert set(ship._git(folder, "ls-files").splitlines()) == {
        "ship.toml", "orders.md", "README.md", "roles/desk.md", ".gitignore"}
    for path, content in before.items():
        assert (folder / path).read_bytes() == content
        assert (template / path).read_bytes() == content
    assert not (project / "ships").exists()
    assert (folder / ".gitignore").read_text() == "/runtime/\n"


def test_empty_fleet_mountpoints_are_not_ships_or_peers(office_project):
    from quarterdeck import fleet
    project, calls = office_project
    office = project/'office'
    ships = project/'ships'
    office.mkdir()
    ships.mkdir()
    assert ship.candidates(project) == []
    setup.refuse_embedded_office(project)
    for folder in (office, ships):
        with pytest.raises(ship.ShipError, match='holds no ship.toml'):
            ship.load(folder)
    crew = ships/'alpha'
    crew.mkdir()
    (crew/'ship.toml').write_text('[ship]\nname="Synthetic crew"\n')
    current = ship.load(crew)
    assert fleet.declaration_files(current) == (crew/'ship.toml',)
    assert set(fleet.peers(current)) == {'alpha'}
    assert ship.candidates(project) == ['alpha']
    setup.refuse_embedded_office(project)
    assert list(office.iterdir()) == []
    assert calls == []


def test_existing_office_keeps_its_declaration_cards_and_person_declared_seats(office_project):
    project, calls = office_project
    initialize()
    folder = project / "office"
    with (folder / "ship.toml").open("a") as stream:
        stream.write('''
[seats.counsel]
session = "office"
role = "counsel"
harness = "claude"
pool = "claude"
model_mode = "program-default"
effort_mode = "program-default"
''')
    (folder / "roles" / "desk.md").write_text("My own desk card\n")
    before = {p: p.read_bytes() for p in (folder / "ship.toml", folder / "roles" / "desk.md")}
    head = ship._git(folder, "rev-parse", "HEAD")
    lines = initialize()
    assert all(path.read_bytes() == content for path, content in before.items())
    assert ship._git(folder, "rev-parse", "HEAD") == head
    assert set(calls[-1].seats) == {"desk", "counsel"}
    assert not any("office created" in line for line in lines)


def test_office_is_kept_when_a_launch_fails_and_the_next_init_resumes_it(office_project, monkeypatch):
    project, calls = office_project
    def unavailable(*_):
        raise ship.ShipError("synthetic program unavailable")
    with monkeypatch.context() as patch:
        patch.setattr(setup.Operations, "up_desk", unavailable)
        with pytest.raises(ship.ShipError, match="program unavailable"):
            initialize()
    head = ship._git(project / "office", "rev-parse", "HEAD")
    initialize()
    assert ship._git(project / "office", "rev-parse", "HEAD") == head
    assert len(calls) == 1


def test_init_uses_normal_up_with_the_desk_named_explicitly(office_project, monkeypatch):
    project, _ = office_project
    initialize()
    observed = []
    monkeypatch.setattr(cli, "cmd_up", lambda args: observed.append(args) or ["normal up"])
    # Bind the original operation captured before fixtures replace the live boundary.
    assert UP_DESK(setup.Operations(), ship.load(project / "office")) == ["normal up"]
    assert vars(observed[0]) == {"ship": str(project / "office"), "seat": "desk", "fresh": False}


UP_DESK = setup.Operations.up_desk
PROJECT_FOLDER = setup.Operations.project_folder


def framework_folder(folder):
    folder.mkdir(parents=True)
    (folder / ".git").mkdir()
    (folder / "pyproject.toml").write_text('[project]\nname = "quarterdeck"\n')
    package = folder / "src" / "quarterdeck"
    package.mkdir(parents=True)
    (package / "install.py").write_text("")


def test_an_installed_office_finds_its_project_at_the_new_depth(tmp_path, monkeypatch):
    project = tmp_path / "project"
    framework_folder(project)
    text, _, _ = setup.template("office")
    office = ship._from_text(text, project / "office")
    monkeypatch.setattr(install, "__file__", str(tmp_path / "prefix" / "lib" / "quarterdeck" / "install.py"))
    assert install.source_repo(office) == project


def test_packaged_init_can_identify_the_checkout_from_its_current_directory(tmp_path, monkeypatch):
    project = tmp_path / "project"
    framework_folder(project)
    nested = project / "notes"
    nested.mkdir()
    monkeypatch.chdir(nested)
    monkeypatch.setattr(install, "__file__", str(tmp_path / "prefix" / "lib" / "quarterdeck" / "install.py"))
    assert PROJECT_FOLDER(setup.Operations()) == project


def test_init_cli_does_not_take_the_inherited_ship_or_default(office_project, monkeypatch, capsys):
    project, calls = office_project
    monkeypatch.setenv("QD_SHIP", str(project / "unrelated"))
    monkeypatch.setenv("QD_SEAT", "example")
    monkeypatch.setattr(cli, "handoff", lambda *_: pytest.fail("init must not hand off to an existing ship"))
    assert cli.main(["init"]) == 0
    assert len(calls) == 1 and calls[0].folder == project / "office"
    assert "Office ready" in capsys.readouterr().out
    assert not (project / "unrelated").exists()


def test_first_setup_initializes_only_the_office_then_keeps_the_crew_wizard(office_project, monkeypatch):
    project, calls = office_project
    wizard = []
    monkeypatch.setattr(setup, "run", lambda args, **kwargs: wizard.append(args) or ["crew wizard"])
    args = cli.build_parser().parse_args(["setup"])
    result = setup.first_step(args)
    assert len(calls) == 1 and not wizard
    assert "Office ready" in result[-1]
    assert setup.first_step(args) == ["crew wizard"]
    assert wizard == [args]


def test_first_setup_cli_has_no_project_workspace_question(office_project, monkeypatch, capsys):
    project, calls = office_project
    monkeypatch.setattr(setup, "run", lambda *_: pytest.fail("no crew wizard on first setup"))
    assert cli.main(["setup"]) == 0
    assert len(calls) == 1
    assert "Office ready" in capsys.readouterr().out
    assert not (project / "ships").exists()


def test_first_setup_keeps_explicit_save_without_launch_consent(office_project):
    project, calls = office_project
    args = cli.build_parser().parse_args(["setup", "--start", "no"])
    result = setup.first_step(args)
    assert ship.load(project / "office").kind == "office"
    assert not calls
    assert "desk not started" in result[-1]


def test_first_setup_keeps_explicit_decline_before_writing(office_project):
    project, calls = office_project
    args = cli.build_parser().parse_args(["setup", "--accept", "no"])
    result = setup.first_step(args)
    assert "no office changes" in result[0]
    assert not (project / "office").exists()
    assert not calls


def test_init_never_replaces_an_existing_non_office_folder(office_project):
    project, calls = office_project
    folder = project / "office"
    folder.mkdir()
    (folder / "note.txt").write_text("keep me")
    with pytest.raises(ship.ShipError, match="holds no ship.toml"):
        initialize()
    assert (folder / "note.txt").read_text() == "keep me"
    assert list(folder.iterdir()) == [folder / "note.txt"]
    assert not calls


@pytest.mark.parametrize("verb", ["init", "setup"])
def test_an_existing_persons_desk_requires_an_explicit_office_move(office_project, capsys, verb):
    project, calls = office_project
    (project / "ships").mkdir()
    text, orders, _ = setup.template("default")
    legacy = ship.create(project / "ships" / "established", text, orders)
    legacy.runtime.mkdir()
    (legacy.runtime / "keep.txt").write_text("Existing office records stay here.\n")
    before = {p.relative_to(legacy.folder): p.read_bytes()
              for p in legacy.folder.rglob("*") if p.is_file()}
    assert cli.main([verb]) == 2
    refusal = capsys.readouterr().err
    assert "office still lives in ship established" in refusal
    assert "person's step once forwarding is proven" in refusal
    assert not (project / "office").exists()
    assert not calls
    assert before == {p.relative_to(legacy.folder): p.read_bytes()
                      for p in legacy.folder.rglob("*") if p.is_file()}


def test_a_crew_without_a_persons_desk_does_not_prevent_first_office(office_project):
    project, calls = office_project
    (project / "ships").mkdir()
    text, orders, _ = setup.template("shuttle")
    ship.create(project / "ships" / "mission", text, orders)
    initialize()
    assert len(calls) == 1 and calls[0].kind == "office"
