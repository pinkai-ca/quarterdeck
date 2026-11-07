"""Codex 0.166.0 against the screens saved from the spike of 7 November 2026."""

from __future__ import annotations

from pathlib import Path

from quarterdeck.harness.codex import CodexHarness
from quarterdeck.ship import Pool, SeatSpec

from tests.conftest import FIXTURES

VERSION = "0.166.0"
SCREENS = FIXTURES / "codex" / VERSION


def screen(name: str):
    return CodexHarness().screen((SCREENS / f"{name}.txt").read_text())


def test_every_fixture_set_has_the_screens_the_adapter_reads():
    names = {p.stem for p in SCREENS.glob("*.txt")}
    assert {"idle", "paste-6-lines-shown", "working", "model-picker", "interrupted"} <= names


def test_an_idle_seat_shows_its_placeholder_as_an_empty_box():
    s = screen("idle")
    assert s.box == "" and not s.working and s.interruption is None
    assert s.shows == {"model": "GPT-6-Astra", "effort": "low"}


def test_a_paste_shows_in_full_with_no_fold():
    harness = CodexHarness()
    six = "qd > order from desk · ord-5678 · Six\na\nb\nc\nd\nqd > reply: qd post report --re ord-5678"
    s = screen("paste-6-lines-shown")
    assert harness.box(six).matches(s.box), s.box
    assert not harness.box("something else").matches(s.box)


def test_a_working_seat_is_open_and_queues():
    s = screen("working")
    assert s.working and s.box == "" and s.interruption is None
    assert CodexHarness().queues_while_working(SeatSpec("s", "bridge", "engineering", "codex", ("codex",)))


def test_a_message_queued_mid_turn_is_echoed_above_the_working_line():
    from quarterdeck.door import echoed

    s = screen("working")
    typed = "qd > order from desk · ord-0002 · Second\nThis arrived mid-turn. Say SECOND-SEEN.\nqd > reply: qd post report --re ord-0002\n"
    assert any(echoed(e, " ".join(typed.split())) for e in s.echoes), s.echoes
    assert s.echoes[-1].startswith("qd > order from desk · ord-0002 · Second"), "the queued one is the last echo before Working"
    assert any("Run exactly this shell command" in e for e in s.echoes), "the turn's own prompt is echoed too"
    assert echoed("qd > order from desk · ord-7f3a · Spike What seat are you, per your develope…",
                  "qd > order from desk · ord-7f3a · Spike What seat are you, per your developer instructions? And more")
    assert not echoed("qd > order from desk · ord-7f3a · Spike", "qd > order from desk · ord-7f3b · Spike")


def test_the_model_picker_is_known_and_closed_with_escape():
    s = screen("model-picker")
    assert s.interruption is not None and s.interruption.name == "model picker" and s.interruption.key == "Escape"
    harness = CodexHarness()
    assert harness.control("model").session is None, "model changes at launch: qd set says restart applies it"


def test_an_interrupted_seat_is_idle():
    s = screen("interrupted")
    assert not s.working


def test_the_launch_builds_a_home_of_the_ships_own(tmp_path: Path):
    harness = CodexHarness()
    pool_dir = tmp_path / "pool"
    pool_dir.mkdir()
    (pool_dir / "auth.json").write_text("{}")
    pool = Pool("codex", "codex", config_dir=pool_dir)
    runtime = tmp_path / "ship" / "runtime"
    seat = SeatSpec("scott", "bridge", "engineering", "codex", ("codex",), model="gpt-6-astra", effort="xhigh",
                    cwd=tmp_path / "work", settings={"model_auto_compact_token_limit": 200000})
    home = runtime / "seats" / "scott" / "home"
    credential = harness.build_home(home, seat, pool, None)
    assert credential.link == (home / "auth.json", pool_dir / "auth.json") and (home / "auth.json").is_symlink()
    config = (home / "config.toml").read_text()
    assert 'model = "gpt-6-astra"' in config and 'model_reasoning_effort = "xhigh"' in config
    assert "project_doc_max_bytes = 0" in config and f'writable_roots = ["{runtime}"]' in config
    assert f'[projects."{tmp_path / "work"}"]\ntrust_level = "trusted"' in config
    assert config.startswith("model_auto_compact_token_limit = 200000\n"), "the seat's own keys pass through"
    brief = tmp_path / "brief.md"
    brief.write_text("# Standing orders\nBe brief.\n")
    launch = harness.launch(seat, pool, home, brief, None, None)
    assert launch.argv[:3] == ("codex", "--no-daemon", "-c") and launch.argv[3] == 'developer_instructions="# Standing orders\\nBe brief.\\n"'
    assert launch.argv[4:] == ("-m", "gpt-6-astra", "-c", 'model_reasoning_effort="xhigh"')
    assert launch.env == {"CODEX_HOME": str(home)}
    resumed = harness.launch(seat, pool, home, brief, "0199-abc", "qd > briefed")
    assert resumed.argv[-3:] == ("resume", "0199-abc", "qd > briefed")
