"""Synthetic gates for Muse's quota-offer dialog.

The fixed-path mask is gone. What it proved about where a launch may write now
lives in test_muse_native_home_gates.py and the real-process confinement test.
Dialog screens are built only from the labels the pinned binary carries; their
layout, option order and preselection are unmeasured, so the gates never depend
on positions. The ordinary composer is the measured 1.4.20 width.
"""

from __future__ import annotations

import pytest

import json

from quarterdeck import launch_opening, turn_failure
from quarterdeck.door import Door
from quarterdeck.harness import get, muse_screen, NAMES
from quarterdeck.harness.base import Harness
from quarterdeck.harness.muse import MuseHarness
from quarterdeck.harness.types import State

from .test_owned_paste import editor  # noqa: F401  (shared synthetic editor fixture)

TITLE = "You're using your quota fast"
OPTIONS = ("Keep going", "Switch to Spark", "Turn on contributor mode")
SWITCHED = "Switched to Spark (saved as default)"
CONTRIBUTOR_ON = "Contributor mode on"
CONTRIBUTOR_OFF_NOTICE = ("Contributor mode is off, so your content won't be used for product improvement. "
                          "Turn it on in /settings.")


def row(text: str) -> str:
    if len(text) > 120:
        return text
    return text + " " * (120 - len(text))


def composer(text: str = "", above: str = "") -> str:
    """A synthetic 1.4.20 idle composer. An empty prompt row is exactly ``❯``, so the box is empty."""
    prompt = "❯" if text == "" else f"❯ {text}"
    if text and len(prompt) <= 120:
        prompt = row(prompt)
    lines = [row("  Muse Code 1.4.20")]
    if above:
        lines.append(row(above) if len(above) <= 120 else above)
    lines += ["─" * 120, prompt, "─" * 120, row("  echo · /synthetic/workspace · Launch overrides")]
    return "\n".join(lines) + "\n"


def offer(*options: str, title: bool = True, wrap: bool = False) -> str:
    lines = ([TITLE] if title else []) + [f"  {option}" for option in options]
    if wrap:  # every word on its own line: no layout is assumed
        lines = [word for line in lines for word in line.split(" ")]
    return "\n".join(lines) + "\nnot active yet - esc close\n"


def test_only_muse_overrides_the_guard_hooks_and_the_strict_interval():
    for name in NAMES:
        cls = type(get(name))
        hooks = (cls.delivery_obstruction is Harness.delivery_obstruction,
                 cls.interrupt_obstruction is Harness.interrupt_obstruction, cls.strict_usage_interval is False)
        assert hooks == ((False, False, False) if name == "muse" else (True, True, True)), name


# -- the quota offer: named, never answered ------------------------------------------------------


@pytest.mark.parametrize("option", OPTIONS)
def test_the_offer_is_named_from_its_title_and_any_one_option(option):
    for text in (offer(option), offer(option) + composer(), "\x1b[1m" + offer(option) + "\x1b[0m"):
        screen = muse_screen.read(text)
        assert screen.interruption == muse_screen.QUOTA_OFFER and screen.box is None
        assert screen.unknown is None and not screen.working
    plain = muse_screen.read(composer())
    assert plain.interruption is None and plain.unknown is None and plain.box == ""
    assert plain.working is False  # the control: an ordinary composer still opens


@pytest.mark.parametrize("text", [offer(*OPTIONS, wrap=True), offer(), offer(*OPTIONS) + composer("draft")],
                         ids=["wrapped", "options-unreadable", "over-a-composer"])
def test_a_wrapped_cut_or_overlaid_offer_is_held_never_open(text):
    screen = muse_screen.read(text)
    assert screen.interruption in (muse_screen.QUOTA_OFFER, muse_screen.QUOTA_INCOMPLETE)
    assert screen.box is None


def offering(editor, monkeypatch, text):
    editor.seat.harness = MuseHarness()
    monkeypatch.setattr(editor.terminal, "capture", lambda *a, **kw: text)
    monkeypatch.setattr(Door, "adapter_state", lambda self: None)
    monkeypatch.setattr(turn_failure, "obstruction", lambda seat, store=None: None)
    return Door(editor.seat)


def test_while_the_offer_shows_the_door_types_nothing_and_presses_no_key(editor, monkeypatch):
    door = offering(editor, monkeypatch, offer(*OPTIONS) + composer())
    result = door.deliver("A fictional order.")
    assert result.result == "withheld" and muse_screen.QUOTA_OFFER.name in result.detail
    assert door.interrupt() and editor.terminal.submitted == []
    assert door.clear_known() is not None
    assert editor.terminal.pastes == [] and editor.terminal.submitted == [] and editor.terminal.clears == []


def test_the_esc_hint_never_makes_a_model_choice_dismissable():
    for interruption in muse_screen.MODEL_CHOICES:
        assert interruption.key is None and not interruption.dismissable


def test_the_offer_reaches_a_person_and_is_named(editor, monkeypatch):
    door = offering(editor, monkeypatch, offer("Switch to Spark"))
    held, _ = door.human_obstruction()
    assert not held.open and "launch opening" in held.reason
    launched = editor.seat.launched()
    launch_opening.prepare(editor.seat, launched, "A fictional brief.")
    path = launch_opening.path_for(editor.seat, launched)
    recorded = json.loads(path.read_text())
    recorded["phase"] = "delivered"
    path.write_text(json.dumps(recorded) + "\n")
    reading, reason = door.human_obstruction()
    assert not reading.open and reading.clears == "needs a human"
    assert reason and muse_screen.QUOTA_OFFER.name in reason and "needs a human" in reason


def test_an_offer_alone_claims_no_model_and_no_contributor_state():
    screen = muse_screen.read(offer(*OPTIONS))
    assert "model" not in screen.shows
    harness = MuseHarness()
    state = State(turn_open=False, model="synthetic-catalog-model")
    held = harness.delivery_obstruction(state, screen)
    assert held and "contributor model" not in held and "selected" not in held
    assert harness.delivery_obstruction(state, muse_screen.read(composer())) is None


@pytest.mark.parametrize("text,named", [
    (SWITCHED, muse_screen.MODEL_SWITCHED),
    (CONTRIBUTOR_ON, muse_screen.CONTRIBUTOR_ENABLED),
    ("Contributor mode is on for this session only: settings could not be saved.", muse_screen.CONTRIBUTOR_ENABLED),
])
def test_a_made_choice_is_reported_as_a_change_and_holds(text, named):
    screen = muse_screen.read(composer(above=text + "\n"))
    assert screen.interruption == named and screen.box is None
    assert MuseHarness().delivery_obstruction(None, screen)


def test_the_off_notice_is_not_misread_as_contributor_on_and_a_contributor_record_holds():
    assert muse_screen.read(composer(above=CONTRIBUTOR_OFF_NOTICE + "\n")).interruption is None
    held = MuseHarness().delivery_obstruction(State(model="synthetic-catalog-model-contributor"), None)
    assert held and "contributor" in held


@pytest.mark.parametrize("option", ["Switch to Spark", "Turn on contributor mode"])
def test_a_model_or_data_use_option_without_its_title_still_holds(option):
    """The captain's rule: either label anywhere names a held quota offer; a quoted line is a safe false hold."""
    for text in (composer(above=f"  {option}\n"), offer(option, title=False) + composer(),
                 offer(option, title=False, wrap=True) + composer()):
        screen = muse_screen.read(text)
        assert screen.interruption in muse_screen.MODEL_CHOICES and screen.box is None
        assert MuseHarness().delivery_obstruction(None, screen)


def test_keep_going_alone_is_too_ordinary_to_hold():
    screen = muse_screen.read(composer(above="  Keep going\n"))
    assert screen.interruption is None and screen.box == ""
