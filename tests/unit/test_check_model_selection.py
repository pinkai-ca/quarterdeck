"""Ordinary requested-model selection scaffolding; all launches and probes are local fakes."""

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from quarterdeck import check
from quarterdeck.cli import build_parser
from quarterdeck.drill import Check
from quarterdeck.harness.antigravity import AntigravityHarness
from quarterdeck.harness.base import Screen
from quarterdeck.harness.codex import CodexHarness
from quarterdeck.ship import Pool, SeatSpec


def test_model_flag_is_general_to_supported_programs():
    parser = build_parser()
    for program in ("antigravity", "claude", "codex", "grok", "pi", "opencode"):
        args = parser.parse_args(["check", program, "--model", "model-synth"])
        assert args.harness == program
        assert args.model == "model-synth"
    assert parser.parse_args(["check", "antigravity"]).model is None


def fake_check(monkeypatch, tmp_path, *, model=None, like=None):
    pool = Pool("pool-synth", "google")
    ship = SimpleNamespace(pools={pool.name: pool}, seats={like.name: like} if like else {},
                           workspace=tmp_path, runtime=tmp_path)
    specs = []
    probes = []

    @contextmanager
    def temporary(ship, tmux, spec, orders, purpose, **_):
        specs.append(spec)
        yield SimpleNamespace(ship=ship, spec=spec)

    monkeypatch.setattr(check, "temporary_seat", temporary)
    monkeypatch.setattr(check, "_version_in_runtime", lambda harness, runtime: "version-synth")
    monkeypatch.setattr(check.secrets, "token_hex", lambda count: "run-synth")

    def probe(ship, seat, orders, **kwargs):
        probes.append(kwargs)
        return [Check("synthetic probe", True)]

    monkeypatch.setattr(check, "probe", probe)
    # The installed-program and waking-line steps main runs after the probe are not what these tests read.
    monkeypatch.setattr(check, "program_where", lambda harness: None)
    monkeypatch.setattr(check, "probe_wake", lambda seat, orders: [Check("synthetic wake", True)])
    result = check.check(ship, "antigravity", object(), model=model)
    assert all(item.passed is not False for item in result)  # an inapplicable step is no failure
    return ship, specs[0], probes[0]


def test_requested_model_selects_temporary_seat_without_a_like_seat(monkeypatch, tmp_path):
    ship, spec, probe = fake_check(monkeypatch, tmp_path, model="flash-synth")
    assert spec.model == "flash-synth"
    assert spec.pools == ("pool-synth",)
    assert ship.seats == {}
    assert probe == {"requested_model": "flash-synth"}


def test_requested_model_overrides_like_model_without_mutating_it(monkeypatch, tmp_path):
    like = SeatSpec("like-synth", "session-synth", "worker", "antigravity", ("pool-synth",), model="argon-synth")
    ship, spec, probe = fake_check(monkeypatch, tmp_path, model="flash-synth", like=like)
    assert spec.model == "flash-synth"
    assert ship.seats[like.name].model == "argon-synth"
    assert probe["requested_model"] == "flash-synth"


def test_omitted_flag_preserves_like_model_selection(monkeypatch, tmp_path):
    like = SeatSpec("like-synth", "session-synth", "worker", "antigravity", ("pool-synth",), model="argon-synth")
    _, spec, probe = fake_check(monkeypatch, tmp_path, like=like)
    assert spec.model == "argon-synth"
    assert probe == {}


def test_requested_model_is_confirmed_record_first(monkeypatch):
    harness = CodexHarness()
    harness.state = lambda record: SimpleNamespace(model="model-synth")
    seat = SimpleNamespace(harness=harness, spec=SimpleNamespace(model="model-synth"),
                           record=lambda: Path("record-synth"))
    monkeypatch.setattr(check, "Door", lambda seat: SimpleNamespace(screen=lambda: Screen("", None, shows={})))
    result = check.model_confirmation(seat, "model-synth", require_record=True)
    assert result.passed
    assert "requested model-synth" in result.detail
    assert "launched model-synth" in result.detail
    assert "record-observed model-synth" in result.detail


def test_startup_screen_can_confirm_before_the_record_model(monkeypatch):
    seat = SimpleNamespace(harness=AntigravityHarness(), spec=SimpleNamespace(model="flash-synth"), record=lambda: None)
    monkeypatch.setattr(check, "Door", lambda seat: SimpleNamespace(screen=lambda: Screen("", None, shows={"model": "flash-synth"})))
    result = check.model_confirmation(seat, "flash-synth")
    assert result.passed
    assert "screen-observed flash-synth" in result.detail


def test_a_requested_model_not_confirmed_records_the_model_reason(monkeypatch):
    from quarterdeck import check_outcome
    seat = SimpleNamespace(record=lambda: None, harness=SimpleNamespace(
        model_matches=lambda requested, observed: requested == observed), spec=SimpleNamespace(model="flash-synth"))
    monkeypatch.setattr(check, "Door", lambda seat: SimpleNamespace(screen=lambda: Screen("", None, shows={"model": "other-synth"})))
    result = check.model_confirmation(seat, "flash-synth")
    assert result.passed is False and result.reason == "model", "a failed confirmation is classified, not unclassified"
    assert check_outcome.event("failed", "1.0", result.reason)["step"] == "model"


# Antigravity 1.5.5: the record names "Gemini 3.8 Flash (Low)"; the footer shows "Gemini 3.8 Flash · low".
FIXTURES_155 = Path(__file__).parents[1] / "fixtures/antigravity/1.5.5"


def antigravity_seat(monkeypatch, recorded, screen):
    harness = AntigravityHarness()
    harness.state = lambda record: SimpleNamespace(model=recorded)
    seat = SimpleNamespace(harness=harness, spec=SimpleNamespace(model="gemini-3.8-flash-low"),
                           record=lambda: Path("record-synth") if recorded else None)
    monkeypatch.setattr(check, "Door", lambda seat: SimpleNamespace(screen=lambda: screen))
    return seat


def test_the_record_confirms_the_tier_the_measured_footer_leaves_out(monkeypatch):
    screen = AntigravityHarness().screen((FIXTURES_155 / "idle-notice-80.txt").read_text())
    assert screen.shows == {"model": "Gemini 3.8 Flash", "effort": "low"}
    result = check.model_confirmation(antigravity_seat(monkeypatch, "Gemini 3.8 Flash (Low)", screen), "gemini-3.8-flash-low")
    assert result.passed, result.detail
    assert "record-observed Gemini 3.8 Flash (Low)" in result.detail and "screen-observed Gemini 3.8 Flash" in result.detail


def test_a_footer_family_without_effort_is_consistent_with_the_record(monkeypatch):
    screen = Screen("", "", shows={"model": "Gemini 3.8 Flash"})
    assert check.model_confirmation(antigravity_seat(monkeypatch, "Gemini 3.8 Flash (Low)", screen), "gemini-3.8-flash-low").passed


def test_the_footer_family_alone_never_confirms_the_tier(monkeypatch):
    screen = Screen("", "", shows={"model": "Gemini 3.8 Flash", "effort": "low"})
    result = check.model_confirmation(antigravity_seat(monkeypatch, None, screen), "gemini-3.8-flash-low")
    assert result.passed is False and result.reason == "model"
    assert check.model_confirmation(antigravity_seat(monkeypatch, None, screen), "gemini-3.8-flash-low",
                                    require_record=True).passed is False


def test_a_footer_naming_another_model_or_tier_contradicts_the_record(monkeypatch):
    argon = AntigravityHarness().screen((FIXTURES_155 / "idle-argon-medium-200.txt").read_text())
    assert argon.shows == {"model": "Gemini 4 Argon", "effort": "medium"}
    high = Screen("", "", shows={"model": "Gemini 3.8 Flash", "effort": "high"})
    for screen in (argon, high, Screen("", "", shows={"model": "Gemini 3.8 Flash (High)"})):
        result = check.model_confirmation(antigravity_seat(monkeypatch, "Gemini 3.8 Flash (Low)", screen), "gemini-3.8-flash-low")
        assert result.passed is False, screen.shows


def test_screen_consistency_rule_for_antigravity_and_the_exact_default():
    from quarterdeck.harness import antigravity_models as models
    agy = AntigravityHarness()
    assert agy.screen_model_consistent("gemini-3.8-flash-low", "Gemini 3.8 Flash (Low)") is True
    assert agy.screen_model_consistent("gemini-3.8-flash-low", "Gemini 3.8 Flash", "low") is True
    assert agy.screen_model_consistent("gemini-3.8-flash-low", "Gemini 3.8 Flash", "LOW") is True
    assert agy.screen_model_consistent("gemini-3.8-flash-low", "Gemini 3.8 Flash") is True
    assert agy.screen_model_consistent("gemini-3.8-flash-low", "Gemini 3.8 Flash", "medium") is False
    assert agy.screen_model_consistent("gemini-3.8-flash-low", "Gemini 4 Argon", "low") is False
    assert agy.screen_model_consistent("gemini-4-argon-low", "Gemini 4 Argon", "low") is True
    assert agy.screen_model_consistent("model-synth", "other-synth") is False, "no measured family: another name contradicts"
    assert agy.screen_model_consistent("model-synth", None) is None
    assert models.same_model("gemini-3.8-flash-low", "Gemini 3.8 Flash") is not True, "a family is not the model"
    codex = CodexHarness()
    assert codex.screen_model_consistent("model-synth", "model-synth") is True
    assert codex.screen_model_consistent("model-synth", "model") is False
