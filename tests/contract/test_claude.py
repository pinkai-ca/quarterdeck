"""Claude Code 2.1.317 against the screens saved from the spike of 7 November 2026."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from quarterdeck.harness.base import State, same_name
from quarterdeck.harness.claude import ClaudeHarness
from quarterdeck.ship import Pool, SeatSpec

from tests.conftest import FIXTURES

VERSION = "2.1.317"
SCREENS = FIXTURES / "claude" / VERSION


def screen(name: str):
    return ClaudeHarness().screen((SCREENS / f"{name}.txt").read_text())


def test_every_fixture_set_has_the_screens_the_adapter_reads():
    names = {p.stem for p in SCREENS.glob("*.txt")}
    assert {"start", "paste-3-lines-shown", "paste-4-lines-folded", "paste-long-one-line-folded", "working-queued",
            "switch-model-dialog", "effort-set", "interrupted"} <= names


def test_the_start_screen_is_an_open_door():
    s = screen("start")
    assert s.box == "" and not s.working and s.interruption is None and s.unknown is None
    assert s.shows == {}, "the mode line names the effort only once it was set in the session"


def test_a_short_paste_shows_in_full_and_a_long_one_folds():
    harness = ClaudeHarness()
    three = ("qd > order from desk · ord-7f3a · Spike\nAnswer MARKER-LOADED if you read a CLAUDE.md marker, else NO-MARKER. "
             "Then run exactly: qd --version\nqd > reply: qd post report --re ord-7f3a · inbox: post/inbox/captain.md")
    shown = screen("paste-3-lines-shown")
    # This scrubbed screen retains composer rows but no terminal wrap metadata.
    # Supply its observed text-area bound explicitly; bare text cannot prove a wrap.
    assert not harness.box(three).matches(shown.box)
    assert harness.box(three).matches(shown.box, 76), shown.box
    four = "qd > order from desk · ord-9abc · Four\na\nb\nqd > reply: qd post report --re ord-9abc"
    folded = screen("paste-4-lines-folded")
    assert folded.box == "[Pasted text #3 +3 lines]"
    assert harness.box(four).matches(folded.box) and not harness.box(four).matches("[Pasted text #3 +4 lines]")


def test_a_long_paste_folds_however_few_its_lines():
    """Probed 7 November 2026: 1,981 characters on one line fold to a placeholder with no line count at all."""
    harness = ClaudeHarness()
    one_line = "qd probe A: " + " ".join(f"word{i}" for i in range(260))
    assert len(one_line) == 1981
    folded = screen("paste-long-one-line-folded")
    assert folded.box == "[Pasted text #6]" and harness.box(one_line).matches(folded.box)
    assert harness.box(one_line + "\n").matches("[Pasted text #5 +1 lines]"), "a trailing newline is one line"
    assert not harness.box(one_line + "\n").matches(folded.box) and not harness.box(one_line).matches("[Pasted text #6 +1 lines]")
    assert harness.box("short").matches("short") and not harness.box("short").matches("[Pasted text #1 +2 lines]")


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
    confirmed = State(model="Sonnet 5.5")  # what the record's `Set model to …` line leaves behind
    assert harness.confirms(model, "claude-sonnet-5-5", s, confirmed) is True
    assert harness.confirms(model, "claude-haiku-5-5", s, confirmed) is False
    assert harness.confirms(model, "claude-haiku-5-5", s, State()) is None, "not readable yet: pending, not false"
    assert same_name("Haiku 5.5", "claude-haiku-5-5") and not same_name("Opus 5.5", "claude-haiku-5-5")
    assert same_name("GPT-6-Astra", "gpt-6-astra")


def test_prose_on_the_screen_is_never_a_dialog_while_the_box_is_there():
    start = (SCREENS / "start.txt").read_text()
    s = ClaudeHarness().screen("Switch model?\nDo you want to proceed?\n" + start)
    assert s.box == "" and s.interruption is None, "a dialog replaces the box; words in the transcript do not"
    lines = start.split("\n")
    box_at = next(i for i, l in enumerate(lines) if l.startswith("❯"))
    lines[box_at] = "❯ Switch model? is what a human may type"
    s = ClaudeHarness().screen("\n".join(lines))
    assert s.box == "Switch model? is what a human may type" and s.interruption is None


def test_trust_covers_the_inherited_workspace(tmp_path: Path):
    from dataclasses import replace

    from quarterdeck import ship as shipmod
    from tests.conftest import FIXTURES as F

    folder = tmp_path / "enterprise"
    folder.mkdir()
    (folder / "ship.toml").write_text((F / "ships" / "enterprise.toml").read_text())
    ship = shipmod.load(folder)
    pike = ship.seat("pike")
    assert pike.cwd is None, "pike inherits the ship's workspace"
    resolved = replace(pike, cwd=ship.cwd_of(pike))  # what Seat.up hands the adapter
    pool_dir = tmp_path / "pool"
    pool_dir.mkdir()
    (pool_dir / ".credentials.json").write_text("{}")
    home = tmp_path / "home"
    ClaudeHarness().build_home(home, resolved, Pool("claude-secondary", "claude", config_dir=pool_dir), None)
    state = json.loads((home / ".claude.json").read_text())
    assert state["projects"][str(ship.workspace)]["hasTrustDialogAccepted"]


def test_a_ship_template_and_seat_settings_keep_what_the_launch_relies_on(tmp_path: Path):
    ship_home = tmp_path / "homes" / "claude"
    ship_home.mkdir(parents=True)
    (ship_home / "settings.json").write_text(json.dumps({"theme": "light", "permissions": {"allow": ["Bash(git *)"]}}))
    pool_dir = tmp_path / "pool"
    pool_dir.mkdir()
    (pool_dir / ".credentials.json").write_text("{}")
    seat = SeatSpec("w", "crew", "worker", "claude", ("p",), cwd=tmp_path, settings={"permissions": {"allow": ["Bash(make *)"]}})
    home = tmp_path / "home"
    ClaudeHarness().build_home(home, seat, Pool("p", "claude", config_dir=pool_dir), ship_home)
    settings = json.loads((home / "settings.json").read_text())
    assert settings["theme"] == "light" and settings["promptSuggestionEnabled"] is False
    assert {"Bash(qd *)", "Bash(git *)", "Bash(make *)"} <= set(settings["permissions"]["allow"])  # law: can-post


def test_an_interrupted_seat_is_idle_with_an_open_door():
    s = screen("interrupted")
    assert not s.working and s.box == "" and s.interruption is None


def test_the_launch_carries_our_files_and_nothing_of_the_users(tmp_path: Path):
    harness = ClaudeHarness()
    pool_dir = tmp_path / "pool"
    pool_dir.mkdir()
    (pool_dir / ".credentials.json").write_text("{}")
    (pool_dir / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "acc", "organizationUuid": "org"}, "theme": "light"}))
    pool = Pool("claude-secondary", "claude", config_dir=pool_dir)
    seat = SeatSpec("pike", "bridge", "captain", "claude", ("claude-secondary",), model="claude-opus-5-5", effort="max",
                    persona="Pike", cwd=tmp_path / "work", settings={"autoCompactWindow": "auto"})
    home = tmp_path / "runtime" / "seats" / "pike" / "home"
    credential = harness.build_home(home, seat, pool, None)
    assert credential.link == (home / ".credentials.json", pool_dir / ".credentials.json")
    assert (home / ".credentials.json").is_symlink(), "shared by one link, never a copy"
    settings = json.loads((home / "settings.json").read_text())
    assert settings["promptSuggestionEnabled"] is False and settings["env"]["DISABLE_AUTOUPDATER"] == "1"
    assert settings["permissions"]["defaultMode"] == "bypassPermissions", "no approval prompts unless a ship asks for a sandbox"
    assert settings["skipDangerousModePermissionPrompt"] is True, "and no dialog asking a person to accept that, either"
    assert settings["autoMemoryEnabled"] is False, "nothing of the seat's own survives a conversation but posts and ship files"
    assert settings["autoCompactWindow"] == "auto", "the seat's own keys pass through verbatim"
    assert "Bash(qd *)" in settings["permissions"]["allow"]
    state = json.loads((home / ".claude.json").read_text())
    assert state["hasCompletedOnboarding"] and state["projects"][str(tmp_path / "work")]["hasTrustDialogAccepted"]
    assert state["bypassPermissionsModeAccepted"], "the dialog that asks a person to accept bypass mode is answered before the first launch"
    assert state["oauthAccount"] == {"accountUuid": "acc", "organizationUuid": "org"} and state["theme"] == "dark", \
        "the pool's account record, nothing else of its state"
    brief = tmp_path / "brief.md"
    launch = harness.launch(seat, pool, home, brief, None, "qd > briefed as pike")
    assert launch.argv == ("claude", "--setting-sources", "user", "--append-system-prompt-file", str(brief), "--name", "Pike",
                           "--model", "claude-opus-5-5", "--effort", "max", "qd > briefed as pike")
    assert launch.env == {"CLAUDE_CONFIG_DIR": str(home)}
    resumed = harness.launch(seat, pool, home, brief, "abc-123", None)
    assert resumed.argv[-2:] == ("--resume", "abc-123")
    reachable = harness.launch(replace(seat, remote_control=True), pool, home, brief, None, "qd > briefed as pike")
    assert reachable.argv[7:9] == ("--remote-control", "Pike") and reachable.argv[-1] == "qd > briefed as pike"


def test_a_move_to_another_pool_replaces_the_link_and_copies_no_token(tmp_path: Path):
    harness = ClaudeHarness()
    pools = {}
    for name in ("a", "b"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"refreshToken": name}}))
        pools[name] = Pool(f"pool-{name}", "claude", config_dir=folder)
    seat = SeatSpec("pike", "bridge", "captain", "claude", ("pool-a", "pool-b"), cwd=tmp_path)
    home = tmp_path / "home"
    harness.build_home(home, seat, pools["a"], None)
    link = home / ".credentials.json"
    link.unlink()
    link.write_text(json.dumps({"claudeAiOauth": {"refreshToken": "a2"}}))  # a refresh detached it
    harness.build_home(home, seat, pools["b"], None)  # the seat moves to pool b
    assert link.is_symlink() and link.resolve() == (tmp_path / "b" / ".credentials.json").resolve()
    assert json.loads((tmp_path / "b" / ".credentials.json").read_text())["claudeAiOauth"]["refreshToken"] == "b", "nothing of pool a reached pool b"


def test_a_key_pool_reaches_the_program_by_environment(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    pool = Pool("anthropic-api", "claude", api_key="env:ANTHROPIC_API_KEY")
    seat = SeatSpec("w", "crew", "worker", "claude", ("anthropic-api",))
    home = tmp_path / "home"
    credential = ClaudeHarness().build_home(home, seat, pool, None)
    assert credential.variable == "ANTHROPIC_API_KEY" and not (home / ".credentials.json").exists()
    assert ClaudeHarness().launch(seat, pool, home, tmp_path / "b.md", None, None).env["ANTHROPIC_API_KEY"] == "sk-test"
