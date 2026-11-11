"""Synthetic project-source/output cases; no installed Grok or native isolation proof."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import doctor
from quarterdeck.harness import grok, grok_workspace
from quarterdeck.ship import ShipError
from tests.contract.test_grok_home import synthetic, synthetic_native_paths

DISCOVER = grok_workspace.discover
EXPECTED_SOURCES = (
    "Agents.md", "Claude.md", "CLAUDE.md", "CLAUDE.local.md", "AGENT.md", "AGENTS.md",
    ".claude/CLAUDE.md", ".claude/CLAUDE.local.md", ".grok/rules", ".claude/rules", ".cursor/rules",
    ".mcp.json", ".grok/hooks", ".claude/settings.json", ".claude/settings.local.json", ".cursor/hooks.json",
    ".grok/workflows", ".grok/skills", ".grok/commands", ".agents/skills", ".agents/commands",
    ".claude/skills", ".claude/commands", ".cursor/skills", ".grok/agents", ".grok/personas", ".grok/roles",
    ".grok/plugins", ".grok/lsp.json",
)


def make_source(root, relative):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix in (".md", ".json"):
        path.write_text("synthetic project source")
    else:
        path.mkdir()
    return path


@pytest.mark.parametrize("relative", EXPECTED_SOURCES)
def test_each_project_source_at_root_refuses_a_clean_subfolder(tmp_path, monkeypatch, relative):
    adapter, spec, pool, home, brief = synthetic(tmp_path)
    root = tmp_path / "synthetic-repository"
    cwd = root / "clean-subfolder"
    cwd.mkdir(parents=True)
    monkeypatch.setattr(grok_workspace, "discover", lambda folder, environment: [root, cwd])
    spec = replace(spec, cwd=cwd)
    adapter.build_home(home, spec, pool, None)
    source = make_source(root, relative)
    for operation in (lambda: adapter.build_home(home, spec, pool, None),
                      lambda: adapter.launch(spec, pool, home, brief, None, None)):
        with pytest.raises(ShipError) as error:
            operation()
        assert str(source) in str(error.value)
    ship = SimpleNamespace(runtime=tmp_path / "runtime", seats={spec.name: spec}, cwd_of=lambda selected: selected.cwd)
    failed, = doctor.seat_readiness(ship)
    assert str(source) in failed.detail


@pytest.mark.parametrize("section", ("mcp_servers", "plugins"))
def test_project_configuration_sections_are_named(tmp_path, section):
    adapter, spec, pool, home, brief = synthetic(tmp_path)
    adapter.build_home(home, spec, pool, None)
    path = spec.cwd / ".grok" / "config.toml"
    path.parent.mkdir()
    path.write_text(f"[{section}]\n")
    with pytest.raises(ShipError) as error:
        adapter.launch(spec, pool, home, brief, None, None)
    assert str(path) in str(error.value)


def test_unrelated_project_configuration_is_not_refused_by_filename(tmp_path):
    adapter, spec, pool, home, brief = synthetic(tmp_path)
    path = spec.cwd / ".grok" / "config.toml"
    path.parent.mkdir()
    path.write_text('[models]\ndefault = "synthetic"\n')
    adapter.build_home(home, spec, pool, None)
    adapter.launch(spec, pool, home, brief, None, None)
    assert path.read_text() == '[models]\ndefault = "synthetic"\n'


def test_all_found_project_sources_are_reported_together(tmp_path):
    adapter, spec, pool, home, brief = synthetic(tmp_path)
    adapter.build_home(home, spec, pool, None)
    paths = [make_source(spec.cwd, relative) for relative in ("AGENTS.md", ".mcp.json", ".grok/hooks", ".grok/workflows")]
    with pytest.raises(ShipError) as error:
        adapter.launch(spec, pool, home, brief, None, None)
    assert all(str(path) in str(error.value) for path in paths)


def test_git_scope_uses_bounded_metadata_with_private_home(tmp_path, monkeypatch):
    root = tmp_path / "synthetic-repository"
    cwd = root / "package" / "src"
    cwd.mkdir(parents=True)
    home = tmp_path / "native-home"
    calls = []
    def query(argv, **options):
        calls.append((argv, options))
        return SimpleNamespace(returncode=0, stdout=str(root) + "\n", stderr="")
    monkeypatch.setattr(grok_workspace.subprocess, "run", query)
    assert DISCOVER(cwd, grok.private_environment(home)) == [root, root / "package", cwd]
    argv, options = calls[0]
    assert argv == ["git", "-C", str(cwd), "rev-parse", "--path-format=absolute", "--show-toplevel"]
    assert options["timeout"] == 5
    assert options["env"]["HOME"] == str(home / "user-home")


def test_positive_nonrepository_fact_limits_scope_to_cwd(tmp_path, monkeypatch):
    def query(*args, **kwargs):
        return SimpleNamespace(returncode=128, stdout="", stderr="fatal: not a git repository (or any of the parent directories): .git\n")
    monkeypatch.setattr(grok_workspace.subprocess, "run", query)
    monkeypatch.setattr(grok_workspace, "metadata", lambda path: None)
    assert DISCOVER(tmp_path, grok.private_environment(tmp_path / "native-home")) == [tmp_path]


def test_card_inventory_and_documented_bundle_configuration(tmp_path):
    adapter, spec, pool, home, brief = synthetic(tmp_path)
    adapter.build_home(home, spec, pool, None)
    import tomllib
    config = tomllib.loads((home / "config.toml").read_text())
    assert str(home / "bundled" / "skills") in config["skills"]["ignore"]
    assert {"resume-claude", "resume-codex", "resume-cursor"} <= set(config["skills"]["disabled"])
    assert config["features"]["subagent_bundle"] is False
    assert adapter.launch(spec, pool, home, brief, None, None).env["GROK_SUBAGENT_BUNDLE"] == "0"
    card = (Path(__file__).parents[2] / "standing-orders" / "harnesses" / "grok.md").read_text()
    assert set(grok_workspace.PROJECT_SOURCES) == set(EXPECTED_SOURCES)
    assert all(relative in card for relative in EXPECTED_SOURCES)
    assert "cannot work at the framework root" in card
    assert "clean subfolder" in card
    assert all(name in card for name in ("resume-claude", "resume-codex", "resume-cursor", "22", "signed settings cache", "models cache"))
