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
    assert all(item.passed for item in result)
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
