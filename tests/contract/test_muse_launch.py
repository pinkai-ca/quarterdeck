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
    # Replace mask/native planning dependencies: this case proves only the
    # ordinary check's model=None template route, not filesystem isolation.
    monkeypatch.setattr(harness, "preflight", lambda *args: None)
    monkeypatch.setattr("quarterdeck.harness.muse_mask.prepare", lambda *args: None)
    monkeypatch.setattr("quarterdeck.harness.muse_mask.command", lambda home, cwd, argv, **kwargs: argv)
    monkeypatch.setattr("quarterdeck.harness.muse_program.binary", lambda **kwargs: Path("/synthetic/muse-binary"))
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
    assert launch.argv[-1] == "A fictional brief.\n\nA fictional open message."
    assert harness.opening_prompt_turn and launch.env["TMPDIR"] == str(home / "mask/tmp")


def test_no_like_seat_with_no_model_refuses_before_any_home_or_startup(tmp_path):
    harness = MuseHarness()
    spec = SeatSpec("synthetic-seat", "synthetic-session", "worker", "muse", ("synthetic-pool",), cwd=tmp_path)
    pool = Pool("synthetic-pool", "meta", config_dir=tmp_path / "unused-pool")
    home = tmp_path / "uncreated-home"
    with pytest.raises(ShipError, match="explicit noncontributor model"):
        harness.build_home(home, spec, pool, None)
    assert not home.exists()
