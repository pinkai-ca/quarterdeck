"""Synthetic layouts constrained to the safe offline TUI description."""

from datetime import datetime, timezone
from pathlib import Path
from textwrap import wrap

import pytest

from quarterdeck.harness.muse import MuseHarness
from quarterdeck.harness.muse_screen import (EXPAND_HINT, IDLE_HINT, MUTED, PROMPT, TYPED,
                                           TRUST, VisibleBox, characters, counted_characters, read, wrapped_rows)

CURRENT_FIXTURES = Path(__file__).parents[1] / "fixtures/muse/1.4.25"
CURRENT_STARTUP = CURRENT_FIXTURES / "startup-off.ansi"
RGB_PENDING = CURRENT_FIXTURES / "startup-pending-rgb-273x65.ansi"
INDEXED_OFF = CURRENT_FIXTURES / "startup-off-indexed-273x65.ansi"


def synthetic_rgb_off():
    """Insert the measured plain disclosure into the RGB capture that lacks it."""
    plain, _ = characters(INDEXED_OFF.read_text())
    disclosure = plain.splitlines()[3]
    rows = RGB_PENDING.read_text().splitlines(keepends=True)
    rows[3:3] = [disclosure + "\n", "\n"]
    return "".join(rows)


@pytest.mark.parametrize("fixture,contributor", [
    (RGB_PENDING, "pending"),
    (CURRENT_FIXTURES / "startup-pending-indexed-273x65.ansi", "pending"),
    (INDEXED_OFF, "off"),
    (None, "off"),  # Synthetic RGB disclosure.
], ids=["saved-rgb-pending", "captured-indexed-pending", "captured-indexed-off", "synthetic-rgb-off"])
def test_273_column_startup_reads_the_disclosure_at_both_colour_depths(tmp_path, fixture, contributor):
    frame = fixture.read_text() if fixture is not None else synthetic_rgb_off()
    adapter = MuseHarness()
    screen = adapter.screen(frame)
    assert [row for row in screen.text.splitlines() if row.startswith("─")] == ["─" * 273] * 2
    assert screen.shows == {"contributor_mode": contributor}
    if contributor == "pending":
        assert screen.box is None and screen.unknown == "unmeasured Muse contributor disclosure"
        assert adapter.check_launch_reading(screen)[1] is None
        assert not adapter.opening_readiness(tmp_path, screen)[0]
    else:
        assert screen.box == "" and screen.working is False and screen.unknown is None
        assert adapter.check_launch_reading(screen)[1] is True
        assert adapter.opening_readiness(tmp_path, screen)[0]


def test_an_unmeasured_rgb_prompt_does_not_read_pending():
    frame = RGB_PENDING.read_text().replace("\x1b[38;2;90;160;255m❯", "\x1b[38;2;90;160;254m❯", 1)
    adapter = MuseHarness()
    screen = adapter.screen(frame)
    assert screen.shows == {} and screen.box is None
    assert adapter.check_launch_reading(screen)[1] is False


def test_rgb_prompt_evidence_does_not_admit_rgb_body_text():
    frame = synthetic_rgb_off().replace("❯ \n", "❯ \x1b[38;2;204;211;219mDraft\n", 1)
    screen = read(frame)
    assert screen.box is None and screen.unknown == "unmeasured Muse composer text or contextual tip"


@pytest.mark.parametrize("columns", [80, 120, 200])
def test_the_current_startup_disclosure_uses_the_screen_at_different_widths(tmp_path, columns):
    frame = CURRENT_STARTUP.read_text()
    if columns != 200:
        plain, _ = characters(frame)
        rows = plain.splitlines()
        disclosure = ["  " + line for line in wrap(rows[3].strip(), width=columns - 2)]
        rows = rows[:3] + disclosure + rows[4:]
        frame = "\n".join(rows).replace("─" * 200, "─" * columns)
        frame = frame.replace("/workspace/example", "/different folder/task")
    adapter = MuseHarness()
    screen = adapter.screen(frame)
    assert screen.box == "" and screen.working is False and screen.unknown is None
    assert adapter.check_launch_reading(screen)[1]
    assert adapter.opening_readiness(tmp_path, screen)[0]
    assert not (tmp_path / "muse-contributor-off.json").exists()

    task = adapter.minimal_check_task
    draft = adapter.screen(frame.replace("❯ ", "❯ \x1b[38;5;252m" + task, 1))
    assert VisibleBox(task).matches(draft.box, columns - 2)
    assert not VisibleBox(task + " ").matches(draft.box, columns - 2)


def test_a_visible_current_disclosure_does_not_make_a_clipped_composer_ready(tmp_path):
    frame = CURRENT_STARTUP.read_text().split("Launch overrides", 1)[0]
    adapter = MuseHarness()
    screen = adapter.screen(frame)
    assert adapter.check_launch_reading(screen)[1]
    assert screen.unknown == "unmeasured Muse footer"
    assert not adapter.opening_readiness(tmp_path, screen)[0]


def frame(text="", status="", working=False):
    rule = "─" * 120
    return ("Muse Code 1.4.20\n" + ("Thinking (2s · esc to interrupt)\n" if working else "")
            + rule + "\n❯ " + text + "\n" + rule + "\n"
            + "  echo · synthetic-folder · Launch overrides" + status)


def test_working_indicator_survives_reply_text_and_gate_until_terminal_frame():
    screen = read("A fictional reply.\n" + frame(working=True))
    assert screen.box == "" and screen.working is True and screen.unknown is None
    idle = read(frame())
    assert idle.box == "" and idle.working is False and idle.unknown is None
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
    assert read(frame().replace("─" * 120, "unmeasured border")).unknown is not None
    assert read("An unfamiliar screen.").box is None


def colour(text, foreground, background=None):
    """Indexed SGR for a palette index; an RGB triple stays truecolor."""
    def channel(kind, color):
        if isinstance(color, int):
            return f"\x1b[{kind};5;{color}m"
        return "\x1b[" + kind + ";2;" + ";".join(map(str, color)) + "m"

    start = channel("38", foreground)
    if background is not None:
        start += channel("48", background)
    return start + text + "\x1b[0m"


def composer(rows, columns=120, foreground=TYPED, footer="", transcript=""):
    body = colour("❯ ", PROMPT) + colour(rows[0], foreground)
    body += "".join("\n" + colour("  ", MUTED) + colour(row, foreground) for row in rows[1:])
    rule = colour("─" * columns, MUTED)
    return ("  " + colour("Muse Code", TYPED) + " 1.4.20\n" + transcript
            + rule + "\n" + body + "\n" + rule + "\n"
            + "  echo · synthetic-folder · Launch overrides" + footer)


def test_the_legacy_composer_does_not_admit_the_current_rgb_prompt():
    frame = composer([""])
    assert read(frame).box == ""
    rgb = frame.replace(colour("❯ ", PROMPT), colour("❯ ", (90, 160, 255)), 1)
    screen = read(rgb)
    assert screen.box is None and screen.unknown == "Muse composer is not uniquely bound to its rule"


def test_known_grey_idle_tip_is_empty_while_the_same_bright_literal_is_text():
    grey = read(composer([IDLE_HINT], foreground=MUTED))
    bright = read(composer([IDLE_HINT]))
    assert grey.box is None and grey.unknown is not None
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
    assert screen.box == text and screen.unknown is None
    assert wrapped_rows(text, 118) is None
    assert not VisibleBox(text).matches(screen.box, 118)
    assert not VisibleBox("first\nsecond\n\nfourth").matches(screen.box, 118)


def test_ascii_word_wrap_keeps_the_break_space_and_uses_the_pane_width():
    prefix = "word " * 15
    text = prefix + "xy completion"
    assert wrapped_rows(text, 78) is None
    assert wrapped_rows(text, 118) is None
    narrow = read(composer([prefix, "xy completion"], columns=80))
    wide = read(composer([text], columns=120))
    assert not VisibleBox(text).matches(narrow.box, 78)
    assert not VisibleBox(text).matches(wide.box, 118)


def test_counted_fold_is_a_projection_of_the_full_original_form():
    text = "A fictional post · fictional paragraph.\n" * 40
    marker = f"[Pasted Content {len(text)} chars]"
    assert counted_characters(text) is None
    screen = read(composer([marker], footer="\n" + EXPAND_HINT))
    assert screen.box.folded
    assert not VisibleBox(text).matches(screen.box, 118)
    assert not VisibleBox(text + "x").matches(screen.box, 118)
    literal = read(composer([marker]))
    assert not literal.box.folded
    assert not VisibleBox(text).matches(literal.box, 118)
    assert VisibleBox(marker).matches(literal.box, 118)


def test_unmeasured_wrap_shapes_remain_unknown_projections():
    short = "A synthetic short line."
    assert wrapped_rows(short, 118) == [short]
    assert wrapped_rows(short, 78) is None
    assert wrapped_rows("x" * 18, 18) is None
    assert wrapped_rows("first\tsecond", 78) is None
    assert wrapped_rows("A fictional \u754c character.", 78) is None
    assert wrapped_rows("A fictional e\u0301 character.", 78) is None


def test_collapsed_character_counts_are_not_remeasured():
    text = "A fictional ending\t"
    assert counted_characters(text) is None
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
    assert rows[0] == 'qd > order from synthetic · ord-item'
    assert rows[-2] == 'qd > reply: qd post report --re ord-item'
    assert text.count("·") == 5 and rows[-1] == ""
    assert all(len(row) < columns - 3 for row in rows)
    screen = read(composer(rows, columns=columns))
    harness = get("muse")
    assert harness.expected_input(Input(text[:-1], at), text) == text
    assert wrapped_rows(text, harness.box_columns(columns)) is None
    if columns == 120:
        assert screen.unknown is None and screen.box == text and not screen.box.folded
    else:
        assert screen.box is None and screen.unknown is not None
    assert not harness.box(text).matches(screen.box, harness.box_columns(columns))


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
    assert wrapped_rows(text, columns - 2) is None
    assert not VisibleBox(text).matches(read(composer(rows, columns=columns)).box, columns - 2)


def test_unknown_final_word_and_internal_word_breaks_have_no_projection():
    assert wrapped_rows("x" * 77, 78) is None
    assert wrapped_rows("word " * 14 + "a-hyphenated-final-word", 78) is None


def test_greedy_wrap_moves_a_whole_short_word_from_an_earlier_offset():
    prefix = "word " * 10
    text = prefix + "x" * 30 + " end"
    assert wrapped_rows(text, 78) is None
    assert wrapped_rows(text, 118) is None
    screen = read(composer([prefix, "x" * 30 + " end"], columns=80))
    assert not VisibleBox(text).matches(screen.box, 78)


@pytest.mark.parametrize("columns", [80, 120])
def test_greedy_wrap_retains_each_break_space_across_a_long_ascii_line(columns):
    text = "introduction " + "word " * 58 + "ok"
    assert len(text) == 305
    if columns == 80:
        rows = ["introduction " + "word " * 12] + ["word " * 15] * 3 + ["word ok"]
    else:
        rows = ["introduction " + "word " * 20, "word " * 23, "word " * 15 + "ok"]
    assert wrapped_rows(text, columns - 2) is None
    screen = read(composer(rows, columns=columns))
    assert not VisibleBox(text).matches(screen.box, columns - 2)


@pytest.mark.parametrize("columns", [80, 120])
def test_kept_long_line_structural_reproduction_moves_the_inner_prefix_whole(columns):
    # Safe structural reproduction, not the original measured text: seventeen
    # identical 17-character words with inner breaks after characters 10/13.
    word = "synthetic-q3-wrap"
    text = " ".join([word] * 17)
    assert len(word) == 17 and word.index("-") == 9 and word[12] == "-"
    assert len(text) == 305 and not text.endswith("\n")
    if columns == 80:
        rows = [(word + " ") * 4] * 4 + [word]
        assert list(map(len, rows)) == [72, 72, 72, 72, 17]
    else:
        rows = [(word + " ") * 6] * 2 + [" ".join([word] * 5)]
        assert list(map(len, rows)) == [108, 108, 89]
    assert wrapped_rows(text, columns - 2) is None
    screen = read(composer(rows, columns=columns))
    assert not VisibleBox(text).matches(screen.box, columns - 2)


@pytest.mark.parametrize("columns", [80, 120])
def test_overflow_moves_whole_when_its_first_inner_prefix_also_overflows(columns):
    prefix = "word " * ((columns - 5) // 5)
    text = prefix + "abcd-ef end"
    assert len(prefix) == columns - 5
    assert wrapped_rows(text, columns - 2) is None
    screen = read(composer([prefix, "abcd-ef end"], columns=columns))
    assert not VisibleBox(text).matches(screen.box, columns - 2)


@pytest.mark.parametrize("columns", [80, 120])
@pytest.mark.parametrize("word", ["q-", "q."])
def test_word_final_punctuation_is_not_an_inner_break(columns, word):
    prefix = "word " * ((columns - 10) // 5) + "final "
    text = prefix + word + " end"
    assert len(prefix) == columns - 4
    assert wrapped_rows(text, columns - 2) is None
    screen = read(composer([prefix, word + " end"], columns=columns))
    assert not VisibleBox(text).matches(screen.box, columns - 2)


@pytest.mark.parametrize("columns", [80, 120])
@pytest.mark.parametrize("word", ["a-b-cdefgh", "ab_cdefgh", "ab/cdefgh", "ab·cdefgh"])
def test_overflow_holds_when_the_first_inner_prefix_would_fit(columns, word):
    prefix = "word " * ((columns - 10) // 5)
    assert len(prefix) == columns - 10
    assert wrapped_rows(prefix + word + " end", columns - 2) is None


@pytest.mark.parametrize("columns", [80, 120])
def test_final_word_stays_at_the_last_supported_row_length(columns):
    text = "word " * ((columns - 5) // 5) + "q"
    assert len(text) == columns - 4
    assert wrapped_rows(text, columns - 2) is None
    screen = read(composer([text], columns=columns))
    assert not VisibleBox(text).matches(screen.box, columns - 2)


@pytest.mark.parametrize("columns", [80, 120])
@pytest.mark.parametrize("word", ["qq", "qqq"])
def test_final_word_placement_at_the_two_unknown_edge_lengths_holds(columns, word):
    prefix = "word " * ((columns - 5) // 5)
    assert len(prefix) == columns - 5
    assert wrapped_rows(prefix + word, columns - 2) is None


@pytest.mark.parametrize("columns", [80, 120])
@pytest.mark.parametrize("word", ["done", "abcd-ef", "end-"])
def test_final_word_past_the_unknown_edge_moves_by_the_same_prefix_rule(columns, word):
    prefix = "word " * ((columns - 5) // 5)
    text = prefix + word + "\n"
    assert wrapped_rows(text, columns - 2) is None
    screen = read(composer([prefix, word, ""], columns=columns))
    assert not VisibleBox(text).matches(screen.box, columns - 2)


@pytest.mark.parametrize("columns", [80, 120])
def test_final_word_past_the_unknown_edge_holds_when_its_inner_prefix_fits(columns):
    prefix = "word " * ((columns - 5) // 5)
    assert wrapped_rows(prefix + "a-bc", columns - 2) is None


@pytest.mark.parametrize("text", [
    "",
    " \n",
    "Muse Code 1.4.15\n" + "─" * 80 + "\n❯",
    "Muse Code 1.4.15\nA fictional dialog without a composer.",
    "An unfamiliar program layout.",
    frame().replace("❯", "unmeasured prompt"),
    frame().replace("─" * 120, "─" * 119, 1),
    frame("first\nunindented continuation"),
    frame().replace("Launch overrides", "Unmeasured footer"),
    frame() + "\x1b[?m",
    frame() + "\x00",
    composer(["An unfamiliar contextual hint."], foreground=MUTED),
    composer(["A fictional draft."], foreground=(1, 2, 3)),
    frame(colour("A fictional draft.", TYPED, (1, 2, 3))),
    frame(status=" steering the running turn"),
    frame(status=" interrupting run"),
    "Do you trust this workspace?\n1 Trust and continue\n2 Quit\nEsc quits",
    "You're using your quota fast\nKeep going",
    "Switch to Spark",
    "Turn on contributor mode",
    "You're using your quota fast",
    "Switched to Spark (saved as default)",
    "Contributor mode on",
])
def test_unreadable_starting_redraw_and_dialog_frames_have_unknown_activity(text):
    screen = read(text)
    assert screen.working is None
    assert screen.starting or screen.unknown is not None or screen.interruption is not None


@pytest.mark.parametrize("status", [" steering the running turn", " interrupting run"])
def test_transition_footer_does_not_hide_a_positive_working_indicator(status):
    screen = read(frame(status=status, working=True))
    assert screen.working is True and screen.unknown is None


def test_unsupported_payload_projection_does_not_erase_visible_idle_activity():
    text = "A fictional \u754c draft."
    screen = read(composer([text]))
    assert screen.working is False and screen.unknown is None
    assert wrapped_rows(text, 118) is None
    assert not VisibleBox(text).matches(screen.box, 118)
