"""The door waits for the box to show the message, its event, never a guess at how fast a program draws.

A synthetic Codex seat and a stand-in tmux whose box the test draws by an injected clock. The clock moves only by
the door's own waits, so twenty seconds of waiting take no real time. Nothing is typed, resent or cleared while it
waits.
"""

import json

import pytest

from quarterdeck import door as doormod
from quarterdeck.door import cannot_become, fold_head
from quarterdeck.harness.types import Box
from .test_pending_delivery import setup  # noqa: F401


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def drawn(setup, monkeypatch):
    """The box shows what ``draw`` gives for the seconds since the paste."""
    svc, seat, live, record = setup
    clock = Clock()
    monkeypatch.setattr(doormod, "_clock", clock)
    monkeypatch.setattr(doormod, "_sleep", clock.sleep)
    state = {"pasted_at": None, "draw": lambda text, since: text}

    def paste(*args):
        live["pasted"] += 1
        state["pasted"], state["text"], state["pasted_at"] = args[1], args[1].rstrip("\n"), clock.now

    def capture(*a, **k):
        box = ""
        if state["pasted_at"] is not None:
            box = state["draw"](state["text"], clock.now - state["pasted_at"])
        return "• Working (1s • esc to interrupt)\n› " + box.replace("\n", "\n  ") + "\n\n  test low · /work\n"

    svc.tmux.paste, svc.tmux.capture = paste, capture
    return svc, seat, live, state, clock


def deliver(svc, seat):
    message = svc.store.new("order", "desk", seat.name, "a precise  payload\nsecond line")
    svc.deliver(seat, [message])
    return message


def box_waits(seat):
    log = seat.ship.runtime / "log" / "service.jsonl"
    rows = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return [row for row in rows if row.get("event") == "box-wait"]


def test_a_box_drawn_at_2_6_seconds_is_submitted_once(drawn):
    svc, seat, live, state, clock = drawn
    state["draw"] = lambda text, since: text if since >= 2.6 else ""
    message = deliver(svc, seat)
    assert live["pasted"] == 1 and live["submitted"] == 1, "typed once, submitted once, nothing cleared"
    assert svc.attempts.latest()[message.id] != "blind"
    [wait] = box_waits(seat)
    assert wait["seat"] == "worker" and wait["program"] == "codex" and wait["outcome"] == "shown"
    assert 2.6 <= wait["seconds"] <= 2.8


def test_a_box_drawn_at_once_is_not_logged(drawn):
    svc, seat, live, state, clock = drawn
    deliver(svc, seat)
    assert live["submitted"] == 1 and box_waits(seat) == []


def test_a_foreign_box_stops_the_wait_at_once(drawn):
    svc, seat, live, state, clock = drawn
    state["draw"] = lambda text, since: "synthetic foreign draft"
    began = clock.now
    message = deliver(svc, seat)
    assert clock.now - began < 1.0, "not the safety stop"
    assert live["pasted"] == 1 and live["submitted"] == 0
    assert svc.attempts.latest()[message.id] in ("blind", "withheld")
    assert box_waits(seat) == []


def test_an_empty_box_that_never_fills_is_blind_at_the_safety_stop(drawn):
    svc, seat, live, state, clock = drawn
    state["draw"] = lambda text, since: ""
    began = clock.now
    message = deliver(svc, seat)
    assert doormod.PASTE_SHOWN_SECONDS <= clock.now - began < doormod.PASTE_SHOWN_SECONDS + 2
    assert svc.attempts.latest()[message.id] == "blind"
    assert live["pasted"] == 1 and live["submitted"] == 0, "nothing typed again, nothing submitted"
    [wait] = box_waits(seat)
    assert wait["outcome"] == "safety stop" and wait["seconds"] >= doormod.PASTE_SHOWN_SECONDS


def test_a_box_still_drawing_its_start_keeps_the_wait(drawn):
    """A box showing the start of the message, as a program mid-draw would, is waited for."""
    svc, seat, live, state, clock = drawn
    state["draw"] = lambda text, since: text if since >= 5 else text[:int(len(text) * since / 5)]
    message = deliver(svc, seat)
    assert live["submitted"] == 1 and svc.attempts.latest()[message.id] != "blind"
    assert box_waits(seat)[0]["outcome"] == "shown"


def test_a_torn_read_alone_does_not_stop_the_wait(drawn):
    """One read of a foreign box, as a capture mid-redraw may give, is not enough: it must be read so twice."""
    svc, seat, live, state, clock = drawn
    reads = {"n": 0}

    def draw(text, since):
        reads["n"] += 1
        return "torn" if reads["n"] == 2 else (text if since >= 1.0 else "")
    state["draw"] = draw
    deliver(svc, seat)
    assert live["submitted"] == 1


@pytest.mark.parametrize("box, stops", [
    (None, False), ("", False), ("   \n  ", False),
    ("a precise", False), ("a precise  payload\n  second", False), ("a precise payload second line", False),
    ("a pre\n  cise", False),
    ("synthetic foreign draft", True), ("second line", True), ("a precise  payload\nsecond line!", True),
])
def test_what_cannot_become_the_message(box, stops):
    assert cannot_become(Box("a precise  payload\nsecond line"), box) is stops


def fold_of(seat, text):
    """How the Codex box folds this pasted text: '[Pasted Content N chars]', N the text's length."""
    return seat.harness.box(text).folded.replace("\\[", "[").replace("\\]", "]")


def test_a_fold_drawn_in_two_steps_its_first_half_read_twice_is_waited_for(drawn):
    svc, seat, live, state, clock = drawn

    def draw(text, since):
        fold = fold_of(seat, state["pasted"])  # the fold counts the text as pasted
        return fold if since >= 1.0 else fold[:len(fold) // 2]
    state["draw"] = draw
    message = deliver(svc, seat)
    assert live["pasted"] == 1 and live["submitted"] == 1, "the half-drawn fold was waited for"
    assert svc.attempts.latest()[message.id] != "blind"
    assert box_waits(seat) == [], "drawn within the first two seconds"


def test_a_box_that_starts_neither_the_text_nor_its_fold_is_still_stopped_early(drawn):
    svc, seat, live, state, clock = drawn
    state["draw"] = lambda text, since: "[Pasted Content 9"
    began = clock.now
    message = deliver(svc, seat)
    assert clock.now - began < 1.0 and live["submitted"] == 0
    assert svc.attempts.latest()[message.id] in ("blind", "withheld")


@pytest.mark.parametrize("pattern, head", [
    (r"\[Pasted text #\d+ \+2 lines\]", "[Pasted text #"),
    (r"\[Pasted text #\d+ (?:(?:40) chars|\+(?:2) lines)\]", "[Pasted text #"),
    (r"\[paste #\d+ (?:\+2 lines|40 chars)\]", "[paste #"),
    (r"\[Pasted Content 40 chars\]", "[Pasted Content 40 chars]"),
    (r"\[Pasted ~2 lines\]", "[Pasted ~2 lines]"),
    (r"\[Pasted: 5 lines\]", "[Pasted: 5 lines]"),
    (None, ""),
])
def test_a_folds_literal_head(pattern, head):
    assert fold_head(pattern) == head


@pytest.mark.parametrize("drawn, stops", [
    ("[Pasted text #3 +2 li", False), ("[Pasted te", False), ("[Pasted text #", False),
    ("[Pasted text #3 +2 lines]", False), ("[Copied text #3", True), ("Pasted text", True),
])
def test_a_box_that_starts_a_fold_may_still_become_it(drawn, stops):
    box = Box("first\nsecond\nthird", folded=r"\[Pasted text #\d+ \+2 lines\]")
    assert cannot_become(box, drawn) is stops
