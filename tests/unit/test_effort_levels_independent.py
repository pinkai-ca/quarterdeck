"""Independent functional specifications; no native program or account is needed.

Fixture vocabularies below belong only to a synthetic harness. They are not
measurements of any native program, version, model, or account.
"""

from dataclasses import replace
import json
import subprocess
import tomllib

import pytest

from quarterdeck import doctor, endpoint, harness, seat as seatmod, ship as shipmod
from quarterdeck.ledger import Count
from quarterdeck.harness import base, codex, grok
from quarterdeck.harness.capabilities import EffortCapability, validate_effort, validate_selection
from quarterdeck.watch import SeatReading, render_full, render_table


@pytest.mark.parametrize('failure', [PermissionError('template unreadable'), FileNotFoundError('template disappeared')])
def test_doctor_template_read_error_is_a_failed_row(tmp_path, monkeypatch, failure):
    from types import SimpleNamespace

    selected = shipmod.SeatSpec('sample', 'crew', 'worker', 'fake', ('synthetic-pool',))
    proxy = SimpleNamespace(folder=tmp_path, runtime=tmp_path / 'runtime',
                            seats={selected.name: selected}, cwd_of=lambda spec: tmp_path)

    def unreadable(*args, **kwargs):
        raise failure

    monkeypatch.setattr(type(harness.get('fake')), 'check_defaults', unreadable)
    failed, = (row for row in doctor.seat_readiness(proxy) if not row.passed)
    assert failed.name == 'seat sample pool synthetic-pool readiness'
    assert failed.detail == str(failure)


@pytest.mark.parametrize('mode', ['model_mode', 'effort_mode'])
def test_doctor_program_default_needs_template_home_provenance(tmp_path, mode):
    from types import SimpleNamespace

    selected = shipmod.SeatSpec('sample', 'crew', 'worker', 'fake', ('synthetic-pool',),
                                **{mode: 'program-default'})
    proxy = SimpleNamespace(runtime=tmp_path / 'runtime', seats={selected.name: selected},
                            cwd_of=lambda spec: tmp_path)
    failed, = (row for row in doctor.seat_readiness(proxy) if not row.passed)
    assert failed.name == 'seat sample pool synthetic-pool readiness'
    assert 'ship folder unavailable' in failed.detail


PI_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh")
UNMEASURED = ("claude", "codex", "grok", "antigravity")
DEFAULT_LABEL = "program's default (not checked)"


def spec_for(name, **changes):
    return replace(shipmod.SeatSpec("worker-example", "crew-example", "worker", name,
                                    ("pool-example",), model="model-example"), **changes)


def write_ship(folder, name, fields="", *, pools=None):
    provider = harness.get(name).native_provider or "synthetic"
    pool_fields = ('provider = "synthetic"\nendpoint = "http://127.0.0.1:1/v1"'
                   if name in ("pi", "opencode") else f'provider = "{provider}"')
    declaration = f'''[ship]
name = "ship-example"
workspace = "."
[sessions.crew-example]
title = "Crew example"
[pools.pool-example]
{pool_fields}
[pools.backup-example]
{pool_fields}
[seats.worker-example]
session = "crew-example"
harness = "{name}"
pool = {pools or '["pool-example"]'}
{fields}
'''
    (folder / "ship.toml").write_text(declaration)
    return folder


@pytest.fixture
def no_programs(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("a declaration/capability gate tried to observe a native program or endpoint")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(base, "program_version", forbidden)
    monkeypatch.setattr(base, "_version_in_runtime", forbidden)
    monkeypatch.setattr(endpoint, "choose", forbidden)
    return forbidden


@pytest.mark.parametrize("effort", PI_LEVELS)
def test_load_accepts_every_complete_pi_level_without_observation(tmp_path, no_programs, effort):
    ship = shipmod.load(write_ship(tmp_path, "pi", f'model = "model-example"\neffort = "{effort}"'))
    assert ship.seat("worker-example").effort == effort
    assert not ship.runtime.exists()


@pytest.mark.parametrize("name,effort,levels", [
    ("opencode", "max", ()),
    ("opencode", "default", ()),
    ("pi", "max", PI_LEVELS),
    ("pi", "ultra", PI_LEVELS),
    ("pi", "default", PI_LEVELS),
])
def test_load_refuses_only_known_incompatibility_by_name(tmp_path, no_programs, name, effort, levels):
    with pytest.raises(shipmod.ShipError) as caught:
        shipmod.load(write_ship(tmp_path, name, f'model = "model-example"\neffort = "{effort}"'))
    text = str(caught.value)
    assert name in text and repr(effort) in text and "pool-example" in text
    assert all(level in text for level in levels)
    if not levels:
        assert "takes no effort" in text
    assert not (tmp_path / "runtime").exists()


@pytest.mark.parametrize("name", UNMEASURED)
def test_load_keeps_unmeasured_effort_and_legacy_model_strings(tmp_path, no_programs, name):
    ship = shipmod.load(write_ship(tmp_path, name, 'model = "default"\neffort = "max"'))
    selected = ship.seat("worker-example")
    assert (selected.model, selected.effort) == ("default", "max")
    assert selected.model_mode is None and selected.effort_mode is None
    assert harness.get(name).effort_capability().kind == "unknown"
    assert not ship.runtime.exists()


@pytest.mark.parametrize("name", harness.NAMES)
def test_effort_program_default_is_valid_on_every_adapter(tmp_path, no_programs, name):
    ship = shipmod.load(write_ship(tmp_path, name,
                                 'model = "model-example"\neffort_mode = "program-default"'))
    selected = ship.seat("worker-example")
    assert selected.effort is None and selected.effort_mode == "program-default"
    assert validate_selection(selected, harness.get(name), installed=True, version="unmeasured-example") is None


@pytest.mark.parametrize("name", ("fake", "claude", "codex", "grok", "antigravity"))
def test_supported_model_default_loads_with_no_named_model(tmp_path, no_programs, name):
    ship = shipmod.load(write_ship(tmp_path, name,
                                 'model_mode = "program-default"\neffort_mode = "program-default"'))
    assert ship.seat("worker-example").model is None


@pytest.mark.parametrize("name", ("pi", "opencode"))
def test_endpoint_adapter_requires_explicit_model(tmp_path, no_programs, name):
    with pytest.raises(shipmod.ShipError, match="requires an explicit endpoint model"):
        shipmod.load(write_ship(tmp_path, name, 'model_mode = "program-default"'))


@pytest.mark.parametrize("key", ("model", "effort"))
def test_named_string_is_never_reinterpreted_as_program_default(tmp_path, no_programs, key):
    fields = f'{key} = "default"\n{key}_mode = "program-default"'
    with pytest.raises(shipmod.ShipError, match=f"{key} absent"):
        shipmod.load(write_ship(tmp_path, "fake", fields))


@pytest.mark.parametrize("key", ("model", "effort"))
def test_explicit_named_mode_requires_its_string(tmp_path, no_programs, key):
    with pytest.raises(shipmod.ShipError, match=f"nonempty {key}"):
        shipmod.load(write_ship(tmp_path, "fake", f'{key}_mode = "named"'))


@pytest.mark.parametrize("backup,effort", [("pi", "max"), ("opencode", "high")])
def test_pure_load_validates_inactive_declared_pool_choices(tmp_path, no_programs, backup, effort):
    choices = ('[{pool="pool-example", harness="fake", model="model-example"}, '
               f'{{pool="backup-example", harness="{backup}", model="model-example", effort="{effort}"}}]')
    with pytest.raises(shipmod.ShipError) as caught:
        shipmod.load(write_ship(tmp_path, "fake", pools=choices))
    assert "backup-example" in str(caught.value) and backup in str(caught.value)


@pytest.fixture
def fixture_vocabulary(monkeypatch):
    reader = harness.get("fake")
    reader.efforts = EffortCapability("levels", ("fixture-light", "fixture-deep"),
                                     ("fixture-version",), complete=False)
    original = harness.get
    monkeypatch.setattr(harness, "get", lambda name: reader if name == "fake" else original(name))
    return reader


@pytest.mark.parametrize("effort", ("fixture-light", "fixture-deep"))
def test_exact_measured_version_allows_each_fixture_level(fixture_vocabulary, effort):
    assert validate_effort(fixture_vocabulary, effort, version="fixture-version", installed=True) is None


def test_exact_measured_refusal_names_entire_fixture_vocabulary(fixture_vocabulary):
    with pytest.raises(shipmod.ShipError) as caught:
        validate_effort(fixture_vocabulary, "max", version="fixture-version", installed=True)
    assert "'max'" in str(caught.value)
    assert "fixture-light, fixture-deep" in str(caught.value)


def test_version_specific_vocabulary_does_not_refuse_at_pure_load(tmp_path, no_programs, fixture_vocabulary):
    ship = shipmod.load(write_ship(tmp_path, "fake", 'effort = "max"'))
    assert ship.seat("worker-example").effort == "max"


@pytest.mark.parametrize("version", (None, "other-version"))
def test_unmeasured_version_passes_even_outside_historical_tuple(fixture_vocabulary, version):
    note = validate_effort(fixture_vocabulary, "max", version=version, installed=True)
    assert "not measured" in note and "'max' unchecked" in note
    assert (version or "version unknown") in note


@pytest.mark.parametrize("name,version", [
    ("claude", "2.1.317"), ("codex", "0.166.0"),
    ("grok", "1.0.77"), ("antigravity", "1.5.5"),
])
def test_native_model_suggestions_do_not_prove_a_complete_program_vocabulary(name, version):
    reader = harness.get(name)
    facts = reader.effort_capability(version)
    assert facts.kind == "unknown" and facts.measured is False and not facts.complete
    note = validate_effort(reader, "max", version=version, installed=True)
    assert reader.program in note
    assert f"{version} not measured; 'max' unchecked" in note


def isolated_readiness(monkeypatch, reader):
    monkeypatch.setattr(reader, "preflight", lambda *args: None)
    monkeypatch.setattr(reader, "workspace_notice", lambda *args: None)
    original = harness.get
    monkeypatch.setattr(harness, "get", lambda name: reader if name == reader.name else original(name))


@pytest.mark.parametrize("name", UNMEASURED)
@pytest.mark.parametrize("version", (None, "unmeasured-example"))
def test_doctor_has_a_passing_visible_unchecked_note(tmp_path, no_programs, monkeypatch, name, version):
    ship = shipmod.load(write_ship(tmp_path, name, 'model = "model-example"\neffort = "max"'))
    reader = harness.get(name)
    isolated_readiness(monkeypatch, reader)
    rows = doctor.seat_readiness(ship, versions={name: version})
    assert len(rows) == 1 and rows[0].passed is True
    assert "'max' unchecked" in rows[0].detail and "not measured" in rows[0].detail
    assert (version or "version unknown") in rows[0].detail


@pytest.mark.parametrize("effort,passed", [("fixture-deep", True), ("max", False)])
def test_doctor_uses_exact_version_facts(tmp_path, no_programs, monkeypatch, fixture_vocabulary, effort, passed):
    ship = shipmod.load(write_ship(tmp_path, "fake", f'effort = "{effort}"'))
    isolated_readiness(monkeypatch, fixture_vocabulary)
    rows = doctor.seat_readiness(ship, versions={"fake": "fixture-version"})
    assert len(rows) == 1 and rows[0].passed is passed
    if not passed:
        assert "fixture-light, fixture-deep" in rows[0].detail and "'max'" in rows[0].detail


def test_doctor_checks_every_choice_including_a_measured_invalid_backup(
        tmp_path, no_programs, monkeypatch, fixture_vocabulary):
    choices = ('[{pool="pool-example", harness="fake", model="model-example", effort="fixture-light"}, '
               '{pool="backup-example", harness="fake", model="model-example", effort="max"}]')
    ship = shipmod.load(write_ship(tmp_path, "fake", pools=choices))
    isolated_readiness(monkeypatch, fixture_vocabulary)
    rows = doctor.seat_readiness(ship, versions={"fake": "fixture-version"})
    assert [(r.name, r.passed) for r in rows] == [
        ("seat worker-example pool pool-example effort", True),
        ("seat worker-example pool backup-example readiness", False),
    ]
    assert "fixture-light, fixture-deep" in rows[1].detail


@pytest.mark.parametrize("name", UNMEASURED)
@pytest.mark.parametrize("version", (None, "unmeasured-example"))
def test_launch_selection_returns_unchecked_note_without_translating(
        tmp_path, no_programs, monkeypatch, name, version):
    ship = shipmod.load(write_ship(tmp_path, name, 'model = "model-example"\neffort = "max"'))
    seat = seatmod.Seat(ship, ship.seat("worker-example"), tmux=None)
    monkeypatch.setattr(seatmod, "_version_in_runtime", lambda *args: version)
    observed_version, note = seat._observe_selection()
    assert observed_version == version and "'max' unchecked" in note
    assert (version or "version unknown") in note
    assert "not measured" in note and seat.spec.effort == "max"
    assert not ship.runtime.exists()


def test_launch_selection_passes_a_measured_allowed_level(tmp_path, no_programs, monkeypatch, fixture_vocabulary):
    ship = shipmod.load(write_ship(tmp_path, "fake", 'effort = "fixture-deep"'))
    seat = seatmod.Seat(ship, ship.seat("worker-example"), tmux=None)
    monkeypatch.setattr(seatmod, "_version_in_runtime", lambda *args: "fixture-version")
    assert seat._observe_selection() == ("fixture-version", None)
    assert not ship.runtime.exists()


def test_launch_selection_uses_measured_version_refusal(tmp_path, no_programs, monkeypatch, fixture_vocabulary):
    ship = shipmod.load(write_ship(tmp_path, "fake", 'effort = "max"'))
    seat = seatmod.Seat(ship, ship.seat("worker-example"), tmux=None)
    monkeypatch.setattr(seatmod, "_version_in_runtime", lambda *args: "fixture-version")
    with pytest.raises(shipmod.ShipError, match="fixture-light, fixture-deep"):
        seat._observe_selection()
    assert not ship.runtime.exists()


def launch_arguments(tmp_path, monkeypatch, name, selected):
    reader = harness.get(name)
    home = tmp_path / "home-example"
    home.mkdir()
    brief = tmp_path / "brief-example.txt"
    brief.write_text("Synthetic ordinary launch argument fixture.\n")
    if name == "grok":
        monkeypatch.setattr(reader, "preflight", lambda *args: None)
        monkeypatch.setattr(reader, "workspace_notice", lambda *args: None)
        monkeypatch.setattr(grok, "launch_home", lambda *args: None)
        monkeypatch.setattr(grok, "private_environment", lambda *args: {})
        monkeypatch.setattr(grok.grok_workspace, "launch_args", lambda *args: [])
    if name == "antigravity":
        monkeypatch.setattr(reader, "_conversations", lambda: [])
    # Subscription fixtures have no credential directory and name no key variable.
    pool = shipmod.Pool("pool-example", "synthetic")
    return reader.launch(selected, pool, home, brief, None, None).argv


@pytest.mark.parametrize("name,model_flag,effort_flag", [
    ("fake", "--model", "--effort"),
    ("claude", "--model", "--effort"),
    ("codex", "-m", "model_reasoning_effort="),
    ("grok", "--model", "--reasoning-effort"),
    ("antigravity", "--model", "--effort"),
])
def test_program_default_really_omits_launch_selectors(
        tmp_path, no_programs, monkeypatch, name, model_flag, effort_flag):
    selected = spec_for(name, model=None, effort=None,
                        model_mode="program-default", effort_mode="program-default")
    validate_selection(selected, harness.get(name))
    argv = launch_arguments(tmp_path, monkeypatch, name, selected)
    assert model_flag not in argv
    assert not any(arg.startswith(effort_flag) for arg in argv)
    assert "program-default" not in argv


@pytest.mark.parametrize("name,model_flag,effort_flag", [
    ("fake", "--model", "--effort"),
    ("claude", "--model", "--effort"),
    ("codex", "-m", None),
    ("grok", "--model", "--reasoning-effort"),
    ("antigravity", "--model", "--effort"),
])
def test_legacy_launch_keeps_literal_default_model_and_max_effort(
        tmp_path, no_programs, monkeypatch, name, model_flag, effort_flag):
    selected = spec_for(name, model="default", effort="max")
    argv = launch_arguments(tmp_path, monkeypatch, name, selected)
    assert argv[argv.index(model_flag) + 1] == "default"
    if effort_flag:
        assert argv[argv.index(effort_flag) + 1] == "max"
    else:
        assert 'model_reasoning_effort="max"' in argv


@pytest.mark.parametrize("default", (False, True))
def test_codex_generated_config_preserves_legacy_fallback_or_omits_default(
        tmp_path, no_programs, monkeypatch, default):
    reader = harness.get("codex")
    monkeypatch.setattr(codex, "credential", lambda *args: None)
    selected = spec_for("codex", model=None, effort=None,
                        model_mode="program-default" if default else None,
                        effort_mode="program-default" if default else None)
    home = tmp_path / "runtime" / "seats" / "worker-example" / "home"
    reader.build_home(home, selected, shipmod.Pool("pool-example", "openai"), None)
    config = tomllib.loads((home / "config.toml").read_text())
    if default:
        assert "model" not in config and "model_reasoning_effort" not in config
    else:
        assert config["model"] == codex.DEFAULT_MODEL
        assert config["model_reasoning_effort"] == codex.DEFAULT_EFFORT


@pytest.mark.parametrize("default", (False, True))
def test_pi_default_omits_selectors_while_named_effort_is_unchanged(
        tmp_path, no_programs, monkeypatch, default):
    reader = harness.get("pi")
    home = tmp_path / "home-example"
    choice = endpoint.Choice("pool-example", "model-example", 8192, 1024)
    monkeypatch.setattr(reader, "endpoint_home", lambda *args: (choice, {}))
    monkeypatch.setattr("quarterdeck.harness.pi_thinking.launch_evidence", lambda *args: None)
    selected = spec_for("pi", effort=None if default else "xhigh",
                        effort_mode="program-default" if default else None)
    pool = shipmod.Pool("pool-example", "synthetic", endpoint="http://127.0.0.1:1/v1")
    reader.build_home(home, selected, pool, None)
    config = json.loads((home / "agent" / "models.json").read_text())
    model = config["providers"][pool.name]["models"][0]
    if default:
        assert "reasoning" not in model
    else:
        assert model["reasoning"] is True
    brief = tmp_path / "brief-example.txt"
    brief.write_text("Synthetic ordinary launch argument fixture.\n")
    argv = reader.launch(selected, pool, home, brief, None, None).argv
    if default:
        assert "--thinking" not in argv
    else:
        assert argv[argv.index("--thinking") + 1] == "xhigh"
    assert argv[argv.index("--model") + 1] == "model-example"


@pytest.mark.parametrize("observed", (False, True))
def test_status_and_watch_properties_label_program_default_as_unchecked(observed):
    row = SeatReading("worker-example", "idle", "unknown", None, Count(),
                      "pool-example", "observed-model" if observed else None, "fake",
                      effort="observed-effort" if observed else None,
                      model_mode="program-default", effort_mode="program-default")
    suffix = f" (ship file: {DEFAULT_LABEL})"
    assert row.model_text == ("observed-model" + suffix if observed else DEFAULT_LABEL)
    assert row.effort_text == ("observed-effort" + suffix if observed else DEFAULT_LABEL)
    table = "\n".join(render_table(dict(service="example", seats=[row], open_orders=[], usage=[])))
    assert table.count(DEFAULT_LABEL) == 2
    full = "\n".join(render_full(dict(seat=row, record=None, state=None, record_absent=None,
                                      record_limits=None, screen=None, usage=[])))
    assert full.count(DEFAULT_LABEL) == 2


def test_legacy_missing_observation_does_not_rename_literal_default():
    row = SeatReading("worker-example", "idle", "unknown", None, None, "pool-example", None, "fake",
                      declared_model="default", declared_effort="default")
    assert row.model_text == "- (ship file: default)"
    assert row.effort_text == "- (ship file: default)"
