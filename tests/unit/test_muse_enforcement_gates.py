"""Contributor mode is seeded off, and a contributor model never reaches a launch.

No native Muse runs. A private version home is the empty directory version()
prepares before it asks the binary for --version; the binary here is a stub
that records that the file was already written.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quarterdeck import check as checkmod
from quarterdeck.harness.muse import MuseHarness
from quarterdeck.harness import muse_home
from quarterdeck.ship import Pool, SeatSpec, ShipError


@pytest.fixture
def binary(monkeypatch):
    monkeypatch.setattr("quarterdeck.harness.muse_program.binary",
                        lambda **kwargs: Path("/synthetic/bin/muse-bin-1.4.20-R1.1"))


def test_every_built_home_is_seeded_off_at_the_measured_place(tmp_path, binary):
    template = tmp_path / "template"
    template.mkdir()
    (template / "settings.json").write_text(json.dumps(dict(
        provider="echo", model="synthetic-catalog-model", contributor_mode_enabled=True)))
    spec = SeatSpec("synthetic-seat", "synthetic-session", "worker", "muse", ("synthetic-pool",),
                    cwd=tmp_path, model="synthetic-catalog-model",
                    settings=dict(contributor_mode_enabled=True))
    pool = Pool("synthetic-pool", "meta", config_dir=tmp_path / "unused")
    home = tmp_path / "home"
    MuseHarness().build_home(home, spec, pool, template)
    settings = json.loads((home / "config/muse/settings.json").read_text())
    assert settings["contributor_mode_enabled"] is False
    assert settings["schema_version"] == 1
    assert muse_home.seeded_settings(home)["model"] == "synthetic-catalog-model"


def test_a_private_version_home_is_written_off_before_the_program_is_asked(tmp_path, binary, monkeypatch):
    home = tmp_path / "version-home"
    home.mkdir(mode=0o700)
    seen = {}

    def record(argv):
        seen["argv"] = tuple(argv)
        seen["settings"] = json.loads((home / "config/muse/settings.json").read_text())
        return "1.4.20"

    monkeypatch.setattr("quarterdeck.harness.muse.program_version", record)
    assert MuseHarness().version(home) == "1.4.20"
    assert seen["settings"] == dict(schema_version=1, contributor_mode_enabled=False)
    assert seen["argv"][-2:] == ("/synthetic/bin/muse-bin-1.4.20-R1.1", "--version")
    assert f"HOME={home / 'user-home'}" in seen["argv"]
    assert "muse-spark" not in seen["argv"] and "--model" not in seen["argv"]
    assert not (home / "auth.json").exists()


@pytest.mark.parametrize("model", ["muse-spark", "muse-spark-1.0", "MUSE-SPARK", "synthetic-model-contributor"])
def test_a_contributor_model_is_refused_before_a_home_or_a_check(tmp_path, binary, model):
    harness = MuseHarness()
    reason = harness.check_model_reason(model)
    assert reason and "contributor model" in reason and "no launch" in reason
    spec = SeatSpec("synthetic-seat", "synthetic-session", "worker", "muse", ("synthetic-pool",),
                    cwd=tmp_path, model=model)
    pool = Pool("synthetic-pool", "meta", config_dir=tmp_path / "unused")
    home = tmp_path / "uncreated-home"
    with pytest.raises(ShipError, match="contributor model is not allowed"):
        harness.build_home(home, spec, pool, None)
    assert not home.exists()
    checks, version = checkmod._check(None, "muse", None, harness, model)
    assert version is None and checks[0].passed is False
    assert "contributor model" in checks[0].detail and "no launch" in checks[0].detail


def test_wonder_is_not_refused_and_a_planted_spark_settings_file_never_launches(tmp_path, binary):
    harness = MuseHarness()
    assert harness.check_model_reason(None) is None
    assert harness.check_model_reason("muse-wonder-1.0") is None
    template = tmp_path / "template"
    template.mkdir()
    (template / "settings.json").write_text(json.dumps(dict(provider="echo", model="synthetic-catalog-model")))
    spec = SeatSpec("synthetic-seat", "synthetic-session", "worker", "muse", ("synthetic-pool",),
                    cwd=tmp_path, model="synthetic-catalog-model")
    pool = Pool("synthetic-pool", "meta", config_dir=tmp_path / "unused")
    home = tmp_path / "home"
    harness.build_home(home, spec, pool, template)
    path = home / "config/muse/settings.json"
    planted = json.loads(path.read_text())
    planted["model"] = "muse-spark"
    path.write_text(json.dumps(planted))
    brief = tmp_path / "brief.txt"
    brief.write_text("A fictional brief.")
    with pytest.raises(ShipError, match="contributor model is not allowed"):
        harness.launch(spec, pool, home, brief, None, None)
