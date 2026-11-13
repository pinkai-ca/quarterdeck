"""A screen that cannot show whether a seat works reads unknown, never idle (ord-3645). Measured fixtures only, cut.

A mid-redraw frame is a measured idle screen cut above its box, as a redraw between flushes leaves it; a cleared
pane is empty; an unrecognised layout is text no adapter draws. Measured idle and working screens read as before.
"""

import json
from pathlib import Path

import pytest

from quarterdeck import harness as harnesses

FIXTURES = Path(__file__).parents[1] / "fixtures"
TEXT = {  # adapter: (version folder, a measured idle screen, a measured working screen)
    "claude": ("2.1.317", "startup-bypass-ready", "working-queued"),
    "codex": ("0.166.0", "idle", "working"),
    "antigravity": ("1.5.5", "idle", "working"),
    "opencode": ("1.19.5", "idle", "working"),
    "pi": ("0.74.2", "idle", "working"),
}


def read(name, version, fixture):
    return (FIXTURES / name / version / f"{fixture}.txt").read_text()


def grok(fixture):
    return "\n".join(json.loads((FIXTURES / "grok" / "1.0.77" / f"{fixture}.json").read_text())["lines"]) + "\n"


def cut_above_the_box(text):
    lines = text.rstrip("\n").split("\n")
    return "\n".join(lines[:max(1, len(lines) // 3)]) + "\n"


@pytest.mark.parametrize("name", sorted(TEXT))
def test_measured_idle_and_working_read_as_before(name):
    adapter = harnesses.get(name)
    version, idle, working = TEXT[name]
    assert adapter.screen(read(name, version, idle)).working is False
    assert adapter.screen(read(name, version, working)).working is True


@pytest.mark.parametrize("name", sorted(TEXT))
def test_a_mid_redraw_frame_reads_unknown_never_idle(name):
    adapter = harnesses.get(name)
    version, idle, _ = TEXT[name]
    screen = adapter.screen(cut_above_the_box(read(name, version, idle)))
    assert screen.box is None and screen.working is None


@pytest.mark.parametrize("name", sorted(TEXT) + ["grok"])
@pytest.mark.parametrize("text", ["", "\n\n", "synthetic unrecognised layout q\nnothing any adapter draws\n"])
def test_a_cleared_pane_or_unrecognised_layout_reads_unknown(name, text):
    screen = harnesses.get(name).screen(text)
    assert screen.working is None, (name, text)


def test_grok_measured_idle_reads_idle_and_its_unread_hint_reads_unknown():
    adapter = harnesses.get("grok")
    assert adapter.screen(grok("idle-mono")).working is False
    assert adapter.screen(grok("working-response")).working is True
    assert adapter.screen(cut_above_the_box(grok("idle-mono"))).working is None


def test_the_screen_type_never_reports_a_default_as_idle():
    from quarterdeck.harness.types import Screen
    assert Screen("", None).working is None
    assert Screen("x", None, working=False).working is None, "no box: not seeing the sign proves nothing"
    assert Screen("x", "", working=False, unknown="synthetic").working is None
    assert Screen("x", "", working=False, starting=True).working is None
    assert Screen("x", "", working=False).working is False
    assert Screen("x", None, working=True).working is True, "the sign itself, seen, is working"


@pytest.mark.parametrize("age,clears", [(5, "time"), (59, "time"), (61, "needs a human"), (3600, "needs a human")])
def test_a_boxless_frame_within_the_launch_start_up_bound_is_starting_not_an_obstruction(monkeypatch, age, clears):
    """At qd up Codex drew a frame before its box, and the service reported an obstruction (ord-11d1).

    The frame here is synthetic: the measured start-up frames already read as starting, and the frame that raised the
    reports was not captured. Its shape is a Codex banner with neither the status line nor the box drawn yet.
    """
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from quarterdeck import door as doormod, pending, turn_failure
    adapter = harnesses.get("codex")
    frame = adapter.screen(">_ OpenAI Codex (v0.166.0)\n\n model:     synthetic\n directory: ~/synthetic\n")
    assert frame.box is None and frame.starting is False and frame.unknown is None and frame.working is None
    at = (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()
    seat = SimpleNamespace(harness=adapter, tmux=None, launched=lambda: SimpleNamespace(at=at),
                           session=SimpleNamespace(human=False), spec=None)
    monkeypatch.setattr(turn_failure, "obstruction", lambda seat: None)
    monkeypatch.setattr(pending, "held", lambda *args, **kwargs: None)
    monkeypatch.setattr(doormod.Door, "wall", lambda self, screen: None)
    reading = doormod.Door(seat)._reading(frame)
    assert not reading.open and reading.clears == clears, reading
