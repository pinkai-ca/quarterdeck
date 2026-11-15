"""Antigravity 1.5.5's survey about itself: the ship gives no rating and presses the program's own '[0] Skip'.

As Grok's training question is answered: only in a seat no human owns, only on a settled frame, by the choice's
own text, recorded in the launch before the press and shown by watch; a survey not as measured is held and named.
The frames are the measured 1.5.5 layout made synthetic; tmux is a stand-in that records the key and shows the
next frame. No program, server, credential or provider is touched.
"""

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import check, ship as shipmod
from quarterdeck.door import Door
from quarterdeck.harness import antigravity
from quarterdeck.harness.antigravity import AntigravityHarness
from quarterdeck.harness.types import Interruption
from quarterdeck.seat import Launched, Seat
from quarterdeck.service import Service

FIXTURES = Path(__file__).parents[1] / "fixtures/antigravity/1.5.5"
SURVEY = (FIXTURES / "feedback-survey.txt").read_text()
# Measured: one press of 0 drew the empty box under the same transcript, the footer kept.
RULE = "─" * 80
SKIPPED = SURVEY.replace(" How's the CLI experience so far? Help us improve:              \n"
                         " [1] Good  [2] Fine  [3] Bad  [0] Skip                                          \n",
                         f"{RULE}\n>\n{RULE}\n")


@pytest.fixture
def agy(drydock, monkeypatch):
    ship = shipmod.load(drydock)
    spec = replace(ship.seat("worker-1"), harness="antigravity")
    shown, presses, launches = [SURVEY], [], []
    pane = SimpleNamespace(id="%1", pid=123, dead=False, width=80, in_mode=False)

    def keys(pane_id, *pressed):
        launches.append(json.loads((seat.trusted / "launch.json").read_text()))  # what was recorded before the press
        presses.append((pane_id, *pressed))
        if pressed == ("0",):
            shown[0] = after[0]

    def no_click(*a):
        raise AssertionError("the program takes no mouse: nothing is clicked")

    def no_modes(*a):
        raise AssertionError("a press by the choice's own label needs no mouse reports")

    after = [SKIPPED]
    tmux = SimpleNamespace(pane=lambda _: pane, capture=lambda *a, **k: shown[0], keys=keys,
                           paste=lambda *a, **k: presses.append(("paste", a)), click=no_click, mouse_modes=no_modes)
    seat = Seat(ship, spec, tmux)
    seat.folder.mkdir(parents=True, exist_ok=True)
    seat.trusted.mkdir(parents=True, exist_ok=True)
    Launched("%1", 123, "2030-01-01T00:00:00+00:00", "synthetic", None, ["agy"], "1.5.5", "antigravity").write(
        seat.trusted / "launch.json")
    monkeypatch.setattr(seat, "pane", lambda: pane)
    monkeypatch.setattr("quarterdeck.door.ANSWER_SECONDS", 0.5)
    monkeypatch.setattr("quarterdeck.door.SETTLE_SECONDS", 0)
    return SimpleNamespace(seat=seat, shown=shown, after=after, presses=presses, launches=launches)


def test_the_measured_survey_is_read_in_place_of_the_box_with_its_skip_by_text():
    screen = AntigravityHarness().screen(SURVEY)
    assert screen.box is None and screen.unknown is None
    assert screen.interruption is antigravity.SURVEY
    assert screen.interruption.click == "[0] Skip" and screen.interruption.label_key == "0"
    assert screen.interruption.key is None, "no key is configured apart from the choice's own text"
    assert screen.shows == {"model": "Gemini 3.8 Flash", "effort": "low"}, "the footer stays under the survey"
    after = AntigravityHarness().screen(SKIPPED)
    assert after.box == "" and after.interruption is None
    assert "@" not in SURVEY and "/home" not in SURVEY, "no account text in the fixture"


def test_the_label_key_is_read_only_from_a_bracketed_choice():
    assert Interruption("q", "x", click="[0] Skip", by_label=True).label_key == "0"
    assert Interruption("q", "x", click="[0] Skip").label_key is None, "a clicked choice names no key"
    for odd in ("Skip", "[10] Skip", "[0]Skip", "[0] ", "(0) Skip"):
        assert Interruption("q", "x", click=odd, by_label=True).label_key is None


def test_the_survey_words_in_the_transcript_above_a_drawn_box_are_no_interruption():
    screen = AntigravityHarness().screen(SURVEY.replace("? for shortcuts", f"{RULE}\n>\n{RULE}\n? for shortcuts"))
    assert screen.box == "" and screen.interruption is None


def test_skip_is_pressed_once_recorded_first_and_named(agy):
    door = Door(agy.seat)
    reading = door.reading()
    assert not reading.open and reading.clears == "dismissable" and "[0] Skip" in reading.reason
    after = door.clear_known()
    assert agy.presses == [("%1", "0")]
    pressed = agy.launches[0]["answered"]
    assert [(e["choice"], e["outcome"]) for e in pressed] == [("[0] Skip", "pressed")], "recorded before the press"
    done = agy.seat.launched().answered
    assert len(done) == 1 and done[0]["question"] == antigravity.SURVEY.name and done[0]["outcome"] == "cleared"
    assert after.box == "" and door.reading().open
    door.clear_known()
    assert agy.presses == [("%1", "0")], "nothing is pressed on a drawn box"


def test_a_survey_shown_again_after_a_cleared_skip_is_skipped_again(agy):
    door = Door(agy.seat)
    door.clear_known()
    agy.shown[0] = SURVEY  # the program asks again after a later turn
    door.clear_known()
    assert agy.presses == [("%1", "0"), ("%1", "0")]
    assert [e["outcome"] for e in agy.seat.launched().answered] == ["cleared"]


def test_a_skip_that_does_not_clear_is_held_with_nothing_more_pressed(agy):
    agy.after[0] = SURVEY
    door = Door(agy.seat)
    door.clear_known()
    assert agy.seat.launched().answered[0]["outcome"] == "did not clear"
    door.clear_known()
    assert agy.presses == [("%1", "0")]
    reading = door.reading()
    assert reading.clears == "needs a human" and "nothing more is pressed" in reading.reason


def test_nothing_is_pressed_in_a_human_session(agy, monkeypatch):
    monkeypatch.setattr(Seat, "session", property(lambda self: SimpleNamespace(human=True, title="office")))
    Door(agy.seat).clear_known()
    Service.answers(SimpleNamespace(resolved_seats=lambda: [agy.seat], log=lambda *a, **k: None))
    assert agy.presses == [] and agy.seat.launched().answered is None


@pytest.mark.parametrize("variant", [
    lambda t: t.replace("[2] Fine", "[2] Okay"),
    lambda t: t.replace("  [0] Skip", "  [4] Skip"),
    lambda t: t.replace("[1] Good  [2] Fine  [3] Bad  [0] Skip", "[0] Skip  [1] Good  [2] Fine  [3] Bad"),
    lambda t: t.replace("Help us improve:", "Tell us more:"),
])
def test_a_survey_not_as_measured_is_held_named_and_never_pressed(agy, variant):
    agy.shown[0] = variant(SURVEY)
    door = Door(agy.seat)
    door.clear_known()
    Service.answers(SimpleNamespace(resolved_seats=lambda: [agy.seat], log=lambda *a, **k: None))
    assert agy.presses == [] and agy.seat.launched().answered is None
    reading = door.reading()
    assert reading.clears == "needs a human" and "not as measured" in reading.reason


def test_a_frame_that_changes_before_the_press_is_not_pressed(agy):
    door = Door(agy.seat)
    screen = door.screen()
    agy.shown[0] = SURVEY.replace("Standing orders received.", "Standing orders read.")
    door.clear_known(screen)
    assert agy.presses == [] and agy.seat.launched().answered is None


def test_the_service_skips_it_with_no_message_waiting_and_watch_shows_it(agy):
    logs = []
    Service.answers(SimpleNamespace(resolved_seats=lambda: [agy.seat], log=lambda event, **data: logs.append((event, data))))
    assert agy.presses == [("%1", "0")]
    assert logs and logs[0][0] == "answered" and logs[0][1]["choice"] == "[0] Skip" and logs[0][1]["outcome"] == "cleared"
    from quarterdeck import watch
    lines = [line for line in watch.render_full(dict(watch.seat_reading(agy.seat)))
             if "answered for the account holder" in line]
    assert len(lines) == 1 and "[0] Skip pressed at" in lines[0] and "cleared" in lines[0]


def test_the_check_start_wait_skips_it_through_the_door(agy):
    seat = agy.seat
    assert check.ready_for_probe(seat, Door(seat)) is False
    assert agy.presses == [("%1", "0")]
