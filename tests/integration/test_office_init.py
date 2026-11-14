"""A throwaway office with the synthetic program and its own tmux server."""
import argparse
import os
import shutil

from quarterdeck import cli, servicectl, setup, ship, standing


def test_init_runs_a_synthetic_office_with_only_its_desk(tmp_path, monkeypatch, tmux):
    project = tmp_path / "project"
    project.mkdir()
    resources = tmp_path / "resources"
    source = resources / "ships" / "office"
    shutil.copytree(standing.project_dir() / "ships" / "office", source)
    text = (source / "ship.toml").read_text()
    text = text.replace('provider = "anthropic"', 'provider = "fake"')
    text = text.replace('harness = "claude"', 'harness = "fake"')
    text = text.replace('config_dir = "~/.claude"\n', '')
    text = text.replace('model_mode = "program-default"\n', '').replace('effort_mode = "program-default"\n', '')
    text = ship.edit(text, "ship", "code", "checkout")
    text = ship.edit(text, "ship", "tick", 0.1)
    (source / "ship.toml").write_text(text)
    monkeypatch.setattr(standing, "project_dir", lambda: resources)
    monkeypatch.setattr(setup.Operations, "project_folder", lambda _: project)
    monkeypatch.setenv("QD_TMUX_SOCKET", str(tmux.socket))
    for key, value in {
        "GIT_AUTHOR_NAME": "Office Example", "GIT_AUTHOR_EMAIL": "office@example.invalid",
        "GIT_COMMITTER_NAME": "Office Example", "GIT_COMMITTER_EMAIL": "office@example.invalid",
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
    }.items():
        monkeypatch.setenv(key, value)
    office_folder = project / "office"
    current = None
    try:
        lines = setup.init_office(argparse.Namespace(ship=None))
        current = ship.load(office_folder)
        assert list(current.seats) == ["desk"]
        assert any("desk up in" in line for line in lines)
        assert any("service started" in line for line in lines)
        assert servicectl.alive(current.runtime)
        assert tmux.has_session("Office")
        assert len(tmux.panes("Office")) == 1
        assert (current.runtime / ".git").is_dir()
        assert ship._git(office_folder, "status", "--porcelain") == ""
        assert not (project / "ships").exists()
        head = ship._git(office_folder, "rev-parse", "HEAD")
        setup.init_office(argparse.Namespace(ship=None))
        assert ship._git(office_folder, "rev-parse", "HEAD") == head
        assert len(tmux.panes("Office")) == 1
    finally:
        if (office_folder / "ship.toml").exists():
            cli.cmd_down(argparse.Namespace(ship=str(office_folder), seat=None))
