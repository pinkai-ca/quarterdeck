"""Adopt launch settings through the CLI with synthetic records and captured screen forms."""

from datetime import datetime, timezone
from difflib import ndiff
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import cli, ship as shipmod, watch
from quarterdeck.harness.grok_models import EFFORT_REFUSAL, MODEL_REFUSAL
from quarterdeck.harness.synthetic_record import ClaudeShape
from quarterdeck.seat import Seat
from tests.antigravity_shapes import AntigravityShape

from .test_claude_streaming import answered_turn, append_rows, overload_error, resume_placeholder
from .test_grok_model_fallback import record as grok_record


FIXTURES = Path(__file__).parents[1] / "fixtures"
PROVIDERS = {"claude": "anthropic", "codex": "openai", "grok": "xai",
             "antigravity": "google", "pi": "local", "opencode": "local", "muse": "meta"}


@pytest.fixture
def adopt(drydock, monkeypatch, capsys):
    file = drydock / "ship.toml"

    def run(harness, *, record=None, screen=None, inline=False, effort_mode=None, native_files=None):
        text = file.read_text()
        table = "seats.worker-1"
        text = shipmod.edit(text, table, "harness", harness)
        pool = [{"pool": "observed", "harness": harness, "model": "inline-model"}] if inline else "observed"
        text = shipmod.edit(text, table, "pool", pool)
        if effort_mode:
            text = shipmod.edit(text, table, "effort_mode", effort_mode)
        text += f'\n[pools.observed]\nprovider = "{PROVIDERS[harness]}"\n'
        if harness in ("pi", "opencode"):
            text += 'endpoint = "http://127.0.0.1:8000/v1"\n'
        file.write_text(text)
        for name, data in (native_files or {}).items():
            path = drydock / "runtime/seats/worker-1/home" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data))

        def kept(seat):
            if inline:
                pytest.fail("inline refusal must precede a record read")
            return record

        def door(seat):
            if inline:
                pytest.fail("inline refusal must precede a screen read")
            return SimpleNamespace(screen=lambda: seat.harness.screen(screen) if screen is not None else None)

        monkeypatch.setattr(Seat, "record", kept)
        monkeypatch.setattr(watch, "Door", door)
        code = cli.main(["--ship", str(drydock), "set", "worker-1", "--adopt"])
        output = capsys.readouterr()
        return code, output, shipmod.load(drydock).seat("worker-1"), text, file.read_text()

    return run


def claude_result(tmp_path, label, version, *, trailing=""):
    record = ClaudeShape(tmp_path, tmp_path, "claude-adopt", "claude-opus-5-5")
    record.begin("Synthetic prompt.", record.model, "low")
    record.end("Synthetic reply.")
    suffix = ("for this session only" if version == "2.1.331"
              else "and saved as your default for new sessions")
    record.entry("user", dict(role="user", content=(
        f"<local-command-stdout>Set model to `{label}` {suffix}{trailing}</local-command-stdout>"
    )), version=version)
    record.entry("user", dict(role="user", content=(
        "<local-command-stdout>Set effort level to high</local-command-stdout>"
    )), version="2.1.317")
    return record.path


@pytest.mark.parametrize(("label", "native", "version"), [
    ("Haiku 5.5", "claude-haiku-5-5", "2.1.317"),
    ("Sonnet 5.5", "claude-sonnet-5-5", "2.1.331"),
    ("Opus 5.5", "claude-opus-5-5", "2.1.317"),
    ("Fable 5.2", "claude-fable-5-2", "2.1.331"),
])
def test_claude_display_result_adopts_model_and_effort(adopt, tmp_path, label, native, version):
    code, out, spec, before, after = adopt("claude", record=claude_result(tmp_path, label, version))
    assert code == 0 and not out.err
    assert (spec.model, spec.effort) == (native, "high")
    assert [line[2:] for line in ndiff(before.splitlines(), after.splitlines()) if line.startswith("+ ")] == [
        f'model = "{native}"', 'effort = "high"',
    ]


@pytest.mark.parametrize(("label", "version", "trailing"), [
    ("Unmeasured 7.0", "2.1.331", ""),
    ("Fable 5.2", "2.1.317", ""),
    ("claude-sonnet-5-5", "2.1.331", ""),  # command result is still a display
    ("Haiku 5.5", "2.1.331", "\nSet model to Opus 5.5 for this session only"),
])
def test_claude_refusal_is_one_line_and_withholds_the_effort(adopt, tmp_path, label, version, trailing):
    code, out, _, before, after = adopt("claude", record=claude_result(tmp_path, label, version, trailing=trailing))
    assert code == 2 and not out.out
    assert out.err.splitlines() == [f"qd: worker-1: cannot adopt model {label!r}. Use qd set worker-1 model=<launch-id>"]
    assert before == after


def test_claude_custom_picker_makes_a_known_label_ambiguous(adopt, tmp_path):
    code, out, _, before, after = adopt(
        "claude", record=claude_result(tmp_path, "Haiku 5.5", "2.1.331"),
        native_files={"settings.json": {"modelPicker": {"custom-id": "Haiku 5.5"}}},
    )
    assert code == 2 and not out.out and before == after
    assert "cannot adopt model 'Haiku 5.5'" in out.err


def test_claude_catalog_with_two_ids_for_one_name_refuses(adopt, tmp_path):
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    catalog = {"version": 2, "fetchedAt": now - 1000, "staleAt": now + 3600000,
               "catalog": {"config": {"models": [
                   {"id": "claude-haiku-5-5", "name": "Haiku 5.5"},
                   {"id": "different-native-id", "name": "Haiku 5.5"},
               ]}}}
    code, out, _, before, after = adopt(
        "claude", record=claude_result(tmp_path, "Haiku 5.5", "2.1.331"),
        native_files={"cache/model-catalog/synthetic.json": catalog},
    )
    assert code == 2 and not out.out and before == after
    assert "cannot adopt model 'Haiku 5.5'" in out.err


def test_claude_keeps_literal_native_assistant_model(adopt, tmp_path):
    native = "vendor/Model_42"
    record = ClaudeShape(tmp_path, tmp_path, "claude-literal", native)
    record.begin("Synthetic prompt.", native, "low")
    record.end("Synthetic reply.")
    code, out, spec, _, _ = adopt("claude", record=record.path)
    assert code == 0 and not out.err
    assert (spec.model, spec.effort) == (native, "low")


@pytest.mark.parametrize("placeholder", [resume_placeholder, overload_error], ids=["resume", "api-error"])
@pytest.mark.parametrize("selection", ["native", "display"])
def test_claude_measured_placeholder_preserves_adoption(adopt, tmp_path, placeholder, selection):
    native = "claude-fable-5-2"
    if selection == "display":
        record = claude_result(tmp_path, "Fable 5.2", "2.1.331")
    else:
        record = tmp_path / "synthetic.jsonl"
        append_rows(record, *answered_turn(native))
    append_rows(record, dict(placeholder(), sessionId=record.stem))

    code, out, spec, _, _ = adopt("claude", record=record)

    assert code == 0 and not out.err and spec.model == native


def test_claude_unmeasured_placeholder_refuses_atomically(adopt, tmp_path):
    record = tmp_path / "synthetic.jsonl"
    append_rows(record, *answered_turn(), dict(resume_placeholder(), version="2.1.332"))

    code, out, _, before, after = adopt("claude", record=record, screen="  ○ low · /effort\n")

    assert code == 2 and not out.out and before == after
    assert out.err.splitlines() == ["qd: worker-1: cannot adopt model '<synthetic>'. Use qd set worker-1 model=<launch-id>"]


def test_claude_measured_placeholder_alone_allows_effort_only(adopt, tmp_path):
    record = tmp_path / "synthetic.jsonl"
    append_rows(record, resume_placeholder())

    code, out, spec, _, _ = adopt("claude", record=record, screen="  ○ low · /effort\n")

    assert code == 0 and not out.err and (spec.model, spec.effort) == ("fake-1", "low")


def test_effort_only_adoption_names_effort_and_leaves_model(adopt):
    code, out, spec, _, _ = adopt("claude", screen="  ○ low · /effort\n", effort_mode="program-default")
    assert code == 0 and not out.err
    assert (spec.model, spec.effort, spec.effort_mode) == ("fake-1", "low", "named")


def test_inline_choice_refuses_before_observing_the_program(adopt):
    code, out, spec, before, after = adopt("claude", inline=True)
    assert code == 2 and not out.out and before == after
    assert out.err.startswith("qd: inline pool choices: --adopt is unavailable;")


def test_codex_title_case_footer_reuses_the_native_identity(adopt):
    screen = (FIXTURES / "codex/0.166.0/idle.txt").read_text().replace("GPT-6-Astra", "GPT-6.1-Sol")
    code, out, spec, _, _ = adopt("codex", screen=screen)
    assert code == 0 and not out.err and spec.model == "gpt-6.1-sol"


def test_codex_unknown_footer_token_refuses(adopt):
    screen = (FIXTURES / "codex/0.166.0/idle.txt").read_text().replace("GPT-6-Astra", "Default")
    code, out, _, before, after = adopt("codex", screen=screen)
    assert code == 2 and not out.out and before == after
    assert "cannot adopt model 'Default'" in out.err


@pytest.mark.parametrize(("label", "native"), [
    ("Gemini 3.8 Flash (Low)", "gemini-3.8-flash-low"),
    ("Gemini 4 Argon (Low)", "gemini-4-argon-low"),
    ("Claude Sonnet 5.5 (Low)", "claude-sonnet-5-5-low"),
    ("gemini-3.1-pro-high", "gemini-3.1-pro-high"),
])
def test_antigravity_record_model_uses_launch_spelling(adopt, tmp_path, label, native):
    record = AntigravityShape(tmp_path, tmp_path, "agy-adopt", label)
    record.begin("Synthetic prompt.", label, "low")
    record.end("Synthetic reply.")
    screen = (FIXTURES / "antigravity/1.5.5/idle-argon-medium-200.txt").read_text()
    code, out, spec, _, _ = adopt("antigravity", record=record.path, screen=screen)
    assert code == 0 and not out.err and spec.model == native


def test_antigravity_record_family_cannot_borrow_the_footers_tier(adopt, tmp_path):
    label = "Gemini 3.8 Flash"
    record = AntigravityShape(tmp_path, tmp_path, "agy-adopt", label)
    record.begin("Synthetic prompt.", label, "low")
    record.end("Synthetic reply.")
    screen = (FIXTURES / "antigravity/1.5.5/idle-notice-80.txt").read_text()
    code, out, _, before, after = adopt("antigravity", record=record.path, screen=screen)
    assert code == 2 and not out.out and before == after
    assert "cannot adopt model 'Gemini 3.8 Flash'" in out.err


@pytest.mark.parametrize(("tier", "expected"), [("low", "gemini-3.8-flash-low"), ("medium", None)])
def test_antigravity_footer_needs_a_measured_family_and_tier(adopt, tier, expected):
    screen = (FIXTURES / "antigravity/1.5.5/idle-notice-80.txt").read_text()
    screen = screen.replace("Gemini 3.8 Flash · low", f"Gemini 3.8 Flash · {tier}")
    code, out, spec, before, after = adopt("antigravity", screen=screen)
    if expected:
        assert code == 0 and not out.err and (spec.model, spec.effort) == (expected, tier)
    else:
        assert code == 2 and not out.out and before == after
        assert "cannot adopt model 'Gemini 3.8 Flash'" in out.err


def grok_screen(name="idle-mono.json", *, warnings=False):
    rows = json.loads((FIXTURES / "grok/1.0.77" / name).read_text())["lines"]
    if warnings:
        rows[:0] = ["     " + EFFORT_REFUSAL, "", "     " + MODEL_REFUSAL, ""]
    return "\n".join(rows)


def test_grok_subscription_footer_label_refuses_even_with_an_effort(adopt):
    code, out, _, before, after = adopt("grok", screen=grok_screen(warnings=True))
    assert code == 2 and not out.out and before == after
    assert "cannot adopt model 'Grok 4.7'" in out.err


def test_grok_config_option_label_cannot_reuse_an_older_record_id(adopt, tmp_path):
    record = grok_record(tmp_path)
    update = {"sessionUpdate": "config_option_update",
              "configOptions": [{"id": "model", "currentValue": "Grok 4.7 Fast"}]}
    with record.open("a") as stream:
        stream.write(json.dumps({"timestamp": 2208988802, "method": "_x.ai/session/update",
                                 "params": {"sessionId": "fictional-model-example", "update": update}}) + "\n")
    code, out, _, before, after = adopt("grok", record=record, screen=grok_screen())
    assert code == 2 and not out.out and before == after
    assert "cannot adopt model 'Grok 4.7 Fast'" in out.err


def test_grok_native_record_wins_and_adopts_only_settings(adopt, tmp_path):
    code, out, spec, before, after = adopt(
        "grok", record=grok_record(tmp_path), screen=grok_screen(warnings=True),
    )
    assert code == 0 and not out.err
    assert (spec.model, spec.effort) == ("grok-4.7", "high")
    assert [line[2:] for line in ndiff(before.splitlines(), after.splitlines()) if line.startswith("+ ")] == [
        'model = "grok-4.7"', 'effort = "high"',
    ]


def test_grok_key_footer_exposes_a_native_id(adopt):
    code, out, spec, _, _ = adopt("grok", screen=grok_screen("xai-key-startup-80-plain.json"))
    assert code == 0 and not out.err
    assert (spec.model, spec.effort) == ("grok-4.20-0309-non-reasoning", None)


@pytest.mark.parametrize(("name", "native"), [
    ("idle-256", "sample-model"), ("xai-key-after-turn-80", "grok-4.20-0309-non-reasoning"),
])
def test_pi_footer_exposes_native_id_even_when_the_effort_is_clipped(adopt, name, native):
    code, out, spec, _, _ = adopt("pi", screen=(FIXTURES / f"pi/0.79.0/{name}.txt").read_text())
    assert code == 0 and not out.err and spec.model == native


def test_pi_footer_without_room_may_only_show_the_start_of_the_id(adopt):
    screen = (FIXTURES / "pi/0.79.0/prefix-does-not-fit-52.txt").read_text()
    footer = screen.rstrip().splitlines()[-1]
    stats = footer.split("  ", 1)[0]
    screen = screen.replace(footer, stats + "  endpoint-mode")
    code, out, _, before, after = adopt("pi", screen=screen)
    assert code == 2 and not out.out and before == after
    assert "cannot adopt model 'endpoint-mode'" in out.err
