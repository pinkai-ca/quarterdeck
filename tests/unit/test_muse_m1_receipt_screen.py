"""Claude-owned synthetic gates for Muse's receipt projection and its measured composer wrap.

Every screen, record and order here is invented. The wrap gates restate kept row and space facts as numbers
(row content lengths, where the break space stays, which piece moves) and build the expected native rows from
those facts by hand; no expected row is computed with the reader under test. Canonical forms come from the
framework's own post.typed. Nothing native, credentialed or provider-facing runs.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from quarterdeck.door import Door
from quarterdeck.harness import NAMES, get
from quarterdeck.harness.base import Harness
from quarterdeck.harness.muse import MuseHarness
from quarterdeck.harness.muse_record import CollapsedInput, first_timestamp, inputs
from quarterdeck.harness.muse_screen import (EXPAND_HINT, IDLE_HINT, MUTED, PROMPT, TYPED, VisibleBox, read,
                                           wrapped_rows)
from quarterdeck.harness.types import Input
from quarterdeck.input_receipt import confirmed
from quarterdeck.post import Message, typed

AT = datetime(2040, 1, 1, tzinfo=timezone.utc)
MOMENT = 2208988800654321  # microseconds: 2040-01-01, after any test's own start
STREAM = "synthetic-stream-qrst"
FILLER = "qrst "
TAIL = "· synthetic-edge-tail"


# -- synthetic material ---------------------------------------------------------------------------


def order(body: str, to: str = "synthetic-seat-qrst") -> str:
    """One canonical typed order from the synthetic ship, exactly as the Door would type it."""
    message = Message("ord-20400101-0000-ship-qrst", "order", "ship", to, AT, body, subject="synthetic edge")
    return typed([message])


def accepted(model: str, refill: str | None = None, ordinal: int = 1) -> dict:
    payload = dict(intent_id=f"synthetic-intent-{ordinal}", source_session_id=STREAM,
                   refill_blocks=[dict(kind="text", text=model if refill is None else refill)],
                   model_messages=[dict(content=[dict(kind="text", text=model)])])
    return dict(schema_version=1, payload_schema_version=1, payload_type="runtime.user_intent.accepted",
                payload=payload, recorded_at=MOMENT + ordinal * 1_000_000, id=f"synthetic-event-{ordinal}",
                sequence=ordinal, stream=dict(kind="session", id=STREAM))


def record(tmp_path, *rows):
    path = tmp_path / STREAM / "session.jsonl"
    path.parent.mkdir(exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def marker(count) -> str:
    return f"[Pasted Content {count} chars]"


def paint(text: str, foreground=TYPED, background=None) -> str:
    if not text:
        return ""
    start = "\x1b[38;2;" + ";".join(map(str, foreground)) + "m"
    if background is not None:
        start += "\x1b[48;2;" + ";".join(map(str, background)) + "m"
    return start + text + "\x1b[0m"


def styled(rows, columns, foreground=TYPED, background=None, prompt_background=None, working=False, footer=""):
    """A composer in the measured styling: muted rules, a blue column-0 prompt, two-space continuation rows."""
    rule = paint("─" * columns, MUTED)
    lines = ["  " + paint("Muse Code", TYPED) + " 1.4.15"]
    if working:
        lines.append("Thinking (3s · esc to interrupt)")
    lines.append(rule)
    lines.append(paint("❯ ", PROMPT, prompt_background) + paint(rows[0], foreground, background))
    lines += [paint("  ", MUTED) + paint(row, foreground, background) for row in rows[1:]]
    lines.append(rule)
    lines.append("  echo · /s/y/n/workspace · Launch overrides" + footer)
    return "\n".join(lines)


# -- every adapter but Muse keeps identity and the legacy boundary comparison --------------------------


@pytest.mark.parametrize("name", [name for name in NAMES if name != "muse"])
def test_every_other_adapter_keeps_identity_projection_and_boundary_whitespace_receipts(name, tmp_path, monkeypatch):
    harness = get(name)
    assert type(harness).expected_input is Harness.expected_input
    assert harness.exact_input_text is False
    form = order("A synthetic request.")
    assert harness.expected_input(Input(form, AT), form) == form
    monkeypatch.setattr(harness, "inputs", lambda path: [Input("  " + form.strip() + " ", AT)])
    assert confirmed(harness, tmp_path / "unused-record", form, AT) == "delivered"


# -- the record projection: the typed form minus exactly one final LF ---------------------------------


def test_a_canonical_typed_order_is_confirmed_by_its_form_minus_one_final_lf(tmp_path):
    form = order("Check · one · two.")
    assert form.endswith("\n") and not form.endswith("\n\n")
    path = record(tmp_path, accepted(form[:-1]))
    item, = inputs(path)
    assert type(item) is not CollapsedInput
    assert confirmed(MuseHarness(), path, form, first_timestamp(path)) == "delivered"


@pytest.mark.parametrize("change", ["final LF kept", "double final LF", "final space for LF",
                                    "internal difference", "leading space"])
def test_any_other_record_text_is_held_never_matched_by_boundary_whitespace(tmp_path, change):
    form = order("Check · one · two.")
    shown = {"final LF kept": form,
             "double final LF": form + "\n",
             "final space for LF": form[:-1] + " ",
             "internal difference": form[:-1].replace(" · one", " ·  one", 1),
             "leading space": " " + form[:-1]}[change]
    assert shown != form[:-1]
    if change != "internal difference":
        assert shown.strip() == form.strip()  # the legacy boundary comparison would have accepted it
    path = record(tmp_path, accepted(shown))
    assert confirmed(MuseHarness(), path, form, first_timestamp(path)) is None


@pytest.mark.parametrize("body", ["A synthetic request.", "A synthetic\ttabbed request."])
def test_a_collapsed_receipt_needs_the_counted_original_with_each_tab_as_four(tmp_path, body):
    form = order(body)
    count = len(form) + 3 * form.count("\t")
    path = record(tmp_path, accepted(form[:-1], refill=marker(count)))
    item, = inputs(path)
    assert isinstance(item, CollapsedInput) and item.characters == str(count)
    assert confirmed(MuseHarness(), path, form, first_timestamp(path)) == "delivered"


@pytest.mark.parametrize("fault", ["one over", "one under", "tab counted as one", "uncounted character",
                                   "extra marker text"])
def test_a_collapsed_receipt_whose_marker_does_not_count_the_form_is_held(tmp_path, fault):
    body = "A synthetic 界 request." if fault == "uncounted character" else "A synthetic\trequest."
    form = order(body)
    count = len(form) + 3 * form.count("\t")
    refill = {"one over": marker(count + 1),
              "one under": marker(count - 1),
              "tab counted as one": marker(len(form)),
              "uncounted character": marker(len(form)),
              "extra marker text": marker(count) + " more"}[fault]
    path = record(tmp_path, accepted(form[:-1], refill=refill))
    assert confirmed(MuseHarness(), path, form, first_timestamp(path)) is None


def test_an_exact_record_input_older_than_the_attempt_proves_nothing(tmp_path):
    form = order("A synthetic request.")
    path = record(tmp_path, accepted(form[:-1]))
    later = datetime(2041, 1, 1, tzinfo=timezone.utc)
    assert confirmed(MuseHarness(), path, form, later) is None


def test_a_form_outside_the_canonical_shape_is_never_confirmed_by_stripped_text(tmp_path):
    # post.typed never ends in two LFs; whatever the native rule for such a form, stripping is not it.
    path = record(tmp_path, accepted("A synthetic request."))
    assert confirmed(MuseHarness(), path, "A synthetic request.\n\n", first_timestamp(path)) is None


@pytest.mark.parametrize("exact", [True, False])
def test_the_door_confirms_only_by_the_exact_projection_and_never_types_again(tmp_path, exact):
    form = order("A synthetic request.")
    path = record(tmp_path, accepted(form[:-1] if exact else form[:-1] + " "))
    terminal = SimpleNamespace(pastes=[], keys_sent=[])
    terminal.paste = lambda *a, **kw: terminal.pastes.append(a)
    terminal.keys = lambda *a, **kw: terminal.keys_sent.append(a)
    seat = SimpleNamespace(name="synthetic-seat-qrst", harness=MuseHarness(), tmux=terminal,
                           record=lambda: path, pane=lambda: None)
    delivery = Door(seat).confirm(form, seconds=0)
    assert delivery.result == ("delivered" if exact else "blind")
    assert terminal.pastes == [] and terminal.keys_sent == []


# -- the styled composer: only measured colours read as text -----------------------------------------


@pytest.mark.parametrize("shape", ["unknown tip colour", "text background", "256-colour text",
                                   "muted hint while working", "prompt background"])
def test_unmeasured_composer_styles_give_no_box(shape):
    if shape == "unknown tip colour":
        text = styled(["A synthetic tip."], 80, foreground=(1, 2, 3))
    elif shape == "text background":
        text = styled(["A synthetic draft."], 80, background=(38, 56, 84))
    elif shape == "256-colour text":
        text = styled(["A synthetic draft."], 80).replace("\x1b[38;2;204;211;219m", "\x1b[38;5;250m")
    elif shape == "muted hint while working":
        text = styled([IDLE_HINT], 80, foreground=MUTED, working=True)
    else:
        text = styled(["A synthetic draft."], 80, prompt_background=(38, 56, 84))
    screen = read(text)
    assert screen.box is None and screen.unknown is not None


def test_a_folded_composer_marker_matches_only_the_tab_counted_original():
    form = order("A synthetic\ttabbed request.")
    screen = read(styled([marker(len(form) + 3)], 80, footer="\n" + EXPAND_HINT))
    assert screen.box.folded
    assert VisibleBox(form).matches(screen.box, 78)
    assert not VisibleBox(form.replace("\t", " ")).matches(screen.box, 78)


# -- wrap at the measured widths: greedy word plus following space within W-3 -------------------------


def edge_lines(columns: int) -> list[str]:
    """S3d's three body lines: the dot word lands one before, on, and one past the W-3 content edge."""
    return [FILLER * ((columns - 10) // 5) + "qqqqq " + TAIL,
            FILLER * ((columns - 5) // 5) + "q " + TAIL,
            FILLER * ((columns - 5) // 5) + "qq " + TAIL]


def edge_rows(columns: int) -> list[str]:
    """The native rows S3d kept for those lines, restated from their measured lengths and break spaces."""
    first, second, third = (FILLER * ((columns - 10) // 5) + "qqqqq ", FILLER * ((columns - 5) // 5) + "q ",
                            FILLER * ((columns - 5) // 5))
    assert (len(first), len(second), len(third)) == (columns - 4, columns - 3, columns - 5)
    assert first.endswith(" ") and second.endswith(" ") and third.endswith(" ")
    return [first, TAIL, second, TAIL, third, "qq " + TAIL]


@pytest.mark.parametrize("columns", [80, 120])
def test_the_measured_middle_dot_edge_rows_are_the_projection_of_their_lines(columns):
    text = "\n".join(edge_lines(columns)) + "\n"
    screen = read(styled(edge_rows(columns) + [""], columns))
    assert screen.unknown is None and screen.box.columns == columns - 2
    assert VisibleBox(text).matches(screen.box, columns - 2)


def test_the_whole_edge_envelope_at_120_matches_its_unwrapped_header_and_footer():
    form = order("\n".join(edge_lines(120)))
    header, *_, footer, end = form.split("\n")
    assert len(footer) == 83 and end == ""
    screen = read(styled([header] + edge_rows(120) + [footer, ""], 120))
    assert VisibleBox(form).matches(screen.box, 118)


def test_the_80_column_envelope_holds_on_its_footer_broken_at_a_hyphen():
    form = order("\n".join(edge_lines(80)))
    header, *_, footer, _ = form.split("\n")
    head, rest = footer[:76], footer[76:]
    assert head.endswith("synthetic-seat-") and rest == "qrst.md"  # the kept break: after the last fitting hyphen
    screen = read(styled([header] + edge_rows(80) + [head, rest, ""], 80))
    assert screen.unknown is None
    assert not VisibleBox(form).matches(screen.box, 78)
    assert wrapped_rows(footer, 78) is None


@pytest.mark.parametrize("shape", ["double space", "leading space", "trailing space", "hyphen prefix fits",
                                   "final word reaches the budget", "unmeasured width"])
def test_unmeasured_wrap_shapes_have_no_projection(shape):
    long = FILLER * 14
    line = {"double space": long.replace(" ", "  ", 1) + "qqqqqq tail",
            "leading space": " " + long + "qqqqqq tail",
            "trailing space": long + "qqqqqq tail ",
            "hyphen prefix fits": long + "qq-rstuvwxyz tail",
            "final word reaches the budget": FILLER * 15 + "qq",
            "unmeasured width": FILLER * 19 + "tail"}[shape]
    columns = 98 if shape == "unmeasured width" else 78
    assert len(line) >= columns - 1
    assert wrapped_rows(line, columns) is None


@pytest.mark.parametrize("native", ["break space dropped", "break inside a word"])
def test_native_rows_that_differ_from_the_greedy_shape_do_not_match(native):
    text = FILLER * 15 + "qqqqqq tail"
    rows = ([FILLER * 15, "qqqqqq tail"] if native == "break space dropped" else
            [FILLER * 15 + "qq", "qqqq tail"])
    if native == "break space dropped":
        rows[0] = rows[0].rstrip(" ")
    screen = read(styled(rows, 80))
    assert screen.unknown is None
    assert not VisibleBox(text).matches(screen.box, 78)


def test_a_line_that_needs_no_wrap_projects_unchanged_at_an_unmeasured_width():
    text = FILLER * 18 + "tail\n"
    screen = read(styled([text[:-1], ""], 100))
    assert VisibleBox(text).matches(screen.box, 98)
