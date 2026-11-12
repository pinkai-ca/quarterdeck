"""Ordinary synthetic trusted-workspace launch output; no native program calls."""

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import tomllib

import pytest

from quarterdeck.harness import grok, grok_notice, grok_workspace
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


def write_native_grant(home, root):
    path = home / "trusted_folders.toml"
    path.write_text(f"[folders.{json.dumps(str(root))}]\ntrusted = true\ndecided_at = 1700000000\n")
    return path


def test_native_repository_grant_roundtrips_for_a_nested_workspace(tmp_path, monkeypatch):
    adapter, spec, pool, home, brief = prepared(tmp_path)
    root = spec.cwd
    child = root / "package"
    child.mkdir()
    spec = replace(spec, cwd=child)
    adapter.build_home(home, spec, pool, None)
    path = write_native_grant(home, root)
    before = path.read_bytes()
    calls = []
    def metadata(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=str(root) + "\n", stderr="")
    monkeypatch.setattr(grok_workspace.subprocess, "run", metadata)
    adapter.preflight(home, child)
    adapter.build_home(home, spec, pool, None)
    for resume in (None, "synthetic-resume"):
        launch = adapter.launch(spec, pool, home, brief, resume, None)
        assert launch.argv[launch.argv.index("--cwd") + 1] == str(child)
        assert launch.argv.count("--trust") == 1
    assert path.read_bytes() == before
    assert tomllib.loads(path.read_text()) == {"folders": {str(root): {
        "trusted": True, "decided_at": 1700000000}}}
    assert calls
    for argv, kwargs in calls:
        assert argv == ["git", "-C", str(child), "rev-parse", "--path-format=absolute", "--show-toplevel"]
        assert kwargs["timeout"] == 5
        assert kwargs["env"]["HOME"] == str(home / "user-home")
        assert kwargs["env"]["GROK_HOME"] == str(home)


def test_native_nonrepository_grant_uses_the_workspace(tmp_path, monkeypatch):
    adapter, spec, pool, home, brief = prepared(tmp_path)
    adapter.build_home(home, spec, pool, None)
    path = write_native_grant(home, spec.cwd)
    before = path.read_bytes()
    # Both the query and marker metadata describe an invented non-repository;
    # the runner's own temporary folder may be inside the framework repository.
    monkeypatch.setattr(grok_workspace, "has_repository_marker", lambda cwd: False)
    monkeypatch.setattr(grok_workspace.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=128, stdout="", stderr="fatal: not a git repository (or any of the parent directories): .git"))
    adapter.build_home(home, spec, pool, None)
    launch = adapter.launch(spec, pool, home, brief, None, None)
    assert "--trust" in launch.argv
    assert path.read_bytes() == before


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
    assert "project hooks run" in card
    assert "project MCP servers start" in card


def test_notice_inventory_names_declared_sources_without_transport_or_hook_values(tmp_path, monkeypatch):
    adapter, spec, pool, home, brief = prepared(tmp_path)
    root = spec.cwd
    child = root / "package"
    child.mkdir()
    hooks = root / ".grok" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "synthetic.json").write_text(json.dumps({"hooks": {"SessionStart": [
        {"hooks": [{"type": "command", "command": "synthetic-command-value"}]}]}}))
    (root / ".grok" / "config.toml").write_text(
        '[mcp_servers.synthetic_native]\ncommand = "synthetic-native-transport"\n')
    (child / ".mcp.json").write_text(json.dumps({"mcpServers": {"synthetic_json": {
        "command": "synthetic-json-transport", "env": {"EXAMPLE": "synthetic-env-value"},
        "headers": {"Example": "synthetic-header-value"}}}}))
    monkeypatch.setattr(grok_notice, "trust_root", lambda cwd, environment: root)
    notice = grok_notice.inventory(child, grok.private_environment(home))
    for name in ("synthetic_native", "synthetic_json", "SessionStart", "synthetic.json"):
        assert name in notice
    assert str(root) in notice and str(child) in notice
    for value in ("synthetic-command-value", "synthetic-native-transport", "synthetic-json-transport",
                  "synthetic-env-value", "synthetic-header-value"):
        assert value not in notice
    assert "inventory only, native activation not inspected" in notice


def test_notice_unknown_sources_are_information_without_a_workspace_refusal(tmp_path, monkeypatch):
    adapter, spec, pool, home, brief = prepared(tmp_path)
    (spec.cwd / ".mcp.json").write_text("synthetic unrecognized document")
    agents = spec.cwd / ".grok" / "agents"
    agents.mkdir(parents=True)
    monkeypatch.setattr(grok_notice, "trust_root", lambda cwd, environment: spec.cwd)
    notice = grok_notice.inventory(spec.cwd, grok.private_environment(home))
    assert "unknown" in notice
    assert ".mcp.json" in notice and str(agents) in notice
    assert "none declared" not in notice
    adapter.build_home(home, spec, pool, None)
    assert "--trust" in adapter.launch(spec, pool, home, brief, None, None).argv


def test_notice_reports_unknown_scope_instead_of_an_empty_inventory(tmp_path, monkeypatch):
    adapter, spec, pool, home, brief = prepared(tmp_path)
    def unavailable(cwd, environment):
        from quarterdeck.ship import ShipError
        raise ShipError("synthetic metadata unavailable")
    monkeypatch.setattr(grok_notice, "trust_root", unavailable)
    notice = grok_notice.inventory(spec.cwd, grok.private_environment(home))
    assert "project hooks unknown" in notice
    assert "project MCP servers unknown" in notice
    assert "none declared" not in notice
