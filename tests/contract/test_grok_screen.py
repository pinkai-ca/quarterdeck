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
def test_trailing_blank_rows_are_retained_and_counted_exactly(count):
    adapter = GrokHarness()
    screen = adapter.screen(with_blank_rows(count))
    assert screen.unknown is None
    assert len(screen.box.split("\n")) == 3 + count
    assert all(row == " " * 71 for row in screen.box.split("\n")[-count:])
    prefix = "First synthetic line.\nSecond line has [brackets].\nLast line: done."
    assert adapter.box(prefix + "\n" * count).matches(screen.box, 70)
    assert not adapter.box(prefix + "\n" * (count - 1)).matches(screen.box, 70)
    assert not adapter.box(prefix + "\n" * (count + 1)).matches(screen.box, 70)


def test_interior_blank_input_line_is_preserved():
    adapter = GrokHarness()
    screen = adapter.screen(with_blank_rows(1, interior=True))
    wanted = "First synthetic line.\n\nSecond line has [brackets].\nLast line: done."
    assert screen.unknown is None
    assert adapter.box(wanted).matches(screen.box, 71)
    assert not adapter.box(wanted.replace("\n\n", "\n")).matches(screen.box, 71)


def test_multiple_empty_rows_are_not_assumed_to_be_an_empty_composer():
    screen = GrokHarness().screen(with_blank_rows(1, empty=True))
    assert screen.box == " " * 71 + "\n" + " " * 71
    assert not GrokHarness().box("\n").matches(screen.box, 70)


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
def test_folded_visual_comparison_is_derived_from_payload_line_count(leading, trailing, count):
    adapter = GrokHarness()
    screen = adapter.screen(fixture("folded-multiline").replace("Pasted: 24 lines", f"Pasted: {count} lines"))
    # Keep the measured right border fixed when a display count changes width.
    assert screen.unknown is None
    assert screen.interruption is None and screen.box is not None
    wanted = "\n" * leading + "\n".join(f"Synthetic item {i}." for i in range(24)) + "\n" * trailing
    collision = "\n" * leading + "\n".join(f"Different content {i}." for i in range(24)) + "\n" * trailing
    # This is only a visual projection: different text can share its count.
    # Neither result confirms an input receipt or a delivered message.
    assert wanted != collision
    assert adapter.box(wanted).matches(screen.box, adapter.box_columns(80))
    assert adapter.box(collision).matches(screen.box, adapter.box_columns(80))
    marker = f"[Pasted: {count} lines]"
    assert adapter.box(wanted).matches(marker, adapter.box_columns(80))
    assert not adapter.box(wanted).matches(f"[Pasted: {count + 1} lines]", adapter.box_columns(80))


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


@pytest.mark.parametrize("mode", ["color", "mono"])
@pytest.mark.parametrize("payload,count", [
    ("Alpha.\nBeta.", None),
    ("Alpha.\nBeta.\n", None),
    ("Alpha.\nBeta.\nGamma.", None),
    ("Alpha.\nBeta.\nGamma.\n", None),
    ("Alpha.\nBeta.\nGamma.\nDelta.", 4),
    ("Alpha.\nBeta.\nGamma.\nDelta.\n", 4),
    ("Alpha.\nBeta.\nGamma.\nDelta.\nEpsilon.", 5),
    ("Alpha.\nBeta.\nGamma.\n\n", 4),
    ("\nAlpha.\nBeta.\nGamma.", 4),
    ("\nAlpha.\nBeta.\n", None),
    ("Alpha.\n\n\n", None),
])
def test_measured_fold_threshold_and_lf_shapes(mode, payload, count):
    adapter = GrokHarness()
    if count is not None:
        text = fixture(f"fold-boundary-{mode}").replace("Pasted: 4 lines", f"Pasted: {count} lines")
    else:
        text = fixture(f"idle-{mode}")
        rows = text.splitlines()
        at = next(i for i, row in enumerate(rows) if " ❯ " in row)
        plain = payload.split("\n")
        if mode == "color":
            rows[at:at + 1] = ["  \x1b[38;2;80;80;88m│\x1b[38;2;225;225;225m"
                               + (" ❯ " if i == 0 else "   ") + row.ljust(71)
                               + "\x1b[38;2;80;80;88m│\x1b[39m  " for i, row in enumerate(plain)]
        else:
            rows = [row.rstrip(" ") for row in rows]
            rows[at:at + 1] = ["  │ " + ("❯ " if i == 0 else "  ") + row.ljust(71) + "│"
                               for i, row in enumerate(plain)]
        rows[-2] = "  Enter:send  │  Alt+Enter:newline  │  Shift+Tab:mode  │  Ctrl+x:shortcuts"
        text = "\n".join(rows) + "\n"
    screen = adapter.screen(text)
    assert screen.unknown is None and screen.interruption is None
    assert adapter.box(payload).matches(screen.box, adapter.box_columns(80))
    if count is None:
        assert adapter.box(payload).folded is None
        assert not adapter.box(payload).matches("[Pasted: 3 lines]", 70)
    else:
        assert adapter.box(payload).folded is not None
        assert not adapter.box(payload).matches(f"[Pasted: {count + 1} lines]", 70)


@pytest.mark.parametrize("payload", [
    "A.\r\nB.\r\nC.\r\nD.",
    "A.\t\nB.\nC.\nD.",
    "A.\v\nB.\nC.\nD.",
    "A.\x85\nB.\nC.\nD.",
    "A.\u2028\nB.\nC.\nD.",
])
def test_unmeasured_control_and_line_ending_layouts_have_no_projection(payload):
    box = GrokHarness().box(payload)
    assert box.folded is None
    assert not box.matches(payload, 70)
    assert not box.matches("[Pasted: 4 lines]", 70)


@pytest.mark.parametrize("name,spinner,payload", [
    ("working-response", "⠹ Responding…", ""),
    ("working-tool", "⠴ Synthetic shell command…", ""),
    ("working-draft", "⠸ Synthetic shell command…",
     "qd > answer from synthetic · ans-bbbb\nA harmless synthetic follow-up.\nqd > inbox: post/inbox/example.md\n"),
])
def test_measured_working_hints_and_visible_draft(name, spinner, payload):
    adapter = GrokHarness()
    screen = adapter.screen(fixture(name))
    assert screen.unknown is None and screen.interruption is None
    assert screen.working is True
    assert spinner in screen.text and "[stop]" in screen.text
    assert screen.shows == {"model": "Grok 4.7", "effort": "high"}
    assert adapter.box(payload).matches(screen.box, adapter.box_columns(80))


@pytest.mark.parametrize("old,new", [("Enter:queue", "Enter:steer"), ("Ctrl+c:cancel", "Ctrl+c:new-action")])
def test_unmeasured_working_hint_stays_unknown(old, new):
    screen = GrokHarness().screen(fixture("working-draft").replace(old, new))
    assert screen.unknown == "unmeasured Grok composer hint"
    assert screen.box is None


# The 14 November crew paste, at a 256-column pane. Private words are replaced with
# equal-width synthetic text. The composer frame, the empty interior, the footer and
# the hint row are the captured bytes.
WIDE_DRAFT = (
    "Synthetic wide pane one. Synthetic wide pane on",
    "Synthetic wide pane two. Synthetic wide pane two. Synthetic wide pane two. "
    "Synthetic wide pane two. Synthetic wide pane two. Synthetic wide pane two. "
    "Synthetic wide pane two. Synthetic wide pane two. Synthetic wide pane two. "
    "Synthetic wide pane",
    "Synthetic wide pane three. Synthetic wide pane three. Synthetic wide pane three. "
    "Synthetic wide pane three. Synthetic wide pane three. Synthetic wide pane three. "
    "Synthetic wide pane three. Sy",
    "Synthetic wide pane four. Synthet",
)
WIDE_HINT = (
    "  Enter:queue  │  Alt+Enter:newline  │  Shift+Tab:mode  │  Ctrl+c:cancel  │  "
    "Enter Enter:send now  │  Ctrl+x:shortcuts"
)


def test_measured_wide_working_draft_hint_reads_the_box():
    """A 256-column working draft draws Ctrl+x and the send-now chip, and the box is still the draft."""
    adapter = GrokHarness()
    native = fixture("working-draft-256")
    rows = native.split("\n")
    assert rows[-1] == "" and len(rows[-2]) == 256
    assert rows[-2].startswith(WIDE_HINT) and set(rows[-2][len(WIDE_HINT):]) == {" "}
    assert rows[0].startswith("  ╭") and rows[0].endswith("╮  ") and len(rows[0]) == 256
    assert rows[5].strip(" │") == ""  # the captured empty interior, borders only
    assert "Grok 4.7 (high) · always-approve" in rows[6]
    body = [row[len("  │ ❯ " if i == 0 else "  │   "):-3].rstrip(" ") for i, row in enumerate(rows[1:5])]
    assert tuple(body) == WIDE_DRAFT
    screen = adapter.screen(native)
    assert screen.unknown is None and screen.interruption is None
    assert screen.working is True
    assert screen.shows == {"model": "Grok 4.7", "effort": "high"}
    assert adapter.keys.send_now is None
    assert adapter.box_columns(256) == 246
    assert adapter.box("\n".join(WIDE_DRAFT) + "\n").matches(screen.box, adapter.box_columns(256))


@pytest.mark.parametrize("old,new", [
    ("Enter:queue", "Enter:steer"),
    ("Enter Enter:send now", "Enter Enter:send later"),
    ("Ctrl+x:shortcuts", "Ctrl+x:other"),
])
def test_unmeasured_wide_working_hint_stays_blind(old, new):
    native = fixture("working-draft-256")
    assert native.count(old) == 1
    screen = GrokHarness().screen(native.replace(old, new, 1))
    assert screen.unknown == "unmeasured Grok composer hint" and screen.box is None


WIDE_FOLDED_PAYLOAD = (
    "Reply with exactly the word ok.\n"
    "Synthetic line two.\nSynthetic line three.\nSynthetic line four.\nSynthetic line five."
)


@pytest.mark.parametrize("name", ["folded-draft-256", "folded-draft-indexed-256"])
def test_measured_wide_folded_paste_before_submit(name):
    adapter = GrokHarness()
    screen = adapter.screen(fixture(name))
    assert screen.unknown is None and screen.interruption is None and screen.working is False
    assert screen.box is not None and screen.box.strip() == "[Pasted: 5 lines]"
    assert adapter.box(WIDE_FOLDED_PAYLOAD).matches(screen.box, adapter.box_columns(256))
    assert not adapter.box(WIDE_FOLDED_PAYLOAD.rsplit("\n", 1)[0]).matches(screen.box, adapter.box_columns(256))


@pytest.mark.parametrize("name", ["idle-after-fold-256", "idle-after-fold-indexed-256"])
def test_measured_wide_composer_after_the_folded_turn(name):
    adapter = GrokHarness()
    native = fixture(name)
    screen = adapter.screen(native)
    assert screen.unknown is None and screen.interruption is None and screen.working is False
    assert screen.box is not None and not screen.box.strip()
    assert native.count("shortcuts") == 1
    altered = adapter.screen(native.replace("shortcuts", "other"))
    assert altered.unknown == "unmeasured Grok composer hint" and altered.box is None


def test_measured_wide_indexed_text_draft():
    adapter = GrokHarness()
    screen = adapter.screen(fixture("text-draft-indexed-256"))
    assert screen.unknown is None and screen.working is False
    assert adapter.box(WIDE_FOLDED_PAYLOAD.split("\n", 1)[0]).matches(screen.box, adapter.box_columns(256))


def test_wide_indexed_fold_keeps_unmeasured_style_unknown():
    native = fixture("folded-draft-indexed-256")
    assert native.count("38;5;251mPasted") == 1
    screen = GrokHarness().screen(native.replace("38;5;251mPasted", "38;5;250mPasted"))
    assert screen.unknown == "unmeasured Grok composer text styling" and screen.box is None


def test_transcript_working_words_do_not_replace_native_hint_evidence():
    text = "   ⠹ Responding… [stop]\nCtrl+c:cancel\n" + fixture("idle-mono")
    screen = GrokHarness().screen(text)
    assert screen.unknown is None
    assert screen.working is False  # read from the known idle hint, not transcript text


@pytest.mark.parametrize("name", ["queued-tool", "queued-response"])
def test_measured_queued_footer_does_not_supply_a_record_receipt(name):
    adapter = GrokHarness()
    before = adapter.screen(fixture("working-tool"))
    after = adapter.screen(fixture(name))
    assert after.unknown is None and after.working is True
    assert after.box is not None and not after.box.strip()
    assert "▾ Queued 1" in after.text
    assert adapter.queued_input("A fictional payload.", before, after) is None
    assert adapter.keys.submit == "Enter" and adapter.keys.interrupt == "C-c"
    assert adapter.keys.send_now is None


def test_unmeasured_queued_control_stays_unknown():
    screen = GrokHarness().screen(fixture("queued-response").replace("Ctrl+;:queue", "Ctrl+;:steer"))
    assert screen.unknown == "unmeasured Grok composer hint" and screen.box is None


def test_measured_shift_enter_draft_hint_reads_the_folded_paste_idle():
    """Measured on 13 November at 80 x 24 under VTE_VERSION=8400: the idle draft's hint names Shift+Enter."""
    adapter = GrokHarness()
    screen = adapter.screen(fixture("folded-shift-enter-80"))
    assert screen.unknown is None and screen.interruption is None
    assert screen.working is False
    assert screen.shows == {"model": "Grok 4.7", "effort": "high"}
    wanted = "\n".join(f"Synthetic item {i}." for i in range(42))
    assert adapter.box(wanted).matches(screen.box, adapter.box_columns(80))
    assert not adapter.box("\n".join(f"Synthetic item {i}." for i in range(41))).matches(
        screen.box, adapter.box_columns(80))


@pytest.mark.parametrize("old,new", [
    ("Shift+Enter", "Shift+Space"),  # another key named for the newline
    (":send", ":queue"),  # a send that is not the idle one
    ("Shift+Tab", "Shift+Esc"),  # a mode key not measured
])
def test_unmeasured_shift_enter_hint_stays_blind(old, new):
    native = fixture("folded-shift-enter-80")
    assert native.count(old) == 1
    screen = GrokHarness().screen(native.replace(old, new))
    assert screen.unknown == "unmeasured Grok composer hint" and screen.box is None
