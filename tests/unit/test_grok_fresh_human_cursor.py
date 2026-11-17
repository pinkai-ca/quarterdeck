"""In a person's own Grok session, the fresh composer reads idle only with the cursor at the prompt's first cell.

Grok 1.0.77 draws typed spaces exactly as its padding, and its fresh one-row composer, before any turn, is read by the
Door as the measured empty idle composer in a seeded or cleared launch. A person's typed spaces there would read
empty, and the waking line would be typed after them. The same rule as the ordinary composer: in a human-owned
session the fresh form reads idle only where the visible cursor, read with the frame, sits on the prompt's first cell
(column 6, as measured at 80 and 112 columns; typed spaces move it to 8). A crew seat is unchanged. The frame is the
measured seeded frame (training-seeded-256); the cursor positions are the measured ones, set on its prompt row.
"""
from __future__ import annotations

import pytest

from quarterdeck import human_wake, post
from quarterdeck.door import Door
from quarterdeck.harness import grok_screen
from tests.unit.test_grok_empty_box import CursorTmux
from tests.unit.test_grok_seeded_composer import EXACT, composer, pastes, seeded  # noqa: F401

PROMPT_ROW = 30  # the seeded frame's prompt row (training-seeded-256)


@pytest.fixture
def fresh(seeded):  # noqa: F811
    """A seeded Grok seat on its fresh composer, with a cursor reading as a test sets it."""
    def make(seat_name, cursor, *, visible=True, readable=True):
        d = seeded(seat_name)
        tmux = CursorTmux(d.tmux.panes["%1"], composer(""), cursor, visible)
        tmux.record = d.record
        if not readable:
            tmux.read_control_frame = lambda ident: None
        d.seat.tmux = d.tmux = tmux
        return d
    return make


def wake(d):
    post.Store(d.ship.runtime).new("order", "captain", d.seat.name, "Synthetic waiting order", subject="synthetic")
    return human_wake.run(d.seat)


def test_the_reader_names_the_fresh_composers_first_cell():
    screen = grok_screen.read(composer(""))
    assert screen.fresh is not None and screen.box is None
    assert grok_screen.empty_cell(screen) == (6, PROMPT_ROW)


def test_a_persons_fresh_composer_with_the_cursor_at_the_first_cell_reads_idle_and_takes_the_waking_line(fresh):
    d = fresh("desk", (6, PROMPT_ROW))
    assert d.seat.session.human
    screen = Door(d.seat).screen()
    assert screen.box == "" and screen.unknown is None and screen.working is False
    wake(d)
    assert pastes(d) == [EXACT]


@pytest.mark.parametrize("cursor, visible, readable", [
    ((8, PROMPT_ROW), True, True),   # two typed spaces moved it, as measured
    ((6, PROMPT_ROW), False, True),  # hidden
    ((6, PROMPT_ROW), True, False),  # unread: no frame at all
])
def test_a_persons_typed_spaces_or_an_unread_cursor_keep_the_fresh_composer_unknown(fresh, cursor, visible, readable):
    d = fresh("desk", cursor, visible=visible, readable=readable)
    screen = Door(d.seat).screen()
    assert screen.box is None and screen.unknown == grok_screen.FRESH
    assert wake(d) == "human wake box unknown or obstructed; untouched"
    assert pastes(d) == [] and d.tmux.sent == []


@pytest.mark.parametrize("cursor, readable", [((8, PROMPT_ROW), True), ((6, PROMPT_ROW), False)])
def test_a_crew_seats_fresh_composer_reads_idle_as_before_whatever_the_cursor(fresh, cursor, readable):
    d = fresh("worker-1", cursor, readable=readable)
    assert not d.seat.session.human
    screen = Door(d.seat).screen()
    assert screen.box == "" and screen.unknown is None
