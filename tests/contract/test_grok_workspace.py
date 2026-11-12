"""Ordinary synthetic trusted-workspace launch output; no native program calls."""

from dataclasses import replace
from pathlib import Path
import tomllib

import pytest

from quarterdeck.harness import grok
from quarterdeck.harness.grok import GrokHarness
from quarterdeck.ship import Pool, SeatSpec


@pytest.fixture(autouse=True)
def synthetic_native_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(grok, "SYSTEM_CONFIG", tmp_path / "synthetic-system")
    for name in grok.INHERITED_REDIRECTS:
        monkeypatch.delenv(name, raising=False)


def prepared(tmp_path):
    cwd = tmp_path / "project"
    cwd.mkdir()
    home = tmp_path / "seat-home"
    brief = tmp_path / "brief.txt"
    brief.write_text("The ship's orders take precedence over project files.")
    pool = Pool("synthetic-pool", "xai")
    spec = SeatSpec("synthetic-seat", "synthetic-session", "worker", "grok", (pool.name,), cwd=cwd)
    return GrokHarness(), spec, pool, home, brief


def test_instruction_bearing_project_has_an_ordinary_trusted_launch(tmp_path):
    adapter, spec, pool, home, brief = prepared(tmp_path)
    for name in ("AGENTS.md", "CLAUDE.md"):
        (spec.cwd / name).write_text("Synthetic project conventions.\n")
    adapter.preflight(home, spec.cwd)
    adapter.build_home(home, spec, pool, None)
    launch = adapter.launch(spec, pool, home, brief, None, None)
    assert "--trust" in launch.argv
    assert launch.argv[launch.argv.index("--cwd") + 1] == str(spec.cwd)
    assert launch.argv[launch.argv.index("--rules") + 1] == brief.read_text()
    assert launch.env["GROK_HOME"] == str(home)
    for name in ("AGENTS.md", "CLAUDE.md"):
        assert (spec.cwd / name).read_text() == "Synthetic project conventions.\n"


def test_child_workspace_does_not_need_adapter_git_discovery(tmp_path, monkeypatch):
    adapter, spec, pool, home, brief = prepared(tmp_path)
    (spec.cwd / "AGENTS.md").write_text("Synthetic root conventions.\n")
    child = spec.cwd / "package"
    child.mkdir()
    spec = replace(spec, cwd=child)
    # A caller's Git settings no longer select an adapter discovery process.
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "synthetic-git-metadata"))
    adapter.build_home(home, spec, pool, None)
    launch = adapter.launch(spec, pool, home, brief, None, None)
    assert launch.argv[launch.argv.index("--cwd") + 1] == str(child)
    assert "--trust" in launch.argv


def test_card_explains_native_trust_imports_and_configured_compatibility(tmp_path):
    adapter, spec, pool, home, brief = prepared(tmp_path)
    adapter.build_home(home, spec, pool, None)
    config = tomllib.loads((home / "config.toml").read_text())
    for vendor in grok.VENDORS:
        assert config["compat"][vendor] == {kind: False for kind in grok.IMPORTS}
    assert str(home / "bundled" / "skills") in config["skills"]["ignore"]
    assert {"resume-claude", "resume-codex", "resume-cursor"} <= set(config["skills"]["disabled"])
    card = (Path(__file__).parents[2] / "standing-orders" / "harnesses" / "grok.md").read_text()
    for source in ("AGENTS.md", "CLAUDE.md", ".grok/rules", ".agents/skills", ".grok/lsp.json"):
        assert source in card
    assert "ship's brief and standing orders take precedence" in card
    assert "unproved pending independent native validation" in card
