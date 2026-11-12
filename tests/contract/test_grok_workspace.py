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
    monkeypatch.setattr(grok_notice, "trust_scope", lambda cwd, environment: (cwd, False))
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


def test_boundary_nonrepository_grant_and_notice_roundtrip_on_restart(tmp_path, monkeypatch):
    adapter, spec, pool, home, brief = prepared(tmp_path)
    adapter.build_home(home, spec, pool, None)
    path = write_native_grant(home, spec.cwd)
    before = path.read_bytes()
    hooks = spec.cwd / ".grok" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "boundary.json").write_text(json.dumps({"hooks": {"SessionStart": []}}))
    (spec.cwd / ".grok" / "config.toml").write_text(
        '[mcp_servers.synthetic_boundary]\ncommand = "synthetic-transport"\n')
    monkeypatch.setattr(grok_workspace.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=128, stdout="", stderr=(
            "fatal: not a git repository (or any parent up to mount point /synthetic-mount)\n"
            "Stopping at filesystem boundary (GIT_DISCOVERY_ACROSS_FILESYSTEM not set).\n")))
    marker_queries = []
    original_lstat = Path.lstat
    def metadata(candidate, *args, **kwargs):
        if candidate.name == ".git":
            marker_queries.append(candidate)
            raise FileNotFoundError("synthetic absent repository marker")
        return original_lstat(candidate, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", metadata)
    monkeypatch.setattr(grok_notice, "trust_scope", grok_workspace._trust_scope)
    adapter.preflight(home, spec.cwd)
    adapter.build_home(home, spec, pool, None)
    for resume in (None, "synthetic-resume"):
        launch = adapter.launch(spec, pool, home, brief, resume, None)
        assert launch.argv[launch.argv.index("--cwd") + 1] == str(spec.cwd)
        assert "--trust" in launch.argv
        assert "boundary.json" in launch.notice and "synthetic_boundary" in launch.notice
        assert "unknown" not in launch.notice
        assert launch.notice == adapter.workspace_notice(home, spec.cwd)
    assert set(marker_queries) == {folder / ".git" for folder in (spec.cwd, *spec.cwd.parents)}
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
    monkeypatch.setattr(grok_notice, "trust_scope", lambda cwd, environment: (root, True))
    notice = adapter.workspace_notice(home, child)
    spec = replace(spec, cwd=child)
    adapter.build_home(home, spec, pool, None)
    assert adapter.launch(spec, pool, home, brief, None, None).notice == notice
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
    monkeypatch.setattr(grok_notice, "trust_scope", lambda cwd, environment: (spec.cwd, False))
    notice = grok_notice.inventory(spec.cwd, grok.private_environment(home))
    assert "unknown" in notice
    assert ".mcp.json" in notice and str(agents) in notice
    assert "none declared" not in notice
    adapter.build_home(home, spec, pool, None)
    launch = adapter.launch(spec, pool, home, brief, None, None)
    assert "--trust" in launch.argv
    assert launch.notice == notice


def test_notice_reports_unknown_scope_instead_of_an_empty_inventory(tmp_path, monkeypatch):
    adapter, spec, pool, home, brief = prepared(tmp_path)
    def unavailable(cwd, environment):
        from quarterdeck.ship import ShipError
        raise ShipError("synthetic metadata unavailable")
    monkeypatch.setattr(grok_notice, "trust_scope", unavailable)
    notice = grok_notice.inventory(spec.cwd, grok.private_environment(home))
    assert "project hooks unknown" in notice
    assert "project MCP servers unknown" in notice
    assert "none declared" not in notice


@pytest.mark.parametrize("repository", [False, True])
def test_notice_wording_uses_the_positive_scope_fact_at_cwd(tmp_path, monkeypatch, repository):
    adapter, spec, pool, home, brief = prepared(tmp_path)
    monkeypatch.setattr(grok_notice, "trust_scope", lambda cwd, environment: (cwd, repository))
    notice = adapter.workspace_notice(home, spec.cwd)
    if repository:
        assert f"(whole repository {json.dumps(str(spec.cwd))})" in notice
        assert "no repository; grant" not in notice
    else:
        assert f"(no repository; grant {json.dumps(str(spec.cwd))})" in notice
        assert "whole repository" not in notice


def test_notice_lsp_presence_and_unknown_are_path_only_information(tmp_path, monkeypatch):
    adapter, spec, pool, home, brief = prepared(tmp_path)
    plugin = spec.cwd / ".grok" / "plugins" / "synthetic-plugin"
    plugin.mkdir(parents=True)
    direct = spec.cwd / ".grok" / "lsp.json"
    plugin_file = plugin / ".lsp.json"
    manifest = plugin / "plugin.json"
    # No native serialization is established: these bytes are never parsed.
    for path in (direct, plugin_file, manifest):
        path.write_text("synthetic configuration values stay unread")
    notice = adapter.workspace_notice(home, spec.cwd)
    for path in (direct, plugin_file):
        assert f"declared, shape unknown {json.dumps(str(path))}" in notice
    assert "LSP:" in notice and str(manifest) in notice
    assert "layout/activation unknown" in notice
    assert "synthetic configuration values" not in notice
    original_metadata = grok_notice.metadata
    def unavailable(path):
        if path in (direct, plugin_file):
            return None, "metadata unreadable"
        return original_metadata(path)
    monkeypatch.setattr(grok_notice, "metadata", unavailable)
    unknown = adapter.workspace_notice(home, spec.cwd)
    assert "LSP: unknown:" in unknown
    assert "declared, shape unknown" not in unknown
    assert str(direct) in unknown and str(plugin_file) in unknown
    assert "synthetic configuration values" not in unknown
    adapter.build_home(home, spec, pool, None)
    launch = adapter.launch(spec, pool, home, brief, None, None)
    assert "--trust" in launch.argv and launch.notice == unknown
