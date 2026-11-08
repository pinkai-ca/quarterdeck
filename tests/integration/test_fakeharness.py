"""The fake harness in a real pane: box, paste, submit, record, dialog, queueing, controls."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from quarterdeck.harness.fake import FakeHarness
from quarterdeck.ship import Pool, SeatSpec
from quarterdeck.tmux import Tmux

POOL = Pool("fake", "fake")


def wait_for(predicate, seconds: float = 8.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def showing(seat: "Seat", test):
    """The screen when ``test`` holds for it, else None, for wait_for."""
    screen = seat.screen()
    return screen if test(screen) else None


class Seat:
    """One fake seat in a private tmux server, read through its adapter."""

    def __init__(self, tmux: Tmux, folder: Path, name: str = "worker", **settings):
        self.tmux, self.harness = tmux, FakeHarness()
        self.spec = SeatSpec(name, "crew", "worker", "fake", ("fake",), model="fake-1", effort="high", settings=settings)
        self.home = folder / "home"
        brief = folder / "brief.md"
        brief.write_text("# brief\nYou are a fake seat.\n")
        self.harness.build_home(self.home, self.spec, POOL, None)
        launch = self.harness.launch(self.spec, POOL, self.home, brief, None, None)
        self.launched_at = datetime.now(timezone.utc)
        if not tmux.has_session("Crew"):
            tmux.new_session("Crew")
        self.pane = tmux.new_window("Crew", name, folder, list(launch.argv), launch.env)
        wait_for(lambda: self.screen().box is not None)

    def screen(self):
        return self.harness.screen(self.tmux.capture(self.pane.id))

    def send(self, text: str, submit: bool = True):
        self.tmux.paste(self.pane.id, text)
        if submit:
            self.tmux.keys(self.pane.id, self.harness.keys.submit)

    def record(self) -> Path:
        return wait_for(lambda: self.harness.join(self.home, self.launched_at).record)

    def wait_idle(self):
        wait_for(lambda: self.harness.state(self.record()).turns and not self.screen().working)
        return self.harness.state(self.record())


def test_a_message_is_taken_worked_and_recorded(tmux: Tmux, tmp_path: Path):
    seat = Seat(tmux, tmp_path, step_seconds="0.05")
    assert "briefed: " in tmux.capture(seat.pane.id)
    assert seat.screen().shows == {"model": "fake-1", "effort": "high"}
    seat.send("hello")
    state = seat.wait_idle()
    assert state.turns == 1 and state.last_input == "hello"
    turn = seat.harness.turns(seat.record())[0]
    assert turn.reply == "done: hello" and len(turn.tools) == 2 and turn.tokens.total > 0
    screen = tmux.capture(seat.pane.id)
    assert "> hello" in screen and "⏺ done: hello" in screen and seat.screen().box == ""


def test_the_box_folds_a_long_paste_as_the_adapter_predicts(tmux: Tmux, tmp_path: Path):
    seat = Seat(tmux, tmp_path, step_seconds="0.05")
    text = "qd > order from desk · ord-7f3a · a subject\nline two\nline three\n$ echo marker-7f3a"
    seat.send(text, submit=False)
    shown = wait_for(lambda: seat.screen().box or None)
    assert seat.harness.box(text).matches(shown), shown
    assert not seat.harness.box("something else\nentirely").matches("hello")
    tmux.keys(seat.pane.id, "Enter")
    seat.wait_idle()
    turn = seat.harness.turns(seat.record())[0]
    assert turn.input == text.strip()
    assert [t.name for t in turn.tools] == ["Bash"] and not turn.tools[0].failed
    assert "⎿ marker-7f3a" in tmux.capture(seat.pane.id, history=50)


def test_a_dialog_swallows_input_until_its_key(tmux: Tmux, tmp_path: Path):
    seat = Seat(tmux, tmp_path, dialog="daybreak", step_seconds="0.05")
    screen = seat.screen()
    assert screen.interruption and screen.interruption.name == "daybreak notice" and screen.unknown is None
    seat.send("hello")
    time.sleep(0.3)
    assert seat.screen().box == "" and seat.harness.join(seat.home, seat.launched_at).record is None
    tmux.keys(seat.pane.id, screen.interruption.key)
    wait_for(lambda: seat.screen().interruption is None)
    seat.send("hello")
    assert seat.wait_idle().turns == 1


def test_an_unknown_dialog_is_reported_not_dismissed(tmux: Tmux, tmp_path: Path):
    seat = Seat(tmux, tmp_path, dialog="unknown")
    screen = seat.screen()
    assert screen.interruption is None and screen.unknown == "Something else"


def test_a_program_that_queues_takes_a_message_mid_turn(tmux: Tmux, tmp_path: Path):
    seat = Seat(tmux, tmp_path, steps="3", step_seconds="0.6")
    seat.send("first")
    wait_for(lambda: seat.screen().working)
    seat.send("second")
    screen = wait_for(lambda: showing(seat, lambda s: s.queued))
    assert screen.working and screen.queued == 1
    assert seat.harness.state(seat.record()).queued == ("second",)
    wait_for(lambda: seat.harness.state(seat.record()).turns == 2, seconds=15)
    assert [t.input for t in seat.harness.turns(seat.record())] == ["first", "second"]


def test_a_program_that_does_not_queue_refuses_mid_turn(tmux: Tmux, tmp_path: Path):
    seat = Seat(tmux, tmp_path, queues=False, steps="3", step_seconds="0.5")
    assert not seat.harness.queues_while_working(seat.spec)
    seat.send("first")
    wait_for(lambda: seat.screen().working)
    seat.send("second")
    time.sleep(0.3)
    assert seat.screen().box == "" and seat.screen().queued == 0
    wait_for(lambda: not seat.screen().working, seconds=15)
    assert [t.input for t in seat.harness.turns(seat.record())] == ["first"]


def test_a_slash_command_changes_the_program_and_the_screen_confirms_it(tmux: Tmux, tmp_path: Path):
    seat = Seat(tmux, tmp_path)
    control = seat.harness.control("model")
    seat.send(control.command("fake-2"))
    screen = wait_for(lambda: showing(seat, lambda s: s.shows.get("model") == "fake-2"))
    state = seat.harness.state(seat.record()) if seat.harness.join(seat.home, seat.launched_at).record else None
    assert seat.harness.confirms(control, "fake-2", screen, state) is True
    assert "· model set to fake-2" in tmux.capture(seat.pane.id)


def test_interrupt_key_stops_the_open_turn(tmux: Tmux, tmp_path: Path):
    seat = Seat(tmux, tmp_path, steps="5", step_seconds="1")
    seat.send("slow one")
    wait_for(lambda: seat.screen().working)
    tmux.keys(seat.pane.id, seat.harness.keys.interrupt)
    wait_for(lambda: not seat.screen().working)
    assert "· interrupted" in tmux.capture(seat.pane.id)
    assert not seat.harness.state(seat.record()).turn_open


@pytest.mark.parametrize("shape", ["claude", "codex"])
def test_both_record_shapes_are_written_and_joined(tmux: Tmux, tmp_path: Path, shape: str):
    seat = Seat(tmux, tmp_path, **{"as": shape, "step_seconds": "0.05"})
    seat.send("hello")
    seat.wait_idle()
    record = seat.record()
    assert ("sessions" in record.parts) == (shape == "codex")
    assert seat.harness.session_id(record)
