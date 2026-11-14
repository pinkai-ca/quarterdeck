"""The launch opening stays untyped until the startup proof, and a contributor model never receives it.

Screens are the measured 1.4.20 fixtures or a synthetic empty box. No native
Muse runs, and the door stand-in records a delivery without typing.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck.door import Delivery
from quarterdeck.harness.muse import MuseHarness
from quarterdeck.harness import muse_screen
from quarterdeck.harness.types import Screen, State
from quarterdeck.seat import Launched
from quarterdeck.ship import ShipError
from quarterdeck import launch_opening

FIXTURES = Path(__file__).parents[1] / "fixtures" / "muse" / "1.4.20"
BRIEF = "Admiral text stays in the opening.\nIt is not typed before the proof."


def screen_of(name: str):
    return muse_screen.read((FIXTURES / name).read_text())


def prepared(tmp_path, text, screen):
    home = tmp_path / "home"
    home.mkdir()
    launched = Launched("%1", 4242, datetime.now(timezone.utc).isoformat(), "synthetic", None,
                        ["/synthetic/bin/muse-bin-1.4.20-R1.1"], "1.4.20", "muse")
    pane = SimpleNamespace(id=launched.pane, pid=launched.pid)
    seat = SimpleNamespace(folder=tmp_path / "seat", home=home, harness=MuseHarness(),
                           launched=lambda: launched, screen_now=screen, pane=lambda: pane)
    seat.folder.mkdir()
    launch_opening.prepare(seat, launched, text)
    return seat


def test_an_unconfirmed_opening_holds_ordinary_delivery(tmp_path):
    seat = prepared(tmp_path, BRIEF, screen_of("idle-off-120x40.txt"))
    assert launch_opening.hold(seat) == "launch opening unconfirmed; ordinary delivery held; never resent"
    journal = json.loads(next((seat.folder / "launch-openings").iterdir()).read_text())
    assert journal["payload"] == BRIEF and journal["phase"] == "prepared"
    with pytest.raises(ShipError, match="already recorded"):
        launch_opening.prepare(seat, seat.launched(), "A second opening.")


def test_a_contributor_on_screen_types_nothing(tmp_path, monkeypatch):
    typed = []

    class RecordingDoor:
        def __init__(self, seat):
            self.seat = seat

        def screen(self):
            return self.seat.screen_now

        def deliver(self, text, verify=None):
            typed.append(text)
            return Delivery("delivered", "synthetic")

    monkeypatch.setattr("quarterdeck.door.Door", RecordingDoor)
    seat = prepared(tmp_path, BRIEF, screen_of("contributor-on-120x40.txt"))
    ready, reason = seat.harness.opening_readiness(seat.home, seat.screen_now)
    assert not ready and "contributor mode is on" in reason
    with pytest.raises(ShipError, match="contributor mode is on"):
        launch_opening.deliver(seat, seat.launched())
    assert typed == []
    journal = json.loads(next((seat.folder / "launch-openings").iterdir()).read_text())
    assert journal["phase"] == "held" and journal["payload"] == BRIEF
    assert launch_opening.hold(seat)


def test_an_empty_off_screen_is_the_proof_and_a_multiline_opening_is_not_typed(tmp_path, monkeypatch):
    typed = []

    class RecordingDoor:
        def __init__(self, seat):
            self.opening_owner = None

        def screen(self):
            return seat.screen_now

        def deliver(self, text, verify=None):
            reason = seat.harness.input_obstruction(text, seat.screen_now)
            if reason:
                return Delivery("withheld", reason)
            typed.append(text)
            return Delivery("delivered", "synthetic")

    monkeypatch.setattr("quarterdeck.door.Door", RecordingDoor)
    line = muse_screen.CONTRIBUTOR_OFF
    seat = prepared(tmp_path, BRIEF, Screen(line, "", working=False))
    ready, reason = seat.harness.opening_readiness(seat.home, seat.screen_now)
    assert ready and "positive off line" in reason
    with pytest.raises(ShipError, match="held without retry"):
        launch_opening.deliver(seat, seat.launched())
    assert typed == []
    assert seat.harness.input_obstruction(BRIEF, seat.screen_now)
    journal = json.loads(next((seat.folder / "launch-openings").iterdir()).read_text())
    assert journal["phase"] == "held" and journal["payload"] == BRIEF


def test_the_measured_idle_off_screen_is_the_startup_proof(tmp_path):
    """The design names this fixture as the contributor-off proof. Padding in the
    measured empty row must not withhold it."""
    home = tmp_path / "home"
    home.mkdir()
    screen = screen_of("idle-off-120x40.txt")
    ready, reason = MuseHarness().opening_readiness(home, screen)
    assert ready, f"{reason}; measured box length {len(screen.box or '')}"
    assert "positive off line" in reason


def test_a_contributor_model_holds_the_opening_text(tmp_path):
    screen = screen_of("idle-off-120x40.txt")
    held = MuseHarness().delivery_obstruction(State(model="muse-spark"), screen)
    assert held and "contributor" in held
    assert "Admiral" not in held
