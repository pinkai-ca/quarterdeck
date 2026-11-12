"""Ordinary pure configuration output; no native or operator-isolation proof."""

from copy import deepcopy

import pytest

from quarterdeck.harness.muse_home import configuration, environment
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


def test_environment_is_a_pure_plan_with_one_canonical_auth_path(tmp_path):
    home = tmp_path / "synthetic-home"
    values = environment(home)
    assert values["MUSE_AUTH_PATH"] == str(home / "auth.json")
    assert values["XDG_CONFIG_HOME"] == str(home / "config")
    assert values["XDG_DATA_HOME"] == str(home / "data")
    assert values["MUSE_NO_AUTO_UPDATE"] == "1" and values["MUSE_LOGIN"] == "0"
    assert not home.exists()
