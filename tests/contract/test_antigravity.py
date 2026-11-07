"""Antigravity 1.5.2 against the screens saved from the spike of 7 November 2026, best effort by design."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from quarterdeck.door import echoed
from quarterdeck.harness import antigravity
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
    assert {"idle", "paste-shown", "paste-long-folded", "paste-long-lines-folded", "working", "working-queued", "interrupted"} <= names


def test_an_idle_seat_shows_an_empty_box_and_its_model_and_effort():
    s = screen("idle")
    assert s.box == "" and not s.working and s.interruption is None
    assert s.shows == {"model": "Gemini 4 Argon", "effort": "high"} and same_name(s.shows["model"], "gemini-4-argon-high")
    assert any(e.startswith("You are seat laan on ship example") for e in s.echoes), "the first message is echoed in full"


def test_a_short_paste_shows_whole_and_a_long_one_folds_to_its_length():
    """Probed 7 November 2026: four short lines show whole; 2,003 characters fold to a count that leaves out the trailing newline."""
    harness = AntigravityHarness()
    four = "qd > order from desk · ord-1234 · Four\nline two\nline three\nqd > reply: qd post report --re ord-1234"
    s = screen("paste-shown")
    assert harness.box(four).matches(s.box), s.box
    long = "qd probe B: first line\n" + " ".join(f"word{i}" for i in range(260)) + "\nlast line\n"
    assert len(long) == 2003
    folded = screen("paste-long-folded")
    assert folded.box == "[Pasted text #3 2002 chars]" and harness.box(long).matches(folded.box)
    assert not harness.box(long + "x\n").matches(folded.box), "the count is this paste's own"
    many = "\n".join(f"line {i} of a long order" for i in range(42)) + "\n"
    lines = screen("paste-long-lines-folded")
    assert lines.box == "[Pasted text #5 +42 lines]" and harness.box(many).matches(lines.box), "many lines fold by their count"
    assert not harness.box(many + "one more\n").matches(lines.box)


def test_the_join_takes_the_one_conversation_that_appeared_since_the_launch(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(antigravity, "STATE_DIR", tmp_path / "state")
    conversations = tmp_path / "state" / "conversations"
    conversations.mkdir(parents=True)
    (conversations / "older.db").write_text("the user's own, from before")
    harness = AntigravityHarness()
    seat = SeatSpec("laan", "bridge", "navigator", "antigravity", ("google",), cwd=tmp_path)
    home, brief = tmp_path / "home", tmp_path / "brief.md"
    home.mkdir()
    brief.write_text("# Standing orders\n")
    harness.launch(seat, Pool("google", "antigravity"), home, brief, None, None)
    assert (home / antigravity.BEFORE).read_text() == "older.db\n"
    launched_at = datetime.now(timezone.utc)
    assert harness.join(home, launched_at).reason == "no record yet"
    (conversations / "older.db").write_text("written to after the launch: its ctime moved, and it is still not ours")
    assert harness.join(home, launched_at).reason == "no record yet"
    (conversations / "ours.db").write_text("new")
    assert harness.join(home, launched_at).record == conversations / "ours.db"
    (conversations / "theirs.db").write_text("the user started one too")
    join = harness.join(home, launched_at)
    assert join.record is None and join.reason == "ambiguous: ours.db, theirs.db appeared since the launch"  # law: one-record
    harness.launch(seat, Pool("google", "antigravity"), home, brief, "ours", None)
    assert (home / antigravity.BEFORE).read_text() == "older.db\n", "a resumed launch keeps what the first noted"


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


def test_an_echo_keeps_a_paragraph_break_and_stops_at_the_programs_own_line():
    msg = "qd > order from desk · ord-1234\nFirst paragraph.\n\nSecond paragraph.\nqd > reply: qd post report --re ord-1234"
    text = ("─" * 60 + "\n> qd > order from desk · ord-1234\n  First paragraph.\n\n  Second paragraph.\n  qd > reply: qd post report --re ord-1234\n"
            "  ⎿  Interrupted · What should Antigravity CLI do instead?\n" + "─" * 80 + "\n>\n" + "─" * 80 + "\n"
            "esc to cancel                                              Gemini 4 Argon · high\n")
    s = AntigravityHarness().screen(text)
    assert any(echoed(e, " ".join(msg.split())) for e in s.echoes), s.echoes
    assert all("Interrupted" not in e for e in s.echoes)


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
