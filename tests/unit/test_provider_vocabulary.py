"""Company providers, terminal harnesses and compatible declaration spellings."""

import pytest

from quarterdeck import harness
from quarterdeck.ship import Pool, ShipError, load

PAIRS = [("anthropic", "claude"), ("openai", "codex"), ("google", "antigravity"), ("synthetic", "fake")]


def declaration(tmp_path, provider, program, *, key=False, endpoint=False):
    text = f'''[ship]
name="synthetic"
[pools.paid]
provider="{provider}"
'''
    if key:
        text += 'api_key="env:SYNTHETIC_KEY"\n'
    if endpoint:
        text += 'endpoint="http://synthetic.invalid/v1"\n'
    text += f'[sessions.crew]\n[seats.worker]\nsession="crew"\nharness="{program}"\npool="paid"\n'
    (tmp_path/"ship.toml").write_text(text)
    return load(tmp_path)


@pytest.mark.parametrize("company,program", PAIRS)
def test_native_company_declaration_keeps_the_terminal_harness(tmp_path, company, program):
    ship = declaration(tmp_path, company, program)
    assert ship.pool("paid").provider == company and ship.seat("worker").harness == program


@pytest.mark.parametrize("company,program", PAIRS)
def test_legacy_native_spellings_are_read_as_company_aliases(tmp_path, company, program):
    ship = declaration(tmp_path, program, program)
    assert ship.pool("paid").provider == company


@pytest.mark.parametrize("company,program", PAIRS)
def test_native_provider_fact_belongs_to_the_adapter(company, program):
    assert harness.get(program).native_provider == company


@pytest.mark.parametrize("program", ["opencode", "pi", "codex"])
def test_anthropic_subscription_refusal_names_its_native_harness(tmp_path, program):
    with pytest.raises(ShipError, match="native harness claude"):
        declaration(tmp_path, "anthropic", program)


@pytest.mark.parametrize("program", ["opencode", "pi"])
def test_open_harness_requires_an_endpoint_for_a_key_and_preserves_operator_identity(tmp_path, program):
    with pytest.raises(ShipError, match="endpoint"):
        declaration(tmp_path, "anthropic", program, key=True)
    ship = declaration(tmp_path, "synthetic-operator", program, key=True, endpoint=True)
    assert ship.pool("paid").provider == "synthetic-operator"


@pytest.mark.parametrize("company,program", PAIRS)
def test_native_harness_takes_its_own_company_key(tmp_path, company, program):
    assert declaration(tmp_path, company, program, key=True).pool("paid").kind == "key"


@pytest.mark.parametrize("spelling,expected", [(" Anthropic ", "anthropic"), ("OPENAI", "openai"),
    ("Google", "google"), ("x.ai", "xai"), ("Meta", "meta"), ("Z.ai", "zai"),
    ("open-router", "openrouter"), (" private-operator ", "private-operator")])
def test_company_normalization_does_not_infer_endpoint_operator_from_model(spelling, expected):
    assert Pool("remote", spelling, endpoint="https://synthetic.invalid/v1").provider == expected


@pytest.mark.parametrize("program", ["opencode", "pi"])
def test_open_harnesses_have_no_native_company(program):
    assert harness.get(program).native_provider is None


@pytest.mark.parametrize("program", ["opencode", "pi"])
def test_an_unused_open_program_is_not_a_subscription_provider(tmp_path, program):
    (tmp_path / "ship.toml").write_text(f'[ship]\nname="synthetic"\n[pools.unused]\nprovider="{program}"\n')
    with pytest.raises(ShipError, match="unsupported subscription provider"):
        load(tmp_path)


@pytest.mark.parametrize("company,program", PAIRS)
def test_native_harness_refuses_other_company_key_and_endpoints(tmp_path, company, program):
    other = "openai" if company != "openai" else "anthropic"
    with pytest.raises(ShipError, match="requires its own provider's key"):
        declaration(tmp_path, other, program, key=True)
    with pytest.raises(ShipError, match="endpoint operated by"):
        declaration(tmp_path, company, program, key=True, endpoint=True)


def test_legacy_input_doctor_names_exact_edit_without_changing_declaration(tmp_path, monkeypatch):
    from quarterdeck import doctor
    from quarterdeck.drill import Check
    ship = declaration(tmp_path, "claude", "claude")
    before = ship.file.read_bytes()
    monkeypatch.setattr(doctor, "framework_check", lambda s: Check("framework", True, "synthetic"))
    monkeypatch.setattr(doctor, "program_version", lambda a: "synthetic")
    monkeypatch.setattr(doctor.shutil, "which", lambda *a, **k: None)
    guidance = next(c for c in doctor.checks(ship) if c.name == "provider spelling paid")
    assert guidance.passed
    assert 'in [pools.paid] change provider = "claude" to provider = "anthropic"' in guidance.detail
    assert ship.file.read_bytes() == before


@pytest.mark.parametrize("verb", ["up", "restart", "fresh", "failover_to"])
def test_invalid_subscription_never_retires_an_existing_launch(tmp_path, monkeypatch, verb):
    from quarterdeck.seat import Launched, Seat
    from quarterdeck.ship import SeatSpec, Session, Ship
    from types import SimpleNamespace
    from quarterdeck import post
    spec = SeatSpec("worker", "crew", "worker", "codex", ("bad",))
    ship = Ship(tmp_path, "synthetic", "operator", tmp_path, {"bad": Pool("bad", "anthropic")},
                {"crew": Session("crew", "crew")}, {"worker": spec})
    seat = Seat(ship, spec, SimpleNamespace())  # historical readings stay possible
    launch = Launched("%1", 101, post.now().isoformat(), "bad", None, ["codex"], "synthetic", "codex")
    seat.folder.mkdir(parents=True)
    launch.write(seat.folder / "launch.json")
    monkeypatch.setattr(seat, "_down", lambda: pytest.fail("refusal retired the existing launch"))
    args = ("bad",) if verb == "failover_to" else ()
    with pytest.raises(ShipError, match="native harness claude"):
        getattr(seat, verb)(*args)
    assert seat.launched() == launch
