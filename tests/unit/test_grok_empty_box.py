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


def test_no_cell_is_named_for_a_draft_a_question_a_fresh_composer_or_another_program():
    assert grok_screen.empty_cell(grok_screen.read(composer("draft"))) is None
    assert grok_screen.empty_cell(grok_screen.read(composer(""))) is None  # the fresh form: the Door's own rule
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
