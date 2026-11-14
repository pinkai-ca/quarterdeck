"""Ordinary pure configuration output; no native or operator-isolation proof."""

from copy import deepcopy

import pytest

from quarterdeck.harness.muse_home import configuration, environment
from quarterdeck.harness.muse_program import binary
from quarterdeck.ship import ShipError


def test_explicit_model_is_seeded_and_source_settings_are_not_mutated():
    template = dict(model="synthetic-template-model", tui=dict(theme="synthetic-theme"))
    overrides = dict(model="synthetic-settings-model", context=dict(synthetic_option=True))
    original = deepcopy((template, overrides))
    settings = configuration("synthetic-seat-model", "high", template, overrides)
    assert settings["model"] == "synthetic-seat-model"
    assert settings["reasoning_effort"] == "high" and settings["schema_version"] == 1
    assert settings["provider"] == "meta"
    assert settings["tui"] == dict(theme="synthetic-theme")
    assert settings["context"] == dict(synthetic_option=True, foreign_personal_rules=False,
                                       foreign_personal_skills=False)
    assert (template, overrides) == original


def test_declared_settings_model_can_supply_the_seed_without_a_native_default():
    assert configuration(None, None, dict(model="synthetic-model"), {})["model"] == "synthetic-model"
    with pytest.raises(ShipError, match="explicit noncontributor model"):
        configuration(None, None, {}, {})


@pytest.mark.parametrize("model,template,overrides", [
    ("synthetic-contributor", {}, {}),
    ("synthetic-model", dict(model="synthetic-contributor"), {}),
    ("synthetic-model", {}, dict(model="synthetic-contributor")),
])
def test_contributor_choice_is_a_configuration_error_in_each_declared_layer(model, template, overrides):
    with pytest.raises(ShipError, match="contributor model is not allowed"):
        configuration(model, None, template, overrides)


def test_environment_is_a_pure_plan_without_an_unproved_auth_override(tmp_path):
    home = tmp_path / "synthetic-home"
    values = environment(home)
    assert set(values) == {"HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
                           "XDG_STATE_HOME", "MUSE_NO_AUTO_UPDATE", "TMPDIR"}
    assert values["HOME"] == str(home / "user-home")
    assert values["XDG_CONFIG_HOME"] == str(home / "config")
    assert values["XDG_DATA_HOME"] == str(home / "data")
    assert values["TMPDIR"] == str(home / "tmp")
    assert values["MUSE_NO_AUTO_UPDATE"] == "1"
    assert not home.exists()


def test_install_marker_selects_versioned_executable_as_file_data(tmp_path):
    launcher = tmp_path / "muse"
    native = tmp_path / "muse-bin-0.0.1-R1.1"
    for item in (launcher, native):
        item.write_text("synthetic executable placeholder")
        item.chmod(0o755)
    (tmp_path / ".muse-version").write_text("0.0.1-R1.1\n")
    assert binary(str(launcher)) == native
