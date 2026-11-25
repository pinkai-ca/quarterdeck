"""Grok's empty composer reads box '' only with the terminal cursor at the prompt's first cell.

Grok 1.0.77 draws typed spaces exactly as its padding, so its reader returns an empty composer's box as the
padding spaces, and the human waking line, which needs box '', never saw it empty. The captain's ruling: a
composer whose one row is only padding reads '' only where tmux's cursor, read with the frame, sits at the
prompt's first cell. Measured live on 13 November on a throwaway human session with its own tmux server: the
cursor sits at column 6 of the prompt row at 80 x 24 and at 112 x 35, and two typed spaces move it to column 8.
The frames are those measurements, with the workspace path and the check's token made synthetic. tmux here is a
stand-in that draws them; no program, server, credential or provider is touched.
"""

from __future__ import annotations

import json

import pytest

from quarterdeck import human_wake, post
from quarterdeck.door import Door
from quarterdeck.harness import grok, grok_screen
from quarterdeck.harness.claude import ClaudeHarness
from quarterdeck.tmux import ControlFrame
from tests.conftest import FIXTURES
from tests.unit.test_grok_seeded_composer import EXACT, GrokTmux, composer, lines, pastes, seeded  # noqa: F401


def measured(name: str) -> dict:
    return json.loads((FIXTURES / "grok/1.0.77" / f"{name}.json").read_text())


def frame(name: str) -> str:
    return "\n".join(measured(name)["lines"]) + "\n"


EMPTY_80, EMPTY_112_WORKING = "cursor-empty-80", "cursor-empty-112-working"
SPACES_80, SPACES_112, SPACES_HOME_80 = "cursor-spaces-80", "cursor-spaces-112", "cursor-spaces-home-80"
# The ordinary idle composer at 112 x 35, after a turn; its prompt row is the row measured at 112 (cursor-empty-112).
IDLE_112 = "\n".join(lines("training-answered-after-turn-256")) + "\n"


class CursorTmux(GrokTmux):
    """Draws a measured frame while the box is empty, the draft composer once something is pasted, and answers
    the bracketed control-frame reading with the cursor as measured, or as a test sets it."""

    def __init__(self, pane, empty: str, cursor, visible: bool = True):
        super().__init__(pane)
        self.empty, self.cursor, self.visible = empty, cursor, visible
        self.control: str | None = None  # a second frame that differs from the first, where a test sets one
        self.control_reads = 0

    def frame(self) -> str:
        return self.empty if self.box == "" else composer(self.box)

    def read_control_frame(self, ident):
        self.control_reads += 1
        pane = self.panes.get(ident)
        if pane is None:
            return None
        return ControlFrame(pane, self.control or self.frame(), self.visible, self.cursor)


@pytest.fixture
def ordinary(seeded, monkeypatch):
    """A Grok seat with no seed, on an ordinary measured composer, with a cursor reading."""

    def make(empty: str, cursor, *, width: int, height: int, seat_name: str = "worker-1", visible: bool = True):
        d = seeded(seat_name, opt_out=False)
        pane = d.tmux.panes["%1"]
        pane.width, pane.height = width, height
        tmux = CursorTmux(pane, empty, cursor, visible)
        tmux.record = d.record
        d.seat.tmux = tmux
        d.tmux = tmux
        return d

    return make


# -- the reader names the cell; it never reads the padding empty itself ------------------------------------


@pytest.mark.parametrize("name, row", [(EMPTY_80, 19), (EMPTY_112_WORKING, 30), (SPACES_80, 19),
                                       (SPACES_112, 30), (SPACES_HOME_80, 19)])
def test_the_reader_reads_padding_and_names_the_prompts_first_cell(name, row):
    screen = grok_screen.read(frame(name))
    assert screen.box is not None and screen.box != "" and not screen.box.strip(" ")
    assert len(screen.box) == measured(name)["width"] - 9
    assert grok_screen.empty_cell(screen) == (6, row)


def test_the_measured_cursor_is_at_the_first_cell_when_empty_and_moves_with_typed_spaces():
    assert measured(EMPTY_80)["cursor"] == [6, 19] and measured(EMPTY_112_WORKING)["cursor"] == [6, 30]
    assert measured(SPACES_80)["cursor"] == [8, 19] and measured(SPACES_112)["cursor"] == [8, 30]
    assert all(measured(name)["cursor_visible"] for name in (EMPTY_80, EMPTY_112_WORKING, SPACES_80, SPACES_112))


def test_no_cell_is_named_for_a_draft_a_question_or_another_program():
    assert grok_screen.empty_cell(grok_screen.read(composer("draft"))) is None
    # The fresh form names its first cell too: in a person's session the Door reads it idle only with the cursor
    # there (test_grok_fresh_human_cursor).
    assert grok_screen.empty_cell(grok_screen.read(composer(""))) == (6, 30)
    question = "\n".join(lines("training-question-256")) + "\n"
    assert grok_screen.empty_cell(grok_screen.read(question)) is None
    claude = ClaudeHarness()
    assert claude.empty_cell(grok_screen.read(frame(EMPTY_80))) is None


# -- the door reads it empty only with the cursor there ------------------------------------------------


def test_the_empty_composer_at_80_columns_reads_empty(ordinary):
    d = ordinary(frame(EMPTY_80), (6, 19), width=80, height=24)
    screen = Door(d.seat).screen()
    assert screen.box == "" and screen.working is False and screen.unknown is None
    assert human_wake.screen_hold(screen) is None
    assert Door(d.seat).reading().open


@pytest.mark.parametrize("width", [80, 319])
def test_xai_key_startup_reads_an_empty_composer_in_the_owned_pane(ordinary, width):
    name = f"xai-key-startup-{width}-styled"
    captured = measured(name)
    d = ordinary(frame(name), tuple(captured["cursor"]), width=captured["width"], height=captured["height"])
    door = Door(d.seat)

    assert door.screen().box == ""
    assert door.reading().open

    d.tmux.cursor = (8, captured["cursor"][1])
    assert door.screen().box == " " * (width - 9)

    d.tmux.panes["%1"].pid += 1
    assert door.screen() is None
    assert d.tmux.sent == []


@pytest.mark.parametrize("width", [80, 319])
@pytest.mark.parametrize("style", ["plain", "styled"])
def test_xai_key_startup_and_short_draft_go_through_the_ordinary_door(ordinary, monkeypatch, width, style):
    name = f"xai-key-startup-{width}-{style}"
    captured = measured(name)
    draft_name = f"xai-key-draft-{width}-{style}"
    startup, draft = frame(name), frame(draft_name)
    payload = "synthetic draft not to be sent"
    d = ordinary(startup, tuple(captured["cursor"]), width=captured["width"], height=captured["height"],
                 visible=captured["cursor_visible"])
    draft_info = measured(draft_name)
    assert (draft_info['width'], draft_info['height']) == (captured['width'], captured['height'])
    assert draft_info['cursor_visible'] and draft_info['cursor'] == [6 + len(payload), captured['cursor'][1]]
    monkeypatch.setattr(d.tmux, "frame", lambda: draft if d.tmux.box else startup)
    paste = d.tmux.paste

    def pasted(*args, **kwargs):
        paste(*args, **kwargs)
        d.tmux.cursor = tuple(draft_info['cursor'])

    monkeypatch.setattr(d.tmux, "paste", pasted)
    door = Door(d.seat)

    delivery = door.deliver(payload)

    assert delivery.result == "delivered"
    assert d.tmux.sent == [("paste", payload), ("keys", ("Enter",))]
    assert d.seat.harness.inputs(d.record)[-1].text == payload


@pytest.mark.parametrize("receipt, result", [("exact", "delivered"), ("trimmed", "delivered"), ("changed", "blind")])
def test_xai_key_multiline_draft_submits_once_and_uses_ordinary_receipts(ordinary, monkeypatch, receipt, result):
    draft_name = "xai-key-multiline-draft-80-plain"
    capture = measured(draft_name)
    draft, payload = frame(draft_name), capture["payload"]
    width, height = capture["width"], capture["height"]
    # Use the measured startup composer at the same width, retaining its
    # bottom rows in the stand-in's shorter pane.
    startup_info = measured("xai-key-startup-80-plain")
    startup = "\n".join(startup_info["lines"][-height:]) + "\n"
    x, y = startup_info["cursor"]
    d = ordinary(startup, (x, y - (startup_info["height"] - height)), width=width, height=height)
    monkeypatch.setattr(d.tmux, "frame", lambda: draft if d.tmux.box else startup)
    door = Door(d.seat)
    recorded = {"exact": payload, "trimmed": payload.removesuffix("\n"),
                "changed": payload.replace("placeholder", "stand-in")}[receipt]
    keys, confirm = d.tmux.keys, door.confirm

    def record_input(ident, *pressed):
        d.tmux.box = recorded
        keys(ident, *pressed)

    monkeypatch.setattr(d.tmux, "keys", record_input)
    monkeypatch.setattr(door, "confirm", lambda text: confirm(text, seconds=0))
    delivery = door.deliver(payload)

    assert d.tmux.sent == [("paste", payload), ("keys", ("Enter",))]
    assert d.seat.harness.inputs(d.record)[-1].text == recorded
    assert delivery.result == result


@pytest.mark.parametrize("first", ["wrapped", "wrapped-no-lf", "folded"])
def test_xai_key_styled_delivery_resumes_only_after_the_turn_settles(ordinary, monkeypatch, first):
    capture = measured("xai-key-composer-78-styled")
    frames = {name: "\n".join(lines) + "\n" for name, lines in capture["frames"].items()}
    payloads = capture["payloads"]
    phase, draft = "startup", first
    d = ordinary(frames[phase], tuple(capture["empty_cursor"]), width=capture["width"], height=capture["height"])
    monkeypatch.setattr(d.tmux, "frame", lambda: frames[draft if d.tmux.box else phase])
    keys = d.tmux.keys

    def enter(ident, *pressed):
        nonlocal phase
        keys(ident, *pressed)
        phase = "working"

    monkeypatch.setattr(d.tmux, "keys", enter)
    first_payload = payloads[first]

    assert Door(d.seat).deliver(first_payload).result == "delivered"
    working = Door(d.seat).screen()
    assert working.working is True and working.box is None

    draft = "wrapped" if first == "folded" else "folded"
    second_payload = payloads[draft]
    assert Door(d.seat).deliver(second_payload).result == "withheld"
    assert d.tmux.sent == [("paste", first_payload), ("keys", ("Enter",))]

    phase = "idle"
    assert Door(d.seat).screen().box == ""
    assert Door(d.seat).deliver(second_payload).result == "delivered"
    assert d.tmux.sent == [("paste", first_payload), ("keys", ("Enter",)),
                           ("paste", second_payload), ("keys", ("Enter",))]
    assert [item.text for item in d.seat.harness.inputs(d.record)[-2:]] == [first_payload, second_payload]


@pytest.fixture
def keyed_draft(ordinary, monkeypatch):
    capture = measured("xai-key-draft-sizes-80")
    startup = measured("xai-key-startup-80-plain")
    height, width = capture["height"], capture["width"]
    empty = "\n".join(startup["lines"][-height:]) + "\n"
    cursor_x, cursor_y = startup["cursor"]
    cursor = cursor_x, cursor_y - startup["height"] + height

    def make(form):
        draft = capture["drafts"][form]
        text = "\n".join(draft["lines"]) + "\n"
        d = ordinary(empty, cursor, width=width, height=height)
        monkeypatch.setattr(d.tmux, "frame", lambda: d.tmux.override or text if d.tmux.box else empty)
        door = Door(d.seat)
        door.observation_seconds = 0
        return d, door, draft["payload"], text

    return make


@pytest.mark.parametrize("form,changed", [
    ("mixed", None),
    ("mixed", ("alpha", "omega")),
    ("submitted-fold", None),
    ("submitted-fold", ("fixture-03", "fixture-04")),
    ("submitted-fold", ("--re fixture-03", "--re fixture-04")),
    ("submitted-fold", ("marker", "[Pasted: 8 lines]")),
    ("complete-wrap", None),
    ("complete-wrap", ("final LF", "")),
])
def test_keyed_draft_compares_visible_payload_before_one_enter(keyed_draft, form, changed):
    d, door, payload, frame = keyed_draft(form)
    if changed is not None:
        old, new = changed
        if old == "marker":
            payload = new
        elif old == "final LF":
            payload = payload.removesuffix("\n")
        else:
            payload = payload.replace(old, new, 1)
    shown = d.seat.harness.screen(frame)
    box = d.seat.harness.box(payload)
    matches = changed is None

    assert door.matches(box, shown) is matches
    assert door.held_matches(box, shown) is matches

    delivery = door.deliver(payload)

    assert d.tmux.sent == ([("paste", payload), ("keys", ("Enter",))] if matches else [("paste", payload)])
    assert delivery.result == ("delivered" if matches else "blind")
    if matches:
        assert d.seat.harness.inputs(d.record)[-1].text == payload


@pytest.mark.parametrize("change", [None, "visible word", "final LF", "absent thumb", "pane height", "receipt"])
def test_keyed_scroll_checks_bottom_view_before_enter(keyed_draft, monkeypatch, change):
    d, door, payload, frame = keyed_draft("scrolled-wrap")
    if change == "visible word":
        payload = payload.replace("w209", "X209")
    elif change == "final LF":
        payload = payload.removesuffix("\n")
    elif change == "absent thumb":
        frame = d.tmux.override = frame.replace("▆", " ").replace("█", " ")
    elif change == "pane height":
        original = d.tmux.panes["%1"].height
        d.tmux.panes["%1"].height = original + 1
    elif change == "receipt":
        keys, confirm = d.tmux.keys, door.confirm

        def record_different_input(ident, *pressed):
            d.tmux.box = d.tmux.box.replace("w209", "X209")
            keys(ident, *pressed)

        monkeypatch.setattr(d.tmux, "keys", record_different_input)
        monkeypatch.setattr(door, "confirm", lambda text: confirm(text, seconds=0))
    shown = d.seat.harness.screen(frame)
    box = d.seat.harness.box(payload)
    matches = change in (None, "receipt")

    assert door.matches(box, shown) is matches
    assert door.held_matches(box, shown) is matches

    delivery = door.deliver(payload)

    assert delivery.result == ("delivered" if change is None else "blind")
    assert d.tmux.sent == ([("paste", payload), ("keys", ("Enter",))] if matches else [("paste", payload)])
    if change is None:
        assert d.seat.harness.inputs(d.record)[-1].text == payload


def test_keyed_fold_rechecks_the_preview_before_enter(keyed_draft, monkeypatch):
    d, door, payload, frame = keyed_draft("submitted-fold")
    activity_screen = door.activity_screen

    def redraw(*args, **kwargs):
        d.tmux.override = frame.replace("Synthetic body line six.", "Different body line six.")
        return activity_screen(*args, **kwargs)

    monkeypatch.setattr(door, "activity_screen", redraw)
    delivery = door.deliver(payload)

    assert delivery.result == "blind" and "composer changed before submit" in delivery.detail
    assert d.tmux.sent == [("paste", payload)]


@pytest.mark.parametrize("wide_transcript", [False, True])
@pytest.mark.parametrize("receipt", ["exact", "changed"])
def test_fold_with_transcript_submits_once_and_requires_a_record(keyed_draft, monkeypatch, wide_transcript, receipt):
    d, door, payload, shown = keyed_draft("fold-with-transcript")
    if wide_transcript:
        # Six cells still fit to the left of the panel even though the prefix has one fewer character.
        shown = d.tmux.override = shown.replace("sample", "界mple")
    if receipt == "changed":
        keys, confirm = d.tmux.keys, door.confirm

        def record_different_input(ident, *pressed):
            d.tmux.box = d.tmux.box.replace("middle line two", "different hidden line")
            keys(ident, *pressed)

        monkeypatch.setattr(d.tmux, "keys", record_different_input)
        monkeypatch.setattr(door, "confirm", lambda text: confirm(text, seconds=0))

    screen = d.seat.harness.screen(shown)
    evidence = screen.draft_evidence
    assert evidence.rows[2] == evidence.rows[4] == ""
    assert evidence.rows[1].endswith("…") and evidence.rows[5].endswith("…")
    assert door.matches(d.seat.harness.box(payload), screen)
    assert door.held_matches(d.seat.harness.box(payload), screen)
    before = len(d.seat.harness.inputs(d.record))

    delivery = door.deliver(payload)

    assert d.tmux.sent == [("paste", payload), ("keys", ("Enter",))]
    recorded = d.seat.harness.inputs(d.record)[before:]
    assert len(recorded) == 1 and (recorded[0].text == payload) is (receipt == "exact")
    assert delivery.result == ("delivered" if receipt == "exact" else "blind")


@pytest.mark.parametrize("old,new,structural", [
    ("sample", "sampl界", True),
    ("sample", "\x1b[38;2;201;201;201msample", True),
    ("│⋮ (3 more lines)", "│⋮ (2 more lines)", True),
    ("│⋮ (3 more lines)", "X⋮ (3 more lines)", True),
    ("\x1b[38;2;200;200;200mFixture dispatch", "\x1b[38;2;201;201;201mFixture dispatch", True),
    ("           \x1b[38;2;25;25;25m", "       \x1b[38;2;25;25;25m", True),
    ("\x1b[38;2;25;25;25m", "\x1b[38;2;26;26;26m", True),
    ("\x1b[39m" + " " * 55 + "\x1b[38;2;65;65;65m│",
     "\x1b[48;2;20;20;20m" + " " * 55 + "\x1b[48;2;17;17;17m\x1b[38;2;65;65;65m│", True),
    ("Pasted: 9 lines", "Pasted: 8 lines", True),
    ("❯ \x1b[38;2;65;65;65m", "❮ \x1b[38;2;65;65;65m", True),
    ("Fixture dispatch", "Changed dispatch", False),
    ("extend beyond…", "extend within…", False),
])
def test_fold_with_transcript_holds_changed_panel_evidence_or_composer(keyed_draft, old, new, structural):
    d, door, payload, shown = keyed_draft("fold-with-transcript")
    assert old in shown
    d.tmux.override = shown.replace(old, new, 1)
    screen = d.seat.harness.screen(d.tmux.override)
    if structural:
        assert screen.box is None and screen.unknown
    else:
        assert isinstance(screen, grok_screen.KeyedDraft)
    box = d.seat.harness.box(payload)
    assert not door.matches(box, screen)
    assert not door.held_matches(box, screen)

    assert door.deliver(payload).result == "blind"
    assert d.tmux.sent == [("paste", payload)]


def test_fold_with_transcript_requires_the_visible_blank_payload_rows(keyed_draft):
    d, door, payload, _ = keyed_draft("fold-with-transcript")
    changed = payload.replace("\n\n", "\nUnexpected line.\n", 1)

    assert door.deliver(changed).result == "blind"
    assert d.tmux.sent == [("paste", changed)]


def test_the_empty_composer_at_112_columns_reads_empty(ordinary):
    d = ordinary(IDLE_112, (6, 30), width=112, height=35)
    screen = Door(d.seat).screen()
    assert screen.box == "" and screen.working is False
    assert human_wake.screen_hold(screen) is None


def test_the_empty_composer_while_a_turn_runs_reads_empty_and_working(ordinary):
    d = ordinary(frame(EMPTY_112_WORKING), (6, 30), width=112, height=35)
    screen = Door(d.seat).screen()
    assert screen.box == "" and screen.working is True
    assert human_wake.screen_hold(screen) is not None


@pytest.mark.parametrize("name, cursor, width, height", [(SPACES_80, (8, 19), 80, 24), (SPACES_112, (8, 30), 112, 35)])
def test_typed_spaces_stay_text_in_the_box(ordinary, name, cursor, width, height):
    d = ordinary(frame(name), cursor, width=width, height=height)
    screen = Door(d.seat).screen()
    assert screen.box == grok_screen.read(frame(name)).box and screen.box.strip(" ") == "" and screen.box != ""
    assert human_wake.screen_hold(screen) == human_wake.BOX


def test_typed_spaces_on_the_ordinary_idle_composer_stay_text(ordinary):
    d = ordinary(frame(EMPTY_80), (8, 19), width=80, height=24)
    assert human_wake.screen_hold(Door(d.seat).screen()) == human_wake.BOX


@pytest.mark.parametrize("cursor, visible", [(None, True), ((6, 19), False), ((7, 19), True), ((6, 18), True),
                                             ((6, 20), True), ((0, 0), True)])
def test_a_cursor_unread_hidden_or_elsewhere_leaves_the_padding_as_text(ordinary, cursor, visible):
    d = ordinary(frame(EMPTY_80), cursor, width=80, height=24, visible=visible)
    screen = Door(d.seat).screen()
    assert screen.box != "" and human_wake.screen_hold(screen) == human_wake.BOX


def test_no_control_frame_at_all_leaves_the_padding_as_text(ordinary):
    d = ordinary(frame(EMPTY_80), (6, 19), width=80, height=24)
    d.tmux.read_control_frame = lambda ident: None
    assert Door(d.seat).screen().box != ""


def test_a_tmux_with_no_bracketed_reading_leaves_the_padding_as_text(ordinary):
    d = ordinary(frame(EMPTY_80), (6, 19), width=80, height=24)
    plain = GrokTmux(d.tmux.panes["%1"])  # the stand-in with no read_control_frame at all
    plain.override = frame(EMPTY_80)
    d.seat.tmux = plain
    assert Door(d.seat).screen().box != ""


def test_a_second_frame_that_reads_otherwise_leaves_the_padding_as_text(ordinary):
    d = ordinary(frame(EMPTY_80), (6, 19), width=80, height=24)
    d.tmux.control = composer("typed")
    assert Door(d.seat).screen().box != ""
    d.tmux.control = frame(SPACES_80)  # same box, but a turn running: not the frame first read
    assert Door(d.seat).screen().box != ""


def test_a_control_frame_of_another_process_leaves_the_padding_as_text(ordinary, monkeypatch):
    d = ordinary(frame(EMPTY_80), (6, 19), width=80, height=24)
    other = type(d.tmux.panes["%1"])(**{**vars(d.tmux.panes["%1"]), "pid": 999})
    door = Door(d.seat)
    first = door.screen()
    assert first.box == ""
    monkeypatch.setattr(d.seat, "read_control_frame", lambda: ControlFrame(other, frame(EMPTY_80), True, (6, 19)))
    assert door.screen().box != ""


def test_a_draft_takes_no_cursor_reading(ordinary):
    d = ordinary(frame(EMPTY_80), (6, 19), width=80, height=24)
    d.tmux.box = "draft"
    d.seat.pane().width, d.seat.pane().height = 112, 35
    assert Door(d.seat).screen().box.rstrip(" ") == "draft" and d.tmux.control_reads == 0


def test_spaces_moved_home_are_the_measured_limit(ordinary):
    """Ctrl-A or Home after typed spaces puts the cursor back at the first cell: the screen and the cursor are
    then the empty composer's, measured so. This is the ruling's limit, named in the report, not a pass."""
    assert measured(SPACES_HOME_80)["cursor"] == [6, 19]
    d = ordinary(frame(SPACES_HOME_80), (6, 19), width=80, height=24)
    assert Door(d.seat).screen().box == ""


# -- the human waking line ------------------------------------------------------------------------------


def test_a_human_session_on_the_ordinary_empty_composer_takes_the_waking_line(ordinary):
    d = ordinary(IDLE_112, (6, 30), width=112, height=35, seat_name="desk")
    assert d.seat.session.human
    post.Store(d.ship.runtime).new("order", "captain", "desk", "Synthetic waiting order", subject="synthetic")
    human_wake.run(d.seat)
    assert pastes(d) == [EXACT] and [s for s in d.tmux.sent if s[0] == "keys"] == [("keys", ("Enter",))]
    proof = human_wake.load(d.seat)
    assert proof.data["phase"] == "delivered" and proof.data["record"] == str(d.record)


def test_a_human_session_with_typed_spaces_is_left_untouched(ordinary):
    d = ordinary(IDLE_112, (8, 30), width=112, height=35, seat_name="desk")
    post.Store(d.ship.runtime).new("order", "captain", "desk", "Synthetic waiting order", subject="synthetic")
    assert human_wake.run(d.seat) == human_wake.BOX
    assert d.tmux.sent == []


def test_a_human_session_with_no_cursor_reading_is_left_untouched(ordinary):
    d = ordinary(IDLE_112, None, width=112, height=35, seat_name="desk")
    post.Store(d.ship.runtime).new("order", "captain", "desk", "Synthetic waiting order", subject="synthetic")
    assert human_wake.run(d.seat) == human_wake.BOX
    assert d.tmux.sent == []
