"""Ordinary activity values and display, without a native program or input actions."""

from types import SimpleNamespace

import pytest

from quarterdeck import watch
from quarterdeck.door import Reading
from quarterdeck.harness.muse import MuseHarness
from quarterdeck.harness.types import Screen, State
from quarterdeck.ledger import Count
from quarterdeck.seat import Seat
from quarterdeck.service import Service
from quarterdeck.tmux import TmuxError


def observed(record_activity=None, screen_activity=None):
    captured = []
    seat = SimpleNamespace(
        pane=lambda: SimpleNamespace(dead=False), record=lambda: "synthetic-record",
        harness=SimpleNamespace(state=lambda path: State(turn_open=record_activity),
                                screen=lambda text: Screen(text, "", working=screen_activity)),
        read_pane=lambda: (captured.append(True) or (None, "synthetic frame")),
    )
    return seat, captured


@pytest.mark.parametrize("record_activity", [True, False, None])
@pytest.mark.parametrize("screen_activity", [True, False, None])
def test_seat_preserves_record_authority_and_nullable_screen_fallback(record_activity, screen_activity):
    seat, captured = observed(record_activity, screen_activity)
    # An unseen screen holds. Only a screen read idle, or a record that says so, is not working.
    if record_activity is not None:
        expected = record_activity
    else:
        expected = False if screen_activity is False else True
    assert Seat.is_working(seat) is expected
    assert bool(captured) is (record_activity is None)


@pytest.mark.parametrize("screen_activity", [True, False, None])
def test_unreadable_record_uses_the_observed_screen_value(screen_activity):
    seat, _ = observed(screen_activity=screen_activity)
    def unreadable(path):
        raise OSError("synthetic unreadable record")
    seat.harness.state = unreadable
    assert Seat.is_working(seat) is (False if screen_activity is False else True)


def test_missing_capture_of_a_live_pane_is_unknown():
    seat, _ = observed()
    seat.read_pane = lambda: None
    assert Seat.is_working(seat) is True


@pytest.mark.parametrize("error", [OSError, TmuxError])
def test_capture_error_is_unknown_instead_of_off(error):
    seat, _ = observed()
    def unavailable():
        raise error("synthetic unavailable capture")
    seat.read_pane = unavailable
    assert Seat.is_working(seat) is True


def test_proven_absent_launch_and_dead_owned_pane_are_off():
    seat, _ = observed()
    seat.pane = lambda: None
    seat.launched = lambda: None
    assert Seat.is_working(seat) is False
    seat.pane = lambda: SimpleNamespace(dead=True)
    assert Seat.is_working(seat) is False


def test_unknown_owned_pane_identity_does_not_become_off():
    seat, _ = observed()
    seat.pane = lambda: None
    launch = SimpleNamespace(pane="synthetic-pane")
    seat.launched = lambda: launch
    seat.tmux = SimpleNamespace(pane=lambda pane: SimpleNamespace())
    assert Seat.is_working(seat) is None


def test_legacy_screen_default_and_unobserved_reading_are_separate():
    assert Screen("synthetic legacy frame", "").working is None
    assert Reading(False, "unobserved").working is None
    assert str(Reading(True, working=False)) == "open"
    assert str(Reading(True, working=True)) == "open, queues"


@pytest.mark.parametrize("screen, expected", [
    (None, "unknown"),
    (Screen("synthetic unknown frame", None, working=None), "unknown"),
    (Screen("synthetic complete frame", "", working=False), "idle"),
    (Screen("synthetic complete frame", "", working=True), "working"),
])
def test_unjoined_watch_activity_requires_an_observed_value(monkeypatch, screen, expected):
    seat = SimpleNamespace(pane=lambda: SimpleNamespace(dead=False), harness=MuseHarness(),
                           join=lambda: SimpleNamespace(record=None, reason="not yet written"))
    monkeypatch.setattr(watch, "Door", lambda value: SimpleNamespace(screen=lambda: screen))
    assert watch.activity(seat).state == expected


@pytest.mark.parametrize("screen", [None, Screen("synthetic frame", None, working=None)])
def test_watch_reports_unknown_screen_separately_from_record_idle(monkeypatch, screen):
    row = SimpleNamespace(name="synthetic", today=Count(), activity=watch.Activity("idle"),
                          door=Reading(False, "screen unavailable", "time"), order_text="-",
                          harness_text="muse", provider_name="Meta", pool="synthetic", model_text="-",
                          effort_text="-", wake=None, person_quiet=None, compaction=None)
    monkeypatch.setattr(watch, "render_headlines", lambda usage: [])
    text = "\n".join(watch.render_full(dict(seat=row, record=None, state=None, record_absent="not joined",
                                           record_limits=None, screen=screen, usage={})))
    assert "synthetic: idle" in text
    assert "working unknown" in text
    assert "working False" not in text


def test_follow_has_no_idle_baseline_before_the_first_record():
    service = SimpleNamespace(work_marks={}, working={})
    seat = SimpleNamespace(name="synthetic", launched=lambda: "synthetic launch", record=lambda: None)
    assert Service._working(service, seat) is None
    assert service.working == {} and service.work_marks == {}
