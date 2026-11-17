"""Ordinary template proposals and successful synthetic raise lifecycles.

Authorization and consent refusal gates are owned separately on Claude.
"""
import argparse
import json
import os

import pytest

from quarterdeck import cli, raise_ship, setup, ship, standing


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(setup.Operations, "project_folder", lambda _: tmp_path)
    monkeypatch.delenv("QD_SHIP", raising=False)
    monkeypatch.delenv("QD_SEAT", raising=False)
    for key, value in {
        "GIT_AUTHOR_NAME": "Raise Example", "GIT_AUTHOR_EMAIL": "raise@example.invalid",
        "GIT_COMMITTER_NAME": "Raise Example", "GIT_COMMITTER_EMAIL": "raise@example.invalid",
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("GIT_DIR", raising=False)
    monkeypatch.delenv("GIT_WORK_TREE", raising=False)
    setup.init_office(argparse.Namespace(ship=None), start=False)
    office = tmp_path / "office"
    with (office / "ship.toml").open("a") as stream:
        stream.write('''
[pools.codex]
provider = "openai"
config_dir = "~/.codex"
[pools.grok]
provider = "xai"
config_dir = "~/.grok"
[pools.local]
provider = "local"
endpoint = "http://127.0.0.1:8000/v1"
context = 32768
''')
    calls = []
    monkeypatch.setattr(setup.Operations, "up", lambda _, current: calls.append(current) or ["synthetic crew up"])
    # pytest gives no terminal; the yes's terminal gate is proven on real terminals in test_raise_terminal.py.
    monkeypatch.setattr(raise_ship, "approving_terminal", lambda project: "/dev/pts/synthetic")
    return tmp_path, ship.load(office), calls


def args(kind="shuttle", name="mission", *extra):
    mapping = ["--pool", "anthropic=claude", "--pool", "openai=codex"] if kind in ("cascade", "shuttle") else []
    if kind == "cascade":
        mapping += ["--pool", "xai=grok", "--model", "third=subscription-model"]
    if kind == "forum":
        mapping += ["--model", "reviewer-4=served-model"]
    return cli.build_parser().parse_args(["raise", kind, name, *mapping, *extra])


@pytest.mark.parametrize("kind", raise_ship.KINDS)
def test_all_four_complete_template_proposals_are_read_only_and_loadable(project, kind):
    root, office, calls = project
    source = standing.project_dir() / "ships" / kind
    before = {p: p.read_bytes() for p in source.rglob("*") if p.is_file()}
    draft = raise_ship.proposal(args(kind), office)
    current = ship._from_text(draft.files["ship.toml"], draft.folder)
    assert current.name == "mission" and current.workspace == root
    assert draft.folder == root / "ships" / "mission"
    assert draft.files["orders.md"] == (source / "orders.md").read_text()
    for name, content in draft.files.items():
        if name not in ("ship.toml", ".gitignore"):
            assert content == (source / name).read_text()
    if kind == "forum":
        assert "work/README.md" in draft.files
    assert current.seats.keys() == ship._from_text((source / "ship.toml").read_text(), source).seats.keys()
    assert not (root / "ships").exists() and not calls
    assert before == {p: p.read_bytes() for p in source.rglob("*") if p.is_file()}


def test_person_sees_the_proposal_and_the_exact_yes_is_saved_before_start(project):
    root, office, calls = project
    declaration = office.file.read_bytes()
    shown = []
    result = raise_ship.run(args(), interactive=True, read=lambda prompt: shown.append(prompt) or "yes", write=shown.append)
    raised = ship.load(root / "ships" / "mission")
    answers = list((office.runtime / "raise" / "mission").glob("*.answer-*.json"))
    assert len(answers) == 1 and json.loads(answers[0].read_text())["answer"] == "yes"
    assert any("Complete declaration" in line for line in shown)
    assert "synthetic crew up" in result and calls == [raised]
    assert set(ship._git(raised.folder, "ls-files").splitlines()) == set(raise_ship.proposal(args(), office).files)
    assert ship._git(raised.folder, "status", "--porcelain") == ""
    assert office.file.read_bytes() == declaration
    assert result[-1] == "Fleet address: mission"


def test_desk_reuses_an_exact_person_approval_and_resumes_without_editing(project, monkeypatch):
    root, office, calls = project
    raise_ship.run(args(), interactive=True, read=lambda _: "yes", write=lambda _: None)
    folder = root / "ships" / "mission"
    head = ship._git(folder, "rev-parse", "HEAD")
    before = {p.relative_to(folder): p.read_bytes() for p in folder.rglob("*") if p.is_file()}
    answer_files = list((office.runtime / "raise" / "mission").glob("*.answer-*.json"))
    monkeypatch.setenv("QD_SHIP", str(office.folder))
    monkeypatch.setenv("QD_SEAT", "desk")
    lines = raise_ship.run(args(), interactive=False)
    assert "resuming without edits" in lines[0]
    assert len(calls) == 2 and ship._git(folder, "rev-parse", "HEAD") == head
    assert before == {p.relative_to(folder): p.read_bytes() for p in folder.rglob("*") if p.is_file()}
    assert answer_files == list((office.runtime / "raise" / "mission").glob("*.answer-*.json"))


def test_cli_raise_uses_the_office_entry_in_a_persons_shell(project, monkeypatch, capsys):
    root, office, calls = project
    monkeypatch.setattr(cli, "handoff", lambda *_: pytest.fail("raise is a fleet entry"))
    assert cli.main(["raise", "default", "new-crew", "--accept", "yes"]) == 0
    assert calls[0].folder == root / "ships" / "new-crew"
    assert "Fleet address: new-crew" in capsys.readouterr().out


def startless_record(root, pid):
    path = root / "office" / "runtime" / "trusted" / "example" / "launch.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"pid": pid}))
    return path


def test_a_record_without_a_start_beside_the_persons_shell_names_it_and_takes_the_yes(project):
    root, office, calls = project
    ancestors = {pid for pid, _, _ in raise_ship.process_ancestry(os.getpid())}
    stranger = next(pid for pid in range(4194303, 1, -1) if pid not in ancestors)
    path = startless_record(root, stranger)
    result = raise_ship.run(args(), interactive=True, read=lambda _: "yes", write=lambda _: None)
    note = (f"a recorded seat program without a readable start time was ignored: {path} "
            f"(its pid {stranger} is not in this process ancestry)")
    assert result[0] == note and result[-1] == "Fleet address: mission"
    assert calls == [ship.load(root / "ships" / "mission")]


def test_a_record_without_a_start_in_the_persons_ancestry_refuses_the_yes(project):
    root, office, calls = project
    path = startless_record(root, os.getpid())
    with pytest.raises(ship.ShipError, match=rf"without a readable start time \({path}\) has pid {os.getpid()}"):
        raise_ship.run(args(), interactive=True, read=lambda _: "yes", write=lambda _: None)
    assert not (root / "ships" / "mission").exists() and not calls
    assert not list((office.runtime / "raise" / "mission").glob("*.answer-*.json"))
