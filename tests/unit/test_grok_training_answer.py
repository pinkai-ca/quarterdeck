"""The door answers Grok's training question opt out, by its text, once in a launch, and says that it did.

Frames are M3's and M4's measured question and after-press frames; tmux is a stand-in that records the click and
shows the next frame. No program, server, credential or provider is touched.
"""

from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from quarterdeck import ship as shipmod, watch
from quarterdeck.door import Door, choice_cell
from quarterdeck.harness import grok_screen
from quarterdeck.seat import Launched, Seat
from quarterdeck.service import Service
from tests.conftest import FIXTURES


def frame(name):
    return "\n".join(json.loads((FIXTURES / "grok/1.0.77" / f"{name}.json").read_text())["lines"]) + "\n"


QUESTION, ANSWERED = frame("training-question-256"), frame("training-answered-256")
AFTER_TURN, ANSWERED_AFTER_TURN = frame("training-question-after-turn-256"), frame("training-answered-after-turn-256")
WORKING = frame("training-question-working-256")  # M4's w01: the question over a running turn, working hint drawn
SGR_ON = dict(any=True, button=False, all=True, standard=False, sgr=True)  # as M1 to M3 read the pane


@pytest.fixture
def grok(drydock, monkeypatch):
    ship = shipmod.load(drydock)
    spec = replace(ship.seat("worker-1"), harness="grok")
    shown, clicks, modes, launches = [QUESTION], [], [dict(SGR_ON)], []
    pane = SimpleNamespace(id="%1", pid=123, dead=False, width=112, in_mode=False)

    def click(pane_id, x, y):
        launches.append(json.loads((seat.trusted / "launch.json").read_text()))  # what was recorded before the press
        clicks.append((pane_id, x, y))
        shown[0] = after[0]

    after = [ANSWERED]
    tmux = SimpleNamespace(pane=lambda _: pane, capture=lambda *a, **k: shown[0], keys=lambda *a: clicks.append(("keys", a)),
                           paste=lambda *a, **k: clicks.append(("paste", a)), click=click,
                           mouse_modes=lambda pane_id, pid: modes[0] if pid == pane.pid else None)
    seat = Seat(ship, spec, tmux)
    seat.folder.mkdir(parents=True, exist_ok=True)
    seat.trusted.mkdir(parents=True, exist_ok=True)
    Launched("%1", 123, "2030-01-01T00:00:00+00:00", "synthetic", None, ["grok"], "1.0.77", "grok").write(
        seat.trusted / "launch.json")
    monkeypatch.setattr(seat, "pane", lambda: pane)
    monkeypatch.setattr("quarterdeck.door.ANSWER_SECONDS", 0.5)
    return SimpleNamespace(seat=seat, shown=shown, after=after, clicks=clicks, modes=modes, launches=launches)


def test_the_cell_is_found_by_the_choice_text_alone():
    plain = grok_screen.characters(QUESTION)[0]
    assert choice_cell(plain, "[Opt out]") == (97, 25)  # M2 and M3 clicked here
    assert choice_cell(plain.replace("[Opt out]", "[Opt out] [Opt out]", 1), "[Opt out]") is None
    assert choice_cell(plain.replace("[Opt out]", "[Opt-out]"), "[Opt out]") is None


def test_the_question_reads_as_answered_by_the_framework(grok):
    reading = Door(grok.seat).reading()
    assert not reading.open and reading.clears == "dismissable"
    assert "first-run training-data question" in reading.reason and "[Opt out]" in reading.reason


def test_opt_out_is_pressed_once_recorded_first_and_named(grok):
    door = Door(grok.seat)
    door.clear_known()
    assert grok.clicks == [("%1", 97, 25)]
    pressed = grok.launches[0]["answered"]
    assert [(e["choice"], e["outcome"]) for e in pressed] == [("[Opt out]", "pressed")], "recorded before the press"
    done = grok.seat.launched().answered
    assert len(done) == 1 and done[0]["choice"] == "[Opt out]" and done[0]["outcome"] == "cleared"
    assert done[0]["question"] == grok_screen.TRAINING_QUESTION.name and done[0]["at"]
    grok.shown[0] = QUESTION  # the question again in the same launch: never pressed twice
    door.clear_known()
    assert grok.clicks == [("%1", 97, 25)]
    reading = door.reading()
    assert reading.clears == "needs a human" and "nothing more is pressed" in reading.reason


def test_a_screen_that_does_not_clear_is_held_with_nothing_more_pressed(grok):
    grok.after[0] = QUESTION
    door = Door(grok.seat)
    door.clear_known()
    door.clear_known()
    assert grok.clicks == [("%1", 97, 25)]
    assert grok.seat.launched().answered[0]["outcome"] == "did not clear"
    assert door.reading().clears == "needs a human"


@pytest.mark.parametrize(("modes", "reason"), [
    (None, "SGR mouse reporting could not be read"),
    (dict(SGR_ON, sgr=False), "SGR mouse reporting is off"),
    (dict(SGR_ON, button=False, all=False, standard=False), "SGR mouse reporting is off"),
], ids=["unreadable", "sgr-off", "tracking-off"])
def test_missing_mouse_reporting_names_the_held_question_with_no_message(grok, monkeypatch, modes, reason):
    # law: nothing-waits-silently
    seat = grok.seat
    grok.modes[0] = modes
    monkeypatch.setattr(watch, "seats", lambda *args: [seat])
    monkeypatch.setattr(watch, "disks", lambda *args: [])
    logs = []
    service = SimpleNamespace(resolved_seats=lambda: [seat], log=lambda *args, **kwargs: logs.append(args))

    Service.answers(service)
    reading = Door(seat).reading()
    assert reading.clears == "needs a human" and reason in reading.reason
    facts = watch.facts(seat.ship, seat.tmux, login=[])
    assert facts.waiting == 0 and facts.needs_human == [seat.name]
    assert f"{seat.name}: {grok_screen.TRAINING_QUESTION.name}; {reason}" in facts.line()
    assert not grok.clicks and not logs and seat.launched().answered is None

    grok.modes[0] = SGR_ON
    facts = watch.facts(seat.ship, seat.tmux, login=[])
    assert not facts.needs_human and grok_screen.TRAINING_QUESTION.name not in facts.line()
    assert not grok.clicks and seat.launched().answered is None


def test_nothing_is_pressed_in_a_human_session(grok, monkeypatch):
    monkeypatch.setattr(Seat, "session", property(lambda self: SimpleNamespace(human=True, title="office")))
    Door(grok.seat).clear_known()
    assert grok.clicks == [] and grok.seat.launched().answered is None


def test_an_altered_question_is_held_and_never_clicked(grok):
    plain = grok_screen.characters(QUESTION)[0]
    grok.shown[0] = plain.replace("[Opt out] [Opt in]", "[Opt in] [Opt out]")
    door = Door(grok.seat)
    door.clear_known()
    assert grok.clicks == []
    reading = door.reading()
    assert reading.clears == "needs a human" and "not as measured" in reading.reason


def test_a_frame_that_changes_before_the_press_is_not_pressed(grok, monkeypatch):
    door = Door(grok.seat)
    screen = door.screen()
    grok.shown[0] = grok_screen.characters(QUESTION)[0].replace("/sample/project", "/sample/other!!")
    door.clear_known(screen)
    assert grok.clicks == [] and grok.seat.launched().answered is None


def test_a_changed_launch_is_not_recorded_or_pressed(grok, monkeypatch):
    door = Door(grok.seat)
    real = Seat.launched
    calls = []

    def moving(self):
        calls.append(1)
        found = real(self)
        return replace(found, pid=999) if len(calls) > 1 else found
    monkeypatch.setattr(Seat, "launched", moving)
    door.answer(door.screen())
    assert grok.clicks == []


def test_the_service_answers_with_no_message_waiting(grok):
    logs = []
    fake = SimpleNamespace(resolved_seats=lambda: [grok.seat], log=lambda event, **data: logs.append((event, data)))
    Service.answers(fake)
    assert grok.clicks == [("%1", 97, 25)]
    assert logs and logs[0][0] == "answered" and logs[0][1]["choice"] == "[Opt out]" and logs[0][1]["outcome"] == "cleared"
    Service.answers(fake)
    assert grok.clicks == [("%1", 97, 25)]


def test_the_service_leaves_other_programs_alone(grok, monkeypatch):
    monkeypatch.setattr(type(grok.seat.harness), "answers_questions", False)
    Service.answers(SimpleNamespace(resolved_seats=lambda: [grok.seat], log=lambda *a, **k: None))
    assert grok.clicks == []


def test_after_a_turn_the_press_clears_to_an_open_door(grok):
    grok.shown[0], grok.after[0] = AFTER_TURN, ANSWERED_AFTER_TURN
    door = Door(grok.seat)
    door.clear_known()
    assert len([c for c in grok.clicks if c[0] == "%1"]) == 1
    assert grok.seat.launched().answered[0]["outcome"] == "cleared"


def test_no_press_while_a_turn_is_running_then_one_press_after_it(grok):
    grok.shown[0], grok.after[0] = WORKING, ANSWERED_AFTER_TURN
    door = Door(grok.seat)
    reading = door.reading()
    assert reading.clears == "time" and "once the turn under it ends" in reading.reason
    door.clear_known()
    door.answer(door.screen())  # the press itself refuses a working frame, not only its caller
    Service.answers(SimpleNamespace(resolved_seats=lambda: [grok.seat], log=lambda *a, **k: None))
    assert grok.clicks == [] and grok.seat.launched().answered is None
    grok.shown[0] = AFTER_TURN  # the turn ended: the idle hint under the question
    door.clear_known()
    assert grok.clicks == [("%1", 97, 25)] and grok.seat.launched().answered[0]["outcome"] == "cleared"


def test_no_press_under_a_hint_row_that_reads_neither(grok):
    plain = grok_screen.characters(AFTER_TURN)[0]
    grok.shown[0] = plain.replace("Shift+Tab:mode  │  Ctrl+x:shortcuts", "Shift+Tab:mode  │  Ctrl+y:something")
    door = Door(grok.seat)
    door.clear_known()
    door.answer(door.screen())
    assert grok.clicks == [] and grok.seat.launched().answered is None
    reading = door.reading()
    assert reading.clears == "needs a human" and "under the training question; nothing is pressed" in reading.reason


def test_the_launch_record_keeps_its_old_shape_until_a_question_is_answered(grok):
    data = json.loads((grok.seat.trusted / "launch.json").read_text())
    assert "answered" not in data


# The fresh composer after the press reads idle only in the launch that answered the question.

def relaunch(grok, **fields):
    launched = grok.seat.launched()
    replace(launched, **fields).write(grok.seat.trusted / "launch.json")


def test_after_the_press_the_fresh_composer_reads_idle_in_that_launch(grok):
    door = Door(grok.seat)
    door.clear_known()
    assert grok.seat.launched().answered[0]["outcome"] == "cleared"
    reading = Door(grok.seat).reading()
    assert reading.open and not reading.working


def test_the_fresh_composer_alone_is_not_idle(grok):
    grok.shown[0] = ANSWERED  # the same frame, but this launch answered nothing
    reading = Door(grok.seat).reading()
    assert not reading.open and reading.clears == "needs a human" and grok_screen.FRESH in reading.reason


@pytest.mark.parametrize("outcome", ["pressed", "did not clear"])
def test_an_answer_not_cleared_does_not_make_the_fresh_composer_idle(grok, outcome):
    entry = dict(question=grok_screen.TRAINING_QUESTION.name, choice="[Opt out]", at="2030-01-01T00:00:01+00:00",
                 outcome=outcome)
    relaunch(grok, answered=[entry])
    grok.shown[0] = ANSWERED
    assert not Door(grok.seat).reading().open


def test_another_question_answered_does_not_make_the_fresh_composer_idle(grok):
    entry = dict(question="some other question", choice="[Opt out]", at="2030-01-01T00:00:01+00:00", outcome="cleared")
    relaunch(grok, answered=[entry])
    grok.shown[0] = ANSWERED
    assert not Door(grok.seat).reading().open


def test_an_unmeasured_version_keeps_the_fresh_composer_unknown(grok):
    Door(grok.seat).clear_known()
    relaunch(grok, version="1.0.71")
    reading = Door(grok.seat).reading()
    assert not reading.open and grok_screen.FRESH in reading.reason


def test_something_under_the_footer_keeps_the_composer_unknown_after_the_press(grok):
    Door(grok.seat).clear_known()
    lines = grok_screen.characters(ANSWERED)[0].split("\n")
    at = max(i for i, row in enumerate(lines) if grok_screen.FOOTER.fullmatch(row))
    lines[at + 1] = "  unmeasured"
    grok.shown[0] = "\n".join(lines)
    reading = Door(grok.seat).reading()
    assert not reading.open and "unmeasured Grok composer hint" in reading.reason


def test_on_1_0_70_its_own_measured_fresh_composer_reads_idle_after_the_press(grok):
    """M5 (13 November, 1.0.70 by path, 112 x 35): the same question, and after the press the same fresh form."""
    fresh = "\n".join(json.loads((FIXTURES / "grok/1.0.70" / "training-answered-256.json").read_text())["lines"]) + "\n"
    relaunch(grok, version="1.0.70")
    grok.after[0] = fresh
    Door(grok.seat).clear_known()
    assert grok.seat.launched().answered[0]["outcome"] == "cleared"
    assert Door(grok.seat).reading().open
