"""Guide-backed synthetic home output; no native program or isolation proof."""

from dataclasses import replace
import json
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import doctor
from quarterdeck.harness import grok, grok_notice
from quarterdeck.harness.grok import GrokHarness
from quarterdeck.ship import Pool, SeatSpec, ShipError


@pytest.fixture(autouse=True)
def synthetic_native_paths(monkeypatch, tmp_path):
    """Ordinary home-output checks use fake admin paths, never actual /etc."""
    monkeypatch.setattr(grok, "SYSTEM_CONFIG", tmp_path / "synthetic-system")
    monkeypatch.setattr(grok_notice, "trust_root", lambda cwd, environment: cwd)
    for name in grok.INHERITED_REDIRECTS:
        monkeypatch.delenv(name, raising=False)


def synthetic(tmp_path):
    pool_home = tmp_path / "synthetic-pool"
    pool_home.mkdir()
    (pool_home / "auth.json").write_text(json.dumps({"synthetic-grant": {
        "refresh_token": "synthetic-refresh", "expires_at": "2040-02-01T00:00:00Z"}}))
    pool = Pool("synthetic-plan", "xai", config_dir=pool_home)
    spec = SeatSpec("synthetic-seat", "synthetic-session", "worker", "grok", (pool.name,), cwd=tmp_path)
    brief = tmp_path / "brief.txt"
    brief.write_text("Synthetic standing brief.")
    return GrokHarness(), spec, pool, tmp_path / "native-home", brief


def test_launch_grants_workspace_trust_in_its_private_native_home(tmp_path):
    adapter, spec, pool, home, brief = synthetic(tmp_path)
    adapter.build_home(home, spec, pool, None)
    for resume in (None, "synthetic-resume"):
        launch = adapter.launch(spec, pool, home, brief, resume, None)
        assert launch.argv.count("--trust") == 1
        assert launch.argv[launch.argv.index("--cwd") + 1] == str(spec.cwd)
        assert launch.env["GROK_HOME"] == str(home)


def test_guide_home_explicitly_disables_memory_capture_and_dream(tmp_path):
    adapter, spec, pool, home, brief = synthetic(tmp_path)
    spec = replace(spec, settings={"memory": {"enabled": True},
                                  "memory_v2": {"enabled": True, "batch_dream_enabled": True}})
    adapter.build_home(home, spec, pool, None)
    config = tomllib.loads((home / "config.toml").read_text())
    assert config["memory"]["enabled"] is False
    assert config["memory_v2"]["enabled"] is False
    assert config["memory_v2"]["batch_dream_enabled"] is False
    launch = adapter.launch(spec, pool, home, brief, None, None)
    assert launch.env["GROK_MEMORY"] == "0"
    assert "--no-memory" in launch.argv


def test_guide_home_accepts_ship_config_without_extra_rule_dirs(tmp_path):
    adapter, spec, pool, home, brief = synthetic(tmp_path)
    template = tmp_path / "ship-template"
    template.mkdir()
    (template / "config.toml").write_text('[models]\ndefault = "synthetic-model"\n')
    adapter.build_home(home, spec, pool, template)
    config = tomllib.loads((home / "config.toml").read_text())
    assert config["models"]["default"] == "synthetic-model"
    assert "extra_rule_dirs" not in config.get("paths", {})
    adapter.launch(spec, pool, home, brief, None, None)
    for layer in ("template", "seat"):
        (template / "config.toml").write_text('[models]\ndefault = "synthetic-model"\n')
        candidate = spec
        if layer == "template":
            (template / "config.toml").write_text('[paths]\nextra_rule_dirs = []\n')
        else:
            candidate = replace(spec, settings={"paths": {"extra_rule_dirs": ["/synthetic/rules"]}})
        before = (home / "config.toml").read_bytes()
        with pytest.raises(ShipError, match="paths.extra_rule_dirs"):
            adapter.build_home(home, candidate, pool, template)
        assert (home / "config.toml").read_bytes() == before


def test_launch_declares_private_home_and_configuration_paths(tmp_path, monkeypatch):
    adapter, spec, pool, home, brief = synthetic(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "synthetic-operator"))
    monkeypatch.setenv("GROK_CONFIG", '{"models":{"default":"synthetic-inherited"}}')
    monkeypatch.setenv("GROK_CONFIG_PATH", str(tmp_path / "synthetic-overlay.toml"))
    adapter.build_home(home, spec, pool, None)
    launch = adapter.launch(spec, pool, home, brief, None, None)
    assert Path(launch.env["HOME"]) == home / "user-home"
    assert Path(launch.env["HOME"]).is_dir()
    assert launch.env["GROK_HOME"] == str(home)
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        assert Path(launch.env[name]).is_relative_to(home / "user-home")
        assert Path(launch.env[name]).is_dir()
    assert launch.env["GROK_CONFIG"] == "{}"
    assert launch.env["GROK_CONFIG_PATH"] == str(home / "config.toml")


def test_home_does_not_write_native_policy_files(tmp_path):
    adapter, spec, pool, home, brief = synthetic(tmp_path)
    adapter.build_home(home, spec, pool, None)
    config = tomllib.loads((home / "config.toml").read_text())
    assert config["features"]["managed_config"] is False
    assert not set(grok.MCP_POLICY_KEYS).intersection(config)
    assert not (home / "requirements.toml").exists()
    assert not (home / "managed_config.toml").exists()


def test_admin_layer_readiness_names_fake_paths_and_preserves_content(tmp_path):
    adapter, spec, pool, home, brief = synthetic(tmp_path)
    adapter.build_home(home, spec, pool, None)
    paths = (grok.SYSTEM_CONFIG / "managed_config.toml", grok.SYSTEM_CONFIG / "requirements.toml",
             home / "requirements.toml", home / "managed_config.toml")
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic existing admin state")
        for operation in (lambda: adapter.preflight(home, spec.cwd),
                          lambda: adapter.build_home(home, spec, pool, None),
                          lambda: adapter.launch(spec, pool, home, brief, None, None)):
            with pytest.raises(ShipError) as error:
                operation()
            assert str(path) in str(error.value)
        if path.parent == grok.SYSTEM_CONFIG:
            checked = doctor.pool_check(pool)
            assert checked.passed is False
            assert str(path) in checked.detail
        assert path.read_text() == "synthetic existing admin state"
        path.unlink()


def test_doctor_checks_the_resolved_harness_for_each_pool_choice(tmp_path):
    adapter, spec, pool, home, brief = synthetic(tmp_path)
    runtime = tmp_path / "synthetic-runtime"
    seat_home = runtime / "seats" / spec.name / "home"
    seat_home.mkdir(parents=True)
    admin = seat_home / "requirements.toml"
    admin.write_text("synthetic admin state")
    spec = replace(spec, harness="fake", pools=(pool.name, "synthetic-other"), pool_choices={
        pool.name: {"harness": "grok", "model": "synthetic"},
        "synthetic-other": {"harness": "fake", "model": "synthetic"}})
    ship = SimpleNamespace(runtime=runtime, seats={spec.name: spec}, cwd_of=lambda selected: selected.cwd)
    failed, = (row for row in doctor.seat_readiness(ship) if not row.passed)
    assert failed.passed is False
    assert failed.name == f"seat {spec.name} pool {pool.name} readiness"
    assert str(admin) in failed.detail
    admin.unlink()
    assert all(row.passed for row in doctor.seat_readiness(ship))


def test_memory_configuration_and_card_keep_online_behavior_unproved(tmp_path):
    adapter, spec, pool, home, brief = synthetic(tmp_path)
    adapter.build_home(home, spec, pool, None)
    config = tomllib.loads((home / "config.toml").read_text())
    launch = adapter.launch(spec, pool, home, brief, None, None)
    assert config["memory"]["enabled"] is False
    assert config["memory_v2"] == {"enabled": False, "batch_dream_enabled": False}
    assert launch.env["GROK_MEMORY"] == "0"
    assert "--no-memory" in launch.argv
    card = (Path(__file__).parents[2] / "standing-orders" / "harnesses" / "grok.md").read_text()
    assert "unproved until online validation" in card


def test_requirements_are_not_written_or_adopted(tmp_path):
    adapter, spec, pool, home, brief = synthetic(tmp_path)
    adapter.build_home(home, spec, pool, None)
    path = home / "requirements.toml"
    assert not path.exists()
    adapter.preflight(home, spec.cwd)
    adapter.build_home(home, spec, pool, None)
    assert not path.exists()
    path.write_bytes(b"synthetic unexpected requirements")
    with pytest.raises(ShipError, match="unmanaged Grok admin configuration"):
        adapter.build_home(home, spec, pool, None)
    assert path.read_bytes() == b"synthetic unexpected requirements"
    path.unlink()
    target = tmp_path / "synthetic-requirements-copy"
    target.write_bytes(b"synthetic linked requirements")
    path.symlink_to(target)
    with pytest.raises(ShipError, match="unmanaged Grok admin configuration"):
        adapter.preflight(home, spec.cwd)
    assert path.is_symlink() and target.read_bytes() == b"synthetic linked requirements"


def test_telemetry_trace_and_local_storage_are_carried_by_launch_environment(tmp_path, monkeypatch):
    adapter, spec, pool, home, brief = synthetic(tmp_path)
    spec = replace(spec, settings={"relay": {"enabled": True}})
    adapter.build_home(home, spec, pool, None)
    config = tomllib.loads((home / "config.toml").read_text())
    assert config["features"]["telemetry"] is False
    assert config["telemetry"]["trace_upload"] is False
    assert config["relay"]["enabled"] is False
    launch = adapter.launch(spec, pool, home, brief, None, None)
    for key, value in {"GROK_TELEMETRY_ENABLED": "0", "GROK_TELEMETRY_TRACE_UPLOAD": "0",
                       "DISABLE_TELEMETRY": "1", "GROK_STORAGE_MODE": "local"}.items():
        assert launch.env[key] == value
        assert grok.private_environment(home)[key] == value
    assert "--storage-mode" not in launch.argv
    captured = []
    program = tmp_path / "grok-8.6.4-linux-x86_64"
    program.write_text("Synthetic selected file; never executed.\n")
    program.chmod(0o700)
    (tmp_path / "grok").symlink_to(program.name)
    from quarterdeck import seat as seat_module
    monkeypatch.setattr(seat_module, "program_path", lambda: str(tmp_path))
    monkeypatch.setattr(grok, "program_version", lambda argv: captured.append(argv) or "synthetic")
    version_home = tmp_path / "empty-version-home"
    version_home.mkdir()
    assert adapter.version(version_home) == "synthetic"
    assert {"GROK_STORAGE_MODE=local", "GROK_TELEMETRY_ENABLED=0", "GROK_TELEMETRY_TRACE_UPLOAD=0",
            "DISABLE_TELEMETRY=1", "GROK_DISABLE_EXPERIMENTS=1",
            f"HOME={version_home}", f"GROK_HOME={version_home}"} <= set(captured[0])


def test_experiments_are_disabled_in_controlled_invocation_output(tmp_path):
    adapter, spec, pool, home, brief = synthetic(tmp_path)
    adapter.build_home(home, spec, pool, None)
    launch = adapter.launch(spec, pool, home, brief, None, None)
    assert launch.env["GROK_DISABLE_EXPERIMENTS"] == "1"
    assert grok.private_environment(home)["GROK_DISABLE_EXPERIMENTS"] == "1"


def test_claude_advisory_policy_is_documented_without_refusal(tmp_path, monkeypatch):
    adapter, spec, pool, home, brief = synthetic(tmp_path)
    adapter.build_home(home, spec, pool, None)
    advisory = Path("/etc/claude-code/managed-settings.json")
    original = grok.present
    monkeypatch.setattr(grok, "present", lambda path: True if path == advisory else original(path))
    adapter.preflight(home, spec.cwd)
    assert doctor.pool_check(pool).passed is True
    card = (Path(__file__).parents[2] / "standing-orders" / "harnesses" / "grok.md").read_text()
    assert str(advisory) in card
    assert "only tighten MCP" in card
    assert "not refused or edited" in card
