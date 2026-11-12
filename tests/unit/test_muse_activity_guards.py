"""Unknown activity at the Door's action boundaries, at the common callers and under record authority.

Synthetic only: no native program, record capture or live identity. The Door gates drive the shared
typing path through a synthetic pane whose activity a test turns unknown from a chosen frame onward.
Once a frame of the action has been unknown, no later key or paste may follow it.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import cascade, check, cli, paste_refusal, post, recovery
from quarterdeck import watch as watchmod
from quarterdeck.door import Delivery, Door, Reading
from quarterdeck.harness.codex import CodexHarness
from quarterdeck.harness.muse import MuseHarness
from quarterdeck.harness.muse_screen import read as muse_read
from quarterdeck.harness.types import Control, Interruption, Keys, Screen, State
from quarterdeck.service import Service
from tests.contract.test_muse_screen import frame as muse_frame
from .test_pending_delivery import setup  # noqa: F401

NEVER = None
FRAMES = list(range(10)) + [NEVER]  # a whole ordinary delivery reads fewer frames than this


@pytest.fixture
def terminal(setup, monkeypatch):
    """The shared pane, with every frame's activity set by the test and every action recorded in order."""
    svc, seat, live, record = setup
    events = []
    plan = dict(activity=True, unknown_from=NEVER)
    real = CodexHarness.screen

    def screen(self, text):
        found = real(self, text)
        seen = sum(1 for event in events if event[0] == "frame")
        unknown = plan["unknown_from"] is not NEVER and seen >= plan["unknown_from"]
        found = replace(found, working=None if unknown else plan["activity"])
        events.append(("frame", found.working))
        return found

    def paste(*args):
        events.append(("paste",))
        live["pasted"] += 1
        live["box"] = args[1].rstrip("\n")

    def keys(pane, *pressed):
        events.append(("key",) + pressed)
        if pressed == ("Enter",):
            live["submitted"] += 1

    monkeypatch.setattr(CodexHarness, "screen", screen)
    monkeypatch.setattr(svc.tmux, "paste", paste)
    monkeypatch.setattr(svc.tmux, "keys", keys)
    return SimpleNamespace(svc=svc, seat=seat, live=live, events=events, plan=plan)


def acted_after_unknown(events) -> list:
    """Every paste or key that followed a frame whose activity was unknown."""
    first = next((index for index, event in enumerate(events) if event == ("frame", None)), None)
    if first is None:
        return []
    return [event for event in events[first:] if event[0] in ("paste", "key")]


def actions(events) -> list:
    return [event for event in events if event[0] in ("paste", "key")]


@pytest.mark.parametrize("activity", [True, False])
@pytest.mark.parametrize("unknown_from", FRAMES)
def test_no_paste_or_key_follows_an_unknown_frame_of_one_delivery(terminal, activity, unknown_from):
    terminal.plan.update(activity=activity, unknown_from=unknown_from)
    svc, seat, live = terminal.svc, terminal.seat, terminal.live
    message = svc.store.new("order", "issuer", seat.name, "Synthetic task\nexact  body")
    svc.deliver(seat, [message])
    result = svc.attempts.latest().get(message.id)
    assert acted_after_unknown(terminal.events) == []
    assert live["submitted"] <= live["pasted"] <= 1
    if ("frame", None) not in terminal.events:
        # Known activity, idle or in flight on a program that queues, still delivers exactly once.
        assert live["pasted"] == live["submitted"] == 1 and result == "pending"
    elif live["submitted"]:
        assert result == "pending"  # submitted before the unknown frame: the record alone confirms it
    elif live["pasted"]:
        assert result == "blind"  # the draft is kept, never cleared, submitted or retried
    else:
        assert result in (None, "withheld")
    before = list(actions(terminal.events))
    svc.deliveries()
    assert actions(terminal.events) == before  # nothing is replayed while activity stays unknown


@pytest.mark.parametrize("unknown_from", FRAMES)
def test_send_now_needs_a_known_working_frame_after_submit(terminal, monkeypatch, unknown_from):
    terminal.plan.update(activity=True, unknown_from=unknown_from)
    seat = terminal.seat
    monkeypatch.setattr(seat.harness, "keys", Keys(submit="Enter", send_now=("C-x", "C-s"), interrupt="Escape"))
    text = post.typed([terminal.svc.store.new("order", "issuer", seat.name, "Synthetic urgent task")])
    delivery = Door(seat).deliver(text, "now", confirm=False)
    sent = actions(terminal.events)
    assert acted_after_unknown(terminal.events) == []
    if ("frame", None) not in terminal.events:
        assert sent == [("paste",), ("key", "Enter"), ("key", "C-x", "C-s")]
        assert delivery.result == "typed"
    else:
        assert ("key", "C-x", "C-s") not in sent
        if ("key", "Enter") in sent:
            assert delivery.result == "blind" and "no further key sent" in delivery.detail


def test_send_now_is_not_sent_to_a_known_idle_program(terminal, monkeypatch):
    terminal.plan.update(activity=False)
    seat = terminal.seat
    monkeypatch.setattr(seat.harness, "keys", Keys(submit="Enter", send_now=("C-x", "C-s"), interrupt="Escape"))
    text = post.typed([terminal.svc.store.new("order", "issuer", seat.name, "Synthetic idle task")])
    assert Door(seat).deliver(text, "now", confirm=False).result == "typed"
    assert actions(terminal.events) == [("paste",), ("key", "Enter")]


@pytest.mark.parametrize("activity, unknown_from, sent", [
    (False, NEVER, True),
    (False, 0, False),
    (False, 1, False),  # the check just before the key
    (True, NEVER, False),  # a control is typed only to a known idle program
])
def test_control_keys_need_known_idle_at_both_checks(terminal, activity, unknown_from, sent):
    terminal.plan.update(activity=activity, unknown_from=unknown_from)
    proof = SimpleNamespace(same_launch=lambda seat: True)
    delivery = Door(terminal.seat).control_keys(("C-synthetic",), proof, lambda: True)
    assert acted_after_unknown(terminal.events) == []
    assert (actions(terminal.events) == [("key", "C-synthetic")]) is sent
    assert delivery.result == ("typed" if sent else "withheld")


NOTICE = Interruption("synthetic notice", "synthetic notice", key="Escape")


@pytest.mark.parametrize("given, current, dismissed", [
    (False, False, True),
    (None, False, False),  # the frame that showed the notice could not see activity
    (False, None, False),  # the frame read again just before the key could not
    (False, "other", False),  # the notice changed before the key
])
def test_a_known_notice_is_dismissed_only_on_two_known_frames(setup, monkeypatch, given, current, dismissed):
    svc, seat, live, record = setup
    pressed = []
    monkeypatch.setattr(svc.tmux, "keys", lambda pane, *keys: pressed.append(keys))
    shown = Screen("synthetic notice", "", working=given, interruption=NOTICE)
    if current == "other":
        again = Screen("synthetic other notice", "", working=False,
                       interruption=Interruption("synthetic other", "synthetic other", key="Escape"))
    else:
        again = Screen("synthetic notice", "", working=current, interruption=NOTICE)
    monkeypatch.setattr(Door, "screen", lambda self: again)
    Door(seat).clear_known(shown)
    assert pressed == ([("Escape",)] if dismissed else [])


@pytest.mark.parametrize("working, box, opened", [
    (False, "", True),
    (True, "", True),  # this program queues while it works
    (None, "", False),
    (None, "a person's draft", False),
])
def test_an_open_reading_always_carries_known_activity(setup, working, box, opened):
    svc, seat, live, record = setup
    reading = Door(seat)._reading(Screen("synthetic frame", box, working=working))
    assert reading.open is opened
    if opened:
        assert reading.working is working and str(reading) != "activity unknown"
    elif working is None:
        assert reading.reason == "current activity unknown" and reading.clears == "time"


@pytest.mark.parametrize("working, bound", [(False, True), (True, False), (None, False)])
def test_paste_refusal_binds_only_a_known_idle_empty_frame(setup, working, bound):
    svc, seat, live, record = setup
    found = paste_refusal.context(seat, "synthetic payload\n", Screen("synthetic frame", "", working=working))
    assert (found is not None) is bound


# Muse's own hooks: the Door needs a readable composer whatever the record says.

def test_muse_delivers_and_interrupts_only_on_a_readable_composer():
    muse = MuseHarness()
    idle, working = muse_read(muse_frame()), muse_read(muse_frame(working=True))
    redraw = muse_read(muse_frame(status=" steering the running turn"))
    assert (idle.working, working.working, redraw.working) == (False, True, None)
    assert muse.delivery_obstruction(None, idle) is None
    assert muse.delivery_obstruction(None, working) is None
    assert muse.delivery_obstruction(None, redraw) is not None
    assert muse.delivery_obstruction(State(turn_open=False), redraw) is not None  # a closed record does not open it
    assert muse.delivery_obstruction(None, None) is not None
    assert muse.interrupt_obstruction(None, working) is None
    assert muse.interrupt_obstruction(State(turn_open=True), working) is None
    assert muse.interrupt_obstruction(State(turn_open=False), working) is not None  # record first
    assert muse.interrupt_obstruction(None, idle) is not None
    assert muse.interrupt_obstruction(State(turn_open=True), redraw) is not None  # Escape needs a readable screen
    assert muse.interrupt_obstruction(State(turn_open=True), None) is not None


@pytest.mark.parametrize("text, idle", [
    (muse_frame(), True),
    (muse_frame(status=" steering the running turn"), False),
    (muse_frame(status=" interrupting run"), False),
    (muse_frame().replace("Launch overrides", "Unmeasured footer"), False),
    ("", False),
])
def test_recoverys_generic_idle_never_takes_an_unknown_muse_frame(text, idle):
    """Recovery keeps its generic reading until its own change; a Muse frame that cannot see activity
    always says why, so that reading still refuses it."""
    closed = State(turn_open=False, pending_tools=())
    assert recovery._idle(closed, muse_read(text)) is idle


# Lifecycle and routing: an exact record is the authority, and the screen is never read for it (policy A).

T0 = datetime(2026, 1, 2, tzinfo=timezone.utc)
COMPLETED = (SimpleNamespace(timed=True, started=T0, ended=T0 + timedelta(seconds=2)),)
RECORDS = {
    "closed": State(turn_open=False, completed_turns=COMPLETED, last_input_at=T0 + timedelta(seconds=1), pending_tools=()),
    "open": State(turn_open=True, completed_turns=COMPLETED, last_input_at=T0 + timedelta(seconds=1), pending_tools=()),
    "unknown": State(turn_open=None, completed_turns=COMPLETED, last_input_at=T0 + timedelta(seconds=1), pending_tools=()),
}


def blind(*args, **kwargs):
    raise AssertionError("record authority read the screen")


def recorded(state):
    restarted = []
    seat = SimpleNamespace(
        name="synthetic", record=lambda: Path("synthetic-record"), pane=lambda: SimpleNamespace(dead=False),
        join=lambda: SimpleNamespace(record=Path("synthetic-record")), read_pane=blind,
        harness=SimpleNamespace(state=lambda path: state, screen=blind, waiting_for_human=lambda state: False),
        restart=lambda: restarted.append("restart") or "synthetic restart",
        fresh=lambda: restarted.append("fresh") or "synthetic fresh")
    return seat, restarted


@pytest.mark.parametrize("record_state", sorted(RECORDS))
def test_move_readiness_is_decided_by_the_record_alone(record_state):
    seat, _ = recorded(RECORDS[record_state])
    reason = Service.not_idle(None, seat)
    assert (reason is None) is (record_state == "closed")


@pytest.mark.parametrize("record_state", sorted(RECORDS))
@pytest.mark.parametrize("resume", [True, False])
def test_service_relaunch_is_decided_by_the_record_alone(monkeypatch, record_state, resume):
    monkeypatch.setattr(recovery, "held", lambda seat: None)
    seat, restarted = recorded(RECORDS[record_state])
    result = Service.relaunch(None, seat, resume)
    if record_state == "closed":
        assert restarted == ["restart" if resume else "fresh"] and result is not None
    else:
        assert restarted == [] and result is None


# The common callers act on a known value and hold, or defer to the service, on unknown.

@pytest.mark.parametrize("working", [True, False, None])
def test_cli_relaunch_acts_now_only_on_known_idle(monkeypatch, working):
    called = []
    seat = SimpleNamespace(is_working=lambda: working, name="synthetic",
                           restart=lambda: called.append("restart") or "restarted",
                           fresh=lambda: called.append("fresh") or "fresh")
    monkeypatch.setattr(cli, "this_seat", lambda ship, name: "synthetic")
    monkeypatch.setattr(cli, "seat_of", lambda ship, name: seat)
    monkeypatch.setattr(cli.service, "request", lambda runtime, kind, **data: called.append(kind) or "requested")
    cli.relaunch(SimpleNamespace(runtime=Path("synthetic-runtime")), "synthetic", True)
    assert called == (["restart"] if working is False else ["relaunch"])


def controlled(working):
    return SimpleNamespace(
        name="synthetic", is_up=True, is_working=lambda: working,
        spec=SimpleNamespace(pool_choices=()),
        harness=SimpleNamespace(control=lambda name: Control(name, None, "/synthetic {value}", "record")))


@pytest.mark.parametrize("working", [True, False, None])
def test_in_session_control_waits_for_known_idle(monkeypatch, working):
    monkeypatch.setattr(recovery, "held", lambda seat: None)
    typed = []
    monkeypatch.setattr("quarterdeck.service.Door", lambda seat: SimpleNamespace(
        deliver=lambda text, **kwargs: typed.append(text) or Delivery("withheld", "synthetic")))
    result = Service.control(None, controlled(working), "effort", "low")
    if working is False:
        assert typed == ["/synthetic low"]
    else:
        assert typed == [] and result is None  # applied at the turn's end, never into unknown activity


@pytest.mark.parametrize("working, detail", [
    (True, "Previous turn still open; kept current effort."),
    (None, "Current activity unknown; kept current effort."),
    (False, "Effort control not confirmed; kept observed effort without retry."),
])
def test_cascade_effort_changes_only_a_known_idle_seat(monkeypatch, working, detail):
    applied = []
    seat = controlled(working)
    service = SimpleNamespace(seat=lambda name: seat,
                              control=lambda seat, name, value: applied.append(value))
    monkeypatch.setattr(watchmod, "running_settings", lambda seat: {"effort": "high"})
    answer = cascade.effort(service, "synthetic", "low")
    assert answer["detail"] == detail and answer["actual"] == "high"
    assert applied == (["low"] if working is False else [])


@pytest.mark.parametrize("working, opening, ready", [
    (False, False, True),
    (True, False, True),  # no opening turn to wait for: an open known reading is enough
    (False, True, True),
    (True, True, False),  # its opening turn still runs
    (None, False, False),
    (None, True, False),
])
def test_check_probe_needs_known_activity(working, opening, ready):
    finished = State(turn_open=False, turns=1)  # the opening turn ended in the record
    seat = SimpleNamespace(harness=SimpleNamespace(opening_prompt_turn=opening, state=lambda path: finished),
                           record=lambda: Path("synthetic-record"))
    door = SimpleNamespace(reading=lambda: Reading(True, working=working))
    assert check.ready_for_probe(seat, door) is ready
