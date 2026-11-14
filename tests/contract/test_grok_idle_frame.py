"""Ordinary Grok 1.0.77 idle frames, with and without its weekly footer field.

Both full native frames retain their geometry and transcript scrollbar; all
transcript text and the displayed percentage are synthetic.
"""

import json
from pathlib import Path

import pytest

from quarterdeck.harness.grok import GrokHarness
from quarterdeck.harness.grok_screen import empty_cell, queue_entries


FIXTURES = Path(__file__).parents[1] / "fixtures/grok/1.0.77"


def frame(name):
    return "\n".join(json.loads((FIXTURES / f"{name}.json").read_text())["lines"]) + "\n"


@pytest.mark.parametrize("name", ["idle-478", "idle-weekly-478"])
def test_native_idle_frame_reads_its_composer_below_the_transcript(name):
    text = frame(name)
    assert len(text.splitlines()) == 113
    assert all(len(row) == 478 for row in text.splitlines())
    screen = GrokHarness().screen(text)
    assert screen.unknown is None and screen.interruption is None
    assert screen.working is False
    assert screen.box == " " * 469
    assert screen.shows == {"model": "Grok 4.7", "effort": "high"}
    assert empty_cell(screen) == (6, 108)
    assert queue_entries(text) == () and screen.queued == 0
    assert screen.quota is None


def with_percentage(value):
    rows = frame("idle-weekly-478").splitlines()
    rows[109] = rows[109].replace("9%", f"{value}%")
    # Keep the captured right border fixed when the synthetic number changes width.
    dash_delta = 1 - len(value)
    rule, label = rows[109].split(" Weekly", 1)
    rows[109] = "  ╰" + "─" * (len(rule) - 3 + dash_delta) + " Weekly" + label
    return "\n".join(rows) + "\n"


@pytest.mark.parametrize("value", ["0", "1", "10", "99", "100"])
def test_weekly_percentage_is_display_text_without_a_quota_wall(value):
    screen = GrokHarness().screen(with_percentage(value))
    assert screen.unknown is None and screen.working is False
    assert screen.box == " " * 469 and screen.quota is None
    assert screen.shows == {"model": "Grok 4.7", "effort": "high"}


@pytest.mark.parametrize("value", ["-1", "101", "09", "1.5", "", "unknown"])
def test_malformed_weekly_percentage_keeps_the_frame_unreadable(value):
    screen = GrokHarness().screen(with_percentage(value))
    assert screen.unknown and screen.box is None


@pytest.mark.parametrize("old,new", [
    ("Weekly limit left:", "Daily limit left:"),
    ("9% · Grok", "9% / Grok"),
    ("│ ❯", "│ >"),
    ("╭", "+"),
    ("╰", "+"),
    ("Ctrl+x:shortcuts", "Ctrl+x:other"),
    ("always-approve", "ask-permission"),
])
def test_optional_weekly_field_preserves_existing_frame_requirements(old, new):
    screen = GrokHarness().screen(frame("idle-weekly-478").replace(old, new))
    assert screen.unknown and screen.box is None


def test_weekly_words_in_the_transcript_do_not_repair_a_missing_footer():
    text = frame("idle-weekly-478").replace("╰", "+")
    screen = GrokHarness().screen("Weekly limit left: 9% · Grok 4.7 (high)\n" + text)
    assert screen.unknown and screen.box is None
