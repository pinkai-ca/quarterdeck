"""Ordinary model/template/registry wiring; no native or mask boundary proof."""

import json
from pathlib import Path

import pytest

from quarterdeck.harness import get, native_name
from quarterdeck.harness.muse import MuseHarness
from quarterdeck.providers import canonical
from quarterdeck.ship import Pool, SeatSpec, ShipError


def test_registry_routes_the_legacy_program_spelling_to_its_company():
    assert canonical("muse") == "meta" and native_name("meta") == "muse"
    assert isinstance(get("muse"), MuseHarness)


def test_no_like_seat_uses_one_explicit_ship_template_model_in_settings_and_argv(tmp_path, monkeypatch):
    harness = MuseHarness()
    # This case proves the template model reaches settings and argv. The binary
    # is not executed; isolation of the home is a separate gate.
    monkeypatch.setattr("quarterdeck.harness.muse_program.binary",
                        lambda **kwargs: Path("/synthetic/bin/muse-bin-1.4.20-R1.1"))
    template = tmp_path / "template"
    template.mkdir()
    (template / "settings.json").write_text(json.dumps(dict(provider="echo", model="synthetic-catalog-model")))
    spec = SeatSpec("synthetic-seat", "synthetic-session", "worker", "muse", ("synthetic-pool",),
                    cwd=tmp_path, model=None)
    pool = Pool("synthetic-pool", "meta", config_dir=tmp_path / "unused-pool")
    home = tmp_path / "home"
    harness.build_home(home, spec, pool, template)
    settings = json.loads((home / "config/muse/settings.json").read_text())
    brief = tmp_path / "brief.txt"
    brief.write_text("A fictional brief.")
    launch = harness.launch(spec, pool, home, brief, None, "A fictional open message.")
    assert settings["model"] == launch.argv[launch.argv.index("--model") + 1] == "synthetic-catalog-model"
    assert settings["provider"] == launch.argv[launch.argv.index("--provider") + 1] == "echo"
    assert settings["contributor_mode_enabled"] is False
    assert launch.argv[launch.argv.index("--contributor-mode") + 1] == "off"
    opening = "A fictional brief.\n\nA fictional open message."
    assert launch.deferred_opening == opening
    assert opening not in launch.argv and "/usr/bin/bwrap" not in launch.argv
    assert harness.opening_prompt_turn and launch.env["TMPDIR"] == str(home / "tmp")


def test_no_like_seat_with_no_model_refuses_before_any_home_or_startup(tmp_path):
    harness = MuseHarness()
    spec = SeatSpec("synthetic-seat", "synthetic-session", "worker", "muse", ("synthetic-pool",), cwd=tmp_path)
    pool = Pool("synthetic-pool", "meta", config_dir=tmp_path / "unused-pool")
    home = tmp_path / "uncreated-home"
    with pytest.raises(ShipError, match="explicit noncontributor model"):
        harness.build_home(home, spec, pool, None)
    assert not home.exists()
