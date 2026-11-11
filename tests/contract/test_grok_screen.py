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
    assert adapter.box_columns(80) == 70
    assert adapter.box(wanted).matches(screen.box, 70)
    assert not adapter.box(wanted.replace("several words", "severalwords")).matches(screen.box, 70)


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


@pytest.mark.parametrize("change", ["boundary", "hint", "style", "terminal-control", "background", "row-width"])
def test_unmeasured_ui_is_reported_unknown(change):
    text = fixture("draft-multiline")
    if change == "boundary":
        text = text.replace("╭", "+")
    elif change == "hint":
        text = text.replace("Ctrl+x:shortcuts", "Changed native footer")
    elif change == "style":
        text = text.replace("225;225;225", "90;90;90")
    elif change == "background":
        text = text.replace("225;225;225m", "225;225;225m\x1b[48;2;200;0;0m")
    elif change == "row-width":
        text = text.replace("Last line: done.", "Last line: done. extra cells")
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


def with_blank_rows(count, *, interior=False, empty=False):
    """Author additional ordinary composer rows, without inventing native padding semantics."""
    text = fixture("draft-multiline")
    rows = text.splitlines()
    first = next(i for i, row in enumerate(rows) if " ❯ " in row)
    last = next(i for i, row in enumerate(rows) if row.startswith("  ╰"))
    blank = "  │   " + " " * 71 + "│  "
    if empty:
        rows[first:last] = ["  │ ❯ " + " " * 71 + "│  "] + [blank] * count
    elif interior:
        rows[first + 1:first + 1] = [blank] * count
    else:
        rows[last:last] = [blank] * count
    return "\n".join(rows) + "\n"


@pytest.mark.parametrize("count", [1, 2, 3])
def test_trailing_blank_rows_are_retained_and_reported_unknown(count):
    adapter = GrokHarness()
    screen = adapter.screen(with_blank_rows(count))
    assert screen.unknown == "Grok trailing blank input row or unmeasured padding"
    assert len(screen.box.split("\n")) == 3 + count
    assert all(row == " " * 71 for row in screen.box.split("\n")[-count:])
    assert not adapter.box("First synthetic line.\nSecond line has [brackets].\nLast line: done.").matches(screen.box, 71)


def test_interior_blank_input_line_is_preserved():
    adapter = GrokHarness()
    screen = adapter.screen(with_blank_rows(1, interior=True))
    wanted = "First synthetic line.\n\nSecond line has [brackets].\nLast line: done."
    assert screen.unknown is None
    assert adapter.box(wanted).matches(screen.box, 71)
    assert not adapter.box(wanted.replace("\n\n", "\n")).matches(screen.box, 71)


def test_multiple_empty_rows_are_not_assumed_to_be_an_empty_composer():
    screen = GrokHarness().screen(with_blank_rows(1, empty=True))
    assert screen.unknown
    assert screen.box == " " * 71 + "\n" + " " * 71


@pytest.mark.parametrize("length", [70, 71, 140])
def test_measured_unbroken_wrap_boundary(length):
    adapter = GrokHarness()
    text = fixture("idle-mono")
    rows = text.splitlines()
    at = next(i for i, row in enumerate(rows) if " ❯ " in row)
    wanted = "S" * length
    pieces = [wanted[i:i + 70] for i in range(0, length, 70)]
    rows[at:at + 1] = ["  │ " + ("❯ " if i == 0 else "  ") + piece.ljust(71) + "│  "
                       for i, piece in enumerate(pieces)]
    screen = adapter.screen("\n".join(rows) + "\n")
    assert screen.unknown is None
    assert len(screen.box.split("\n")) == len(pieces)
    assert adapter.box(wanted).matches(screen.box, adapter.box_columns(80))
    assert not adapter.box(wanted + "S").matches(screen.box, adapter.box_columns(80))


@pytest.mark.parametrize("leading,trailing,count", [(0, 0, 24), (0, 1, 24), (0, 2, 25), (1, 2, 26)])
def test_folded_line_count_never_proves_intended_content(leading, trailing, count):
    adapter = GrokHarness()
    screen = adapter.screen(fixture("folded-multiline").replace("Pasted: 24 lines", f"Pasted: {count} lines"))
    # Keep the measured right border fixed when a display count changes width.
    assert screen.unknown is None
    assert screen.interruption.name == "folded paste: full content is not visible"
    assert screen.interruption.key is None and screen.box is None
    wanted = "\n" * leading + "\n".join(f"Synthetic item {i}." for i in range(24)) + "\n" * trailing
    collision = "\n" * leading + "\n".join(f"Different content {i}." for i in range(24)) + "\n" * trailing
    assert not adapter.box(wanted).matches(screen.box, adapter.box_columns(80))
    assert not adapter.box(collision).matches(screen.box, adapter.box_columns(80))
    marker = f"[Pasted: {count} lines]"
    assert not adapter.box(wanted).matches(marker, adapter.box_columns(80))
    assert not adapter.box(collision).matches(marker, adapter.box_columns(80))


def test_fold_chip_needs_measured_styling():
    adapter = GrokHarness()
    native = fixture("folded-multiline")
    assert adapter.screen(native).unknown is None
    changed = adapter.screen(native.replace("48;2;17;17;17", "48;2;60;60;60"))
    assert changed.box is None and changed.unknown
    assert "[Pasted: 24 lines]" in changed.text


def test_literal_fold_spelling_is_retained_as_draft_text():
    adapter = GrokHarness()
    hint, token = "<model> [window] [effort]", "[Pasted: 24 lines]"
    text = fixture("draft-literal-hint").replace(hint, token.ljust(len(hint)))
    screen = adapter.screen(text)
    assert screen.unknown is None
    assert adapter.box("[Pasted: 24 lines]").matches(screen.box, adapter.box_columns(80))
    assert not adapter.box("a different literal").matches(screen.box, adapter.box_columns(80))


def test_clipped_single_line_viewport_cannot_prove_full_paste():
    adapter = GrokHarness()
    screen = adapter.screen(fixture("clipped-viewport"))
    assert screen.unknown
    assert not adapter.box("V" * 2000).matches(screen.box, adapter.box_columns(80))


@pytest.mark.parametrize("wanted", ["\n", "   ", "\n\n"])
def test_whitespace_only_draft_never_matches_visually_empty_content(wanted):
    adapter = GrokHarness()
    screen = adapter.screen(fixture("idle-mono"))
    assert not adapter.box(wanted).matches(screen.box, adapter.box_columns(80))
