"""Synthetic layouts constrained to the safe offline TUI description."""

from datetime import datetime, timezone

import pytest

from quarterdeck.harness.muse_screen import (EXPAND_HINT, IDLE_HINT, MUTED, PROMPT, TYPED,
                                           TRUST, VisibleBox, counted_characters, read, wrapped_rows)


def frame(text="", status="", working=False):
    rule = "─" * 80
    return ("Muse Code 1.4.15\n" + ("Thinking (2s · esc to interrupt)\n" if working else "")
            + rule + "\n❯ " + text + "\n" + rule + "\n"
            + "echo · synthetic-folder · Launch overrides" + status)


def test_working_indicator_survives_reply_text_and_gate_until_terminal_frame():
    screen = read("A fictional reply.\n" + frame(working=True))
    assert screen.box == "" and screen.working and screen.unknown is None
    idle = read(frame())
    assert idle.box == "" and not idle.working and idle.unknown is None
    assert read(frame(status=" interrupting run")).unknown is not None


def test_no_placeholder_is_discarded_and_queue_is_only_screen_evidence():
    screen = read(frame("A fictional draft.", status="\nQueued input · ↑ to edit", working=True))
    assert screen.box == "A fictional draft." and screen.queued == 1
    assert VisibleBox("A fictional draft.").matches(screen.box)
    assert not VisibleBox("A fictional draft.\nAnother line.").matches(screen.box)
    assert not VisibleBox("A fictional draft. ").matches(screen.box)


def test_trust_requires_a_person_and_unknown_layout_never_supplies_a_box():
    screen = read("Do you trust this workspace?\n1 Trust and continue\n2 Quit\nEsc quits")
    assert screen.interruption == TRUST and not TRUST.dismissable
    assert read(frame().replace("─" * 80, "unmeasured border")).unknown is not None
    assert read("An unfamiliar screen.").box is None


def colour(text, foreground, background=None):
    start = "\x1b[38;2;" + ";".join(map(str, foreground)) + "m"
    if background is not None:
        start += "\x1b[48;2;" + ";".join(map(str, background)) + "m"
    return start + text + "\x1b[0m"


def composer(rows, columns=80, foreground=TYPED, footer="", transcript=""):
    body = colour("❯ ", PROMPT) + colour(rows[0], foreground)
    body += "".join("\n" + colour("  ", MUTED) + colour(row, foreground) for row in rows[1:])
    rule = colour("─" * columns, MUTED)
    return ("  " + colour("Muse Code", TYPED) + " 1.4.15\n" + transcript
            + rule + "\n" + body + "\n" + rule + "\n"
            + "echo · synthetic-folder · Launch overrides" + footer)


def test_known_grey_idle_tip_is_empty_while_the_same_bright_literal_is_text():
    grey = read(composer([IDLE_HINT], foreground=MUTED))
    bright = read(composer([IDLE_HINT]))
    assert grey.box == "" and grey.unknown is None
    assert bright.box == IDLE_HINT and bright.unknown is None
    assert read(composer(["A different fictional tip."], foreground=MUTED)).unknown is not None


def test_repeated_transcript_prompts_do_not_replace_the_composer():
    transcript = "".join(colour("─" * 80, MUTED) + "\n"
                         + colour("❯ ", PROMPT, (38, 56, 84))
                         + colour("A fictional earlier input.", TYPED, (38, 56, 84)) + "\n"
                         for _ in range(8))
    screen = read(composer(["A fictional current draft."], transcript=transcript))
    assert screen.box == "A fictional current draft." and screen.unknown is None


def test_multiline_and_explicit_blank_rows_keep_their_exact_shape():
    text = "first \nsecond\n\nfourth"
    screen = read(composer(["first ", "second", "", "fourth"]))
    assert screen.box == text and VisibleBox(text).matches(screen.box, 78)
    assert not VisibleBox("first\nsecond\n\nfourth").matches(screen.box, 78)


def test_ascii_word_wrap_keeps_the_break_space_and_uses_the_pane_width():
    prefix = "word " * 15
    text = prefix + "xy completion"
    narrow = read(composer([prefix, "xy completion"], columns=80))
    wide = read(composer([text], columns=120))
    assert VisibleBox(text).matches(narrow.box, 78)
    assert VisibleBox(text).matches(wide.box, 118)
    assert not VisibleBox(text).matches(narrow.box, 118)


def test_counted_fold_is_a_projection_of_the_full_original_form():
    text = "A fictional post · fictional paragraph.\n" * 40
    marker = f"[Pasted Content {len(text)} chars]"
    screen = read(composer([marker], footer="\n" + EXPAND_HINT))
    assert VisibleBox(text).matches(screen.box, 78)
    assert not VisibleBox(text + "x").matches(screen.box, 78)
    literal = read(composer([marker]))
    assert not VisibleBox(text).matches(literal.box, 78)
    assert VisibleBox(marker).matches(literal.box, 78)


def test_unmeasured_wrap_shapes_remain_unknown_projections():
    assert wrapped_rows("x" * 18, 18) is None
    assert wrapped_rows("first\tsecond", 78) is None
    assert wrapped_rows("A fictional \u754c character.", 78) is None
    assert wrapped_rows("A fictional e\u0301 character.", 78) is None


def test_marker_counts_each_tab_as_four_without_expanding_the_input():
    text = "A fictional ending\t"
    assert counted_characters(text) == str(len(text) + 3)
    assert text.endswith("\t")


@pytest.mark.parametrize("columns", [80, 120])
def test_short_canonical_post_retains_middle_dots_and_its_final_empty_row(columns):
    from quarterdeck.harness import get
    from quarterdeck.harness.types import Input
    from quarterdeck.post import Message, typed

    at = datetime(2040, 1, 1, tzinfo=timezone.utc)
    message = Message("ord-synthetic-item", "order", "synthetic", "synthetic", at,
                      "Check · one · two · three · four.")
    text = typed([message])
    rows = text.split("\n")
    assert text.count("·") == 6 and rows[-1] == ""
    assert all(len(row) < columns - 3 for row in rows)
    screen = read(composer(rows, columns=columns))
    harness = get("muse")
    assert screen.box == text and not screen.box.folded
    assert harness.box(text).matches(screen.box, harness.box_columns(columns))
    assert harness.expected_input(Input(text[:-1], at), text) == text[:-1]


@pytest.mark.parametrize("columns", [80, 120])
@pytest.mark.parametrize("dot_offset", [-4, -3, -2])
def test_measured_middle_dot_edge_placements_keep_the_word_and_its_space(columns, dot_offset):
    tail = "· " + "z" * 21
    if dot_offset == -4:
        prefix = "word " * ((columns - 10) // 5) + "final "
        rows = [prefix, tail, ""]
        assert len(prefix) == columns - 4
    elif dot_offset == -3:
        prefix = "word " * ((columns - 5) // 5) + "x "
        rows = [prefix, tail, ""]
        assert len(prefix) == columns - 3
    else:
        prefix = "word " * ((columns - 5) // 5)
        rows = [prefix, "xy " + tail, ""]
        prefix += "xy "
        assert len(prefix) == columns - 2
    text = prefix + tail + "\n"
    assert text.index("·") == columns + dot_offset
    screen = read(composer(rows, columns=columns))
    assert VisibleBox(text).matches(screen.box, columns - 2)


def test_unknown_final_word_and_internal_word_breaks_have_no_projection():
    assert wrapped_rows("x" * 77, 78) is None
    assert wrapped_rows("word " * 14 + "a-hyphenated-final-word", 78) is None


def test_greedy_wrap_moves_a_whole_short_word_from_an_earlier_offset():
    prefix = "word " * 10
    text = prefix + "x" * 30 + " end"
    screen = read(composer([prefix, "x" * 30 + " end"], columns=80))
    assert VisibleBox(text).matches(screen.box, 78)


@pytest.mark.parametrize("columns", [80, 120])
def test_greedy_wrap_retains_each_break_space_across_a_long_ascii_line(columns):
    text = "introduction " + "word " * 58 + "ok"
    assert len(text) == 305
    if columns == 80:
        rows = ["introduction " + "word " * 12] + ["word " * 15] * 3 + ["word ok"]
    else:
        rows = ["introduction " + "word " * 20, "word " * 23, "word " * 15 + "ok"]
    screen = read(composer(rows, columns=columns))
    assert VisibleBox(text).matches(screen.box, columns - 2)
