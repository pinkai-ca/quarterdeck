"""New synthetic UI material following the ordinary native 1.0.77 measurement.

These cases exercise only the adapter's text decoder, with no tmux, provider,
credential, launch or framework protection exercise.
"""

from pathlib import Path
import json

import pytest

from quarterdeck.harness.grok import GrokHarness

FIXTURES = Path(__file__).parents[1] / "fixtures/grok/1.0.77"


def fixture(name):
    if name == "draft-wrapped-payload":
        return (FIXTURES / f"{name}.txt").read_text()
    return "\n".join(json.loads((FIXTURES / f"{name}.json").read_text())["lines"]) + "\n"


@pytest.mark.parametrize("name", ["idle-color", "idle-mono"])
def test_ordinary_composer_and_running_settings(name):
    adapter = GrokHarness()
    screen = adapter.screen(fixture(name))
    assert screen.box is not None and not screen.box.strip()
    assert screen.unknown is None and screen.interruption is None
    assert screen.shows == {"model": "Grok 4.7", "effort": "high"}
    assert "\x1b" not in screen.text


def test_hard_newlines_and_exact_multiline_content():
    adapter = GrokHarness()
    screen = adapter.screen(fixture("draft-multiline"))
    wanted = "First synthetic line.\nSecond line has [brackets].\nLast line: done."
    assert adapter.box(wanted).matches(screen.box, adapter.box_columns(80))
    assert not adapter.box(wanted.replace("\n", " ")).matches(screen.box, adapter.box_columns(80))


def test_word_wrapped_draft_keeps_exact_word_separators():
    adapter = GrokHarness()
    screen = adapter.screen(fixture("draft-wrapped"))
    wanted = fixture("draft-wrapped-payload")
    assert adapter.box_columns(80) == 71
    assert adapter.box(wanted).matches(screen.box, 71)
    assert not adapter.box(wanted.replace("several words", "severalwords")).matches(screen.box, 71)


@pytest.mark.parametrize("name", ["model-picker-color", "model-picker-mono"])
def test_measured_model_hint_needs_picker_and_native_styling(name):
    adapter = GrokHarness()
    screen = adapter.screen(fixture(name))
    assert screen.interruption.name == "model picker"
    assert screen.interruption.key == "C-c"
    assert screen.box == "/model "
    assert "<model> [window] [effort]" in screen.text


def test_identical_bright_literal_is_user_content():
    adapter = GrokHarness()
    wanted = "<model> [window] [effort]"
    screen = adapter.screen(fixture("draft-literal-hint"))
    assert screen.interruption is None
    assert adapter.box(wanted).matches(screen.box, 71)
    assert not adapter.box("").matches(screen.box, 71)


def test_commands_modal_is_distinct_from_underlying_composer():
    screen = GrokHarness().screen(fixture("commands"))
    assert screen.box is None
    assert screen.interruption.name == "commands"
    assert screen.interruption.key == "Escape"


@pytest.mark.parametrize("change", ["boundary", "hint", "style", "terminal-control"])
def test_unmeasured_ui_is_reported_unknown(change):
    text = fixture("draft-multiline")
    if change == "boundary":
        text = text.replace("╭", "+")
    elif change == "hint":
        text = text.replace("Ctrl+x:shortcuts", "Changed native footer")
    elif change == "style":
        text = text.replace("225;225;225", "90;90;90")
    else:
        text = "\x1b]unknown\x07" + text
    screen = GrokHarness().screen(text)
    assert screen.box is None and screen.unknown


def test_control_confirmation_uses_footer_not_transcript_echo():
    adapter = GrokHarness()
    text = fixture("idle-mono")
    screen = adapter.screen("Switched to Grok 4.6 (low effort)\n" + text)
    assert adapter.confirms(adapter.control("model"), "grok-4.6", screen, None) is False
    assert adapter.confirms(adapter.control("effort"), "low", screen, None) is False
    changed = adapter.screen(text.replace("─ Grok 4.7 (high)", "── Grok 4.6 (low)"))
    assert adapter.confirms(adapter.control("model"), "grok-4.6", changed, None) is True
    assert adapter.confirms(adapter.control("effort"), "low", changed, None) is True


def test_unknown_muted_content_is_retained_in_screen_text():
    text = fixture("draft-literal-hint").replace("225;225;225", "108;108;108")
    screen = GrokHarness().screen(text)
    assert screen.unknown and screen.box is None
    assert "<model> [window] [effort]" in screen.text
