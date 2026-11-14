"""A real synthetic crew raised in a throwaway project on a private server."""
import argparse
import os
import shutil

from quarterdeck import cli, raise_ship, servicectl, setup, ship, standing


def test_person_raises_and_resumes_a_complete_synthetic_shuttle(tmp_path, monkeypatch, tmux):
    project = tmp_path / "project"
    project.mkdir()
    resources = tmp_path / "resources"
    for kind in ("office", "shuttle"):
        source = resources / "ships" / kind
        shutil.copytree(standing.project_dir() / "ships" / kind, source)
        text = (source / "ship.toml").read_text()
        text = text.replace('provider = "anthropic"', 'provider = "fake"')
        text = text.replace('provider = "openai"', 'provider = "fake"')
        text = text.replace('harness = "claude"', 'harness = "fake"').replace('harness = "codex"', 'harness = "fake"')
        text = text.replace('config_dir = "~/.claude"\n', '').replace('config_dir = "~/.codex"\n', '')
        text = ship.edit(text, "ship", "code", "checkout")
        text = ship.edit(text, "ship", "tick", 0.1)
        (source / "ship.toml").write_text(text)
    monkeypatch.setattr(standing, "project_dir", lambda: resources)
    monkeypatch.setattr(setup.Operations, "project_folder", lambda _: project)
    monkeypatch.delenv("QD_SHIP", raising=False)
    monkeypatch.delenv("QD_SEAT", raising=False)
    tmux.new_session("anchor")
    monkeypatch.setenv("QD_TMUX_SOCKET", str(tmux.socket))
    for key, value in {
        "GIT_AUTHOR_NAME": "Raise Example", "GIT_AUTHOR_EMAIL": "raise@example.invalid",
        "GIT_COMMITTER_NAME": "Raise Example", "GIT_COMMITTER_EMAIL": "raise@example.invalid",
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
    }.items():
        monkeypatch.setenv(key, value)
    setup.init_office(argparse.Namespace(ship=None), start=False)
    args = cli.build_parser().parse_args([
        "raise", "shuttle", "mission", "--pool", "anthropic=claude", "--pool", "openai=claude", "--accept", "yes"])
    folder = project / "ships" / "mission"
    try:
        result = raise_ship.run(args)
        raised = ship.load(folder)
        assert set(raised.seats) == {"captain", "engineer", "first-officer"}
        assert len(tmux.panes("Shuttle")) == 3
        assert servicectl.alive(raised.runtime)
        assert any("service started" in line for line in result)
        assert ship._git(folder, "status", "--porcelain") == ""
        assert (folder / "roles" / "first-officer.md").is_file()
        assert (folder / "mission-record.md").is_file()
        assert not (project / "office" / "runtime" / "seats").exists()
        head = ship._git(folder, "rev-parse", "HEAD")
        args.accept = None
        result = raise_ship.run(args, interactive=False)
        assert "resuming without edits" in result[0]
        assert len(tmux.panes("Shuttle")) == 3
        assert ship._git(folder, "rev-parse", "HEAD") == head
        assert ship._git(folder, "status", "--porcelain") == ""
    finally:
        if (folder / "ship.toml").exists():
            cli.cmd_down(argparse.Namespace(ship=str(folder), seat=None))
