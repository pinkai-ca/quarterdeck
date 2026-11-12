"""Synthetic layouts constrained to the safe offline TUI description."""

from quarterdeck.harness.muse_screen import (EXPAND_HINT, IDLE_HINT, MUTED, PROMPT, TYPED,
                                           TRUST, VisibleBox, counted_characters, read, wrapped_rows)


def frame(text="", status="", working=False):
    return ("Muse Code 1.4.15\n" + ("Thinking (2s · esc to interrupt)\n" if working else "")
            + "────────────────────\n❯ " + text + "\n────────────────────\n"
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
    assert read(frame().replace("────────────────────", "unmeasured border")).unknown is not None
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
    text = "alpha beta gamma delta"
    narrow = read(composer(["alpha beta gamma ", "delta"], columns=20))
    wide = read(composer([text], columns=80))
    assert VisibleBox(text).matches(narrow.box, 18)
    assert VisibleBox(text).matches(wide.box, 78)
    assert not VisibleBox(text).matches(narrow.box, 78)


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
