"""Office creation and the first setup step, with synthetic launch operations."""
import argparse
import os
from pathlib import Path

import pytest

from quarterdeck import cli, setup, ship, standing


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


def test_init_copies_and_commits_the_office_then_starts_only_its_desk(office_project):
    project, calls = office_project
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
    args = argparse.Namespace()
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
