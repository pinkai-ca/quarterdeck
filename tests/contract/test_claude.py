"""Claude Code 2.1.317 against the screens saved from the spike of 7 November 2026."""

from __future__ import annotations

import json
from pathlib import Path

from quarterdeck.harness.base import State
from quarterdeck.harness.claude import ClaudeHarness, same_model
from quarterdeck.ship import Pool, SeatSpec

from tests.conftest import FIXTURES

VERSION = "2.1.317"
SCREENS = FIXTURES / "claude" / VERSION


def screen(name: str):
    return ClaudeHarness().screen((SCREENS / f"{name}.txt").read_text())


def test_every_fixture_set_has_the_screens_the_adapter_reads():
    names = {p.stem for p in SCREENS.glob("*.txt")}
    assert {"start", "paste-3-lines-shown", "paste-4-lines-folded", "working-queued", "switch-model-dialog",
            "effort-set", "interrupted"} <= names


def test_the_start_screen_is_an_open_door():
    s = screen("start")
    assert s.box == "" and not s.working and s.interruption is None and s.unknown is None
    assert s.shows == {}, "the mode line names the effort only once it was set in the session"


def test_a_short_paste_shows_in_full_and_a_long_one_folds():
    harness = ClaudeHarness()
    three = ("qd > order from desk · ord-7f3a · Spike\nAnswer MARKER-LOADED if you read a CLAUDE.md marker, else NO-MARKER. "
             "Then run exactly: qd --version\nqd > reply: qd post report --re ord-7f3a · inbox: post/inbox/captain.md")
    shown = screen("paste-3-lines-shown")
    assert harness.box(three).pattern is None and harness.box(three).matches(shown.box), shown.box
    four = "qd > order from desk · ord-9abc · Four\na\nb\nqd > reply: qd post report --re ord-9abc"
    folded = screen("paste-4-lines-folded")
    assert folded.box == "[Pasted text #3 +3 lines]"
    assert harness.box(four).matches(folded.box) and not harness.box(four).matches("[Pasted text #3 +4 lines]")


def test_a_working_seat_with_a_queued_message():
    s = screen("working-queued")
    assert s.working and s.queued == 1 and s.box == "", "the placeholder is not text in the box"
    assert s.interruption is None


def test_the_switch_model_confirmation_is_known_and_dismissable():
    s = screen("switch-model-dialog")
    assert s.interruption is not None and s.interruption.name == "switch-model confirmation" and s.interruption.key == "Enter"


def test_effort_shows_on_the_mode_line_and_model_in_the_record():
    harness = ClaudeHarness()
    s = screen("effort-set")
    assert s.shows == {"effort": "medium"} and not s.working
    effort = harness.control("effort")
    assert harness.confirms(effort, "medium", s, State()) is True
    assert harness.confirms(effort, "high", s, State()) is False
    model = harness.control("model")
    confirmed = State(local_stdout="Set model to `Sonnet 5.5` and saved as your default for new sessions")
    assert harness.confirms(model, "claude-sonnet-5-5", s, confirmed) is True
    assert harness.confirms(model, "claude-haiku-5-5", s, confirmed) is False
    assert harness.confirms(model, "claude-haiku-5-5", s, State()) is None, "not readable yet: pending, not false"
    assert same_model("Haiku 5.5", "claude-haiku-5-5") and not same_model("Opus 5.5", "claude-haiku-5-5")


def test_an_interrupted_seat_is_idle_with_an_open_door():
    s = screen("interrupted")
    assert not s.working and s.box == "" and s.interruption is None


def test_the_launch_carries_our_files_and_nothing_of_the_users(tmp_path: Path):
    harness = ClaudeHarness()
    pool_dir = tmp_path / "pool"
    pool_dir.mkdir()
    (pool_dir / ".credentials.json").write_text("{}")
    pool = Pool("claude-secondary", "claude", config_dir=pool_dir)
    seat = SeatSpec("pike", "bridge", "captain", "claude", ("claude-secondary",), model="claude-opus-5-5", effort="max",
                    persona="Pike", cwd=tmp_path / "work", settings={"autoCompactWindow": "auto"})
    home = tmp_path / "runtime" / "seats" / "pike" / "home"
    credential = harness.build_home(home, seat, pool, None)
    assert credential.link == (home / ".credentials.json", pool_dir / ".credentials.json")
    assert (home / ".credentials.json").is_symlink(), "shared by one link, never a copy"
    settings = json.loads((home / "settings.json").read_text())
    assert settings["promptSuggestionEnabled"] is False and settings["env"]["DISABLE_AUTOUPDATER"] == "1"
    assert settings["autoCompactWindow"] == "auto", "the seat's own keys pass through verbatim"
    assert "Bash(qd *)" in settings["permissions"]["allow"]
    state = json.loads((home / ".claude.json").read_text())
    assert state["hasCompletedOnboarding"] and state["projects"][str(tmp_path / "work")]["hasTrustDialogAccepted"]
    brief = tmp_path / "brief.md"
    launch = harness.launch(seat, pool, home, brief, None, "qd > briefed as pike")
    assert launch.argv == ("claude", "--setting-sources", "user", "--append-system-prompt-file", str(brief), "--name", "Pike",
                           "--model", "claude-opus-5-5", "--effort", "max", "qd > briefed as pike")
    assert launch.env == {"CLAUDE_CONFIG_DIR": str(home)}
    resumed = harness.launch(seat, pool, home, brief, "abc-123", None)
    assert resumed.argv[-2:] == ("--resume", "abc-123")


def test_a_key_pool_reaches_the_program_by_environment(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    pool = Pool("anthropic-api", "claude", api_key="env:ANTHROPIC_API_KEY")
    seat = SeatSpec("w", "crew", "worker", "claude", ("anthropic-api",))
    home = tmp_path / "home"
    credential = ClaudeHarness().build_home(home, seat, pool, None)
    assert credential.variable == "ANTHROPIC_API_KEY" and not (home / ".credentials.json").exists()
    assert ClaudeHarness().launch(seat, pool, home, tmp_path / "b.md", None, None).env["ANTHROPIC_API_KEY"] == "sk-test"
