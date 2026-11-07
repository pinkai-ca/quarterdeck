"""Antigravity 1.5.2 against the screens saved from the spike of 7 November 2026, best effort by design."""

from __future__ import annotations

import json
from pathlib import Path

from quarterdeck.door import echoed
from quarterdeck.harness.antigravity import AntigravityHarness
from quarterdeck.harness.base import same_name
from quarterdeck.ship import Pool, SeatSpec

from tests.conftest import FIXTURES

VERSION = "1.5.2"
SCREENS = FIXTURES / "antigravity" / VERSION


def screen(name: str):
    return AntigravityHarness().screen((SCREENS / f"{name}.txt").read_text())


def test_every_fixture_set_has_the_screens_the_adapter_reads():
    names = {p.stem for p in SCREENS.glob("*.txt")}
    assert {"idle", "paste-shown", "working", "working-queued", "interrupted"} <= names


def test_an_idle_seat_shows_an_empty_box_and_its_model_and_effort():
    s = screen("idle")
    assert s.box == "" and not s.working and s.interruption is None
    assert s.shows == {"model": "Gemini 4 Argon", "effort": "high"} and same_name(s.shows["model"], "gemini-4-argon-high")
    assert any(e.startswith("You are seat laan on ship example") for e in s.echoes), "the first message is echoed in full"


def test_a_paste_shows_in_full_with_no_fold():
    harness = AntigravityHarness()
    four = "qd > order from desk · ord-1234 · Four\nline two\nline three\nqd > reply: qd post report --re ord-1234"
    s = screen("paste-shown")
    assert harness.box(four).matches(s.box), s.box


def test_a_working_seat_and_a_queued_message():
    s = screen("working")
    assert s.working and s.box == "" and s.interruption is None
    q = screen("working-queued")
    assert q.working and q.box == "" and q.queued == 1, "the placeholder is not text in the box"
    typed = "qd > order from desk · ord-0002 · Second\nThis arrived mid-turn. Say SECOND-SEEN.\nqd > reply: qd post report --re ord-0002\n"
    assert any(echoed(e, " ".join(typed.split())) for e in q.echoes), q.echoes
    assert AntigravityHarness().keys.send_now == ("M-m",), "alt+m sends a queued message immediately"


def test_an_interrupted_seat_is_idle_and_the_turns_prompt_is_echoed():
    s = screen("interrupted")
    assert not s.working and s.box == ""
    assert any(echoed(e, "Run exactly this shell command: sleep 30; echo late") for e in s.echoes)


def test_the_record_is_opaque_and_the_adapter_says_so():
    harness = AntigravityHarness()
    assert harness.record_confirms is False and harness.home_variable is None
    assert harness.keys.empty is None, "no key empties the whole box: the door says what it leaves behind"
    assert all(c.session is None for c in harness.controls()), "model and effort change at launch"


def test_the_launch_carries_the_brief_as_the_first_message_and_our_settings(tmp_path: Path):
    harness = AntigravityHarness()
    seat = SeatSpec("laan", "bridge", "navigator", "antigravity", ("google",), model="gemini-4-argon-high", effort="high",
                    cwd=tmp_path / "work", settings={"theme": "dark"})
    home = tmp_path / "home"
    assert harness.build_home(home, seat, Pool("google", "antigravity"), None) is None
    settings = json.loads((home / "settings.json").read_text())
    assert settings == {"trustedWorkspaces": [str(tmp_path / "work")], "theme": "dark"}
    brief = tmp_path / "brief.md"
    brief.write_text("# Standing orders\nBe brief.\n")
    launch = harness.launch(seat, Pool("google", "antigravity"), home, brief, None, "qd > briefed as laan")
    assert launch.argv[:3] == ("agy", "--dangerously-skip-permissions", "--settings")
    assert launch.argv[-2:] == ("--prompt-interactive", "# Standing orders\nBe brief.\n\nqd > briefed as laan")
    assert launch.env == {"AGY_CLI_DISABLE_AUTO_UPDATE": "true"}
    resumed = harness.launch(seat, Pool("google", "antigravity"), home, brief, "abc", None)
    assert "--conversation" in resumed.argv and "--prompt-interactive" not in resumed.argv, "a resumed conversation is not briefed twice"
