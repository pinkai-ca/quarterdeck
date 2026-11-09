"""The door on the fake harness: one path, cleared, checked, submitted, confirmed by the record."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from quarterdeck import post, seat as seatmod, ship as shipmod
from quarterdeck.door import Door
from quarterdeck.tmux import Tmux

from .test_seat import wait_for
from tests.conftest import FIXTURES


def ship_with(drydock: Path, extra: str) -> shipmod.Ship:
    (drydock / "ship.toml").write_text((drydock / "ship.toml").read_text() + extra)
    return shipmod.load(drydock)


@pytest.fixture
def captain(drydock: Path, tmux: Tmux) -> seatmod.Seat:
    ship = ship_with(drydock, '\n[seats.captain.settings]\nstep_seconds = "0.05"\n')
    seat = seatmod.Seat(ship, ship.seat("captain"), tmux)
    seat.up()
    wait_for(lambda: Door(seat).reading().open)
    return seat


def order(seat: seatmod.Seat, body: str, **fields) -> post.Message:
    return post.Store(seat.ship.runtime).new("order", "desk", seat.name, body, **fields)


def test_a_message_is_typed_once_and_confirmed_by_the_record(captain: seatmod.Seat, tmux: Tmux):
    door = Door(captain)
    message = order(captain, "Count the stars.\n$ echo counted", subject="Stars")
    text = post.typed([message])
    delivery = door.deliver(text)
    assert delivery.result == "delivered" and "in the record" in delivery.detail  # law: one-door
    turns = wait_for(lambda: captain.harness.turns(captain.record()) or None)
    assert turns[0].input == text.strip() and [t.name for t in turns[0].tools] == ["Bash"]
    screen = tmux.capture(captain.pane().id, history=100)
    assert f"qd > order from desk · {message.short} · Stars" in screen and "⎿ counted" in screen
    assert door.reading().open and str(door.reading()) == "open"


@pytest.mark.parametrize("program,version,frame", [("pi", "0.74.2", "startup-blank"),
                                                  ("opencode", "1.19.5", "startup-blank"),
                                                  ("codex", "0.166.0", "startup-logo")])
def test_startup_holds_without_keys_then_ready_delivers_once_with_a_receipt(captain, tmux, monkeypatch, program, version, frame):
    """Replay the captured frame over the real fake pane, then release its ordinary box."""
    from quarterdeck import harness

    raw = (FIXTURES / program / version / f"{frame}.txt").read_text()
    startup = harness.get(program).screen(raw)
    native_screen, read_pane = captain.harness.screen, tmux.read_pane
    holding = [True]
    monkeypatch.setattr(captain.harness, "screen", lambda text: startup if text == raw else native_screen(text))
    def startup_packet(*args, **kwargs):
        found = read_pane(*args, **kwargs)
        return (found[0], raw) if found and holding[0] else found
    monkeypatch.setattr(tmux, "read_pane", startup_packet)
    sent, pressed = [], []
    paste, keys = tmux.paste, tmux.keys

    def tracked_paste(*args, **kwargs):
        sent.append(args)
        return paste(*args, **kwargs)

    def tracked_keys(*args, **kwargs):
        pressed.append(args)
        return keys(*args, **kwargs)

    monkeypatch.setattr(tmux, "paste", tracked_paste)
    monkeypatch.setattr(tmux, "keys", tracked_keys)
    door = Door(captain)
    message = order(captain, "After startup")
    payload = post.typed([message])
    for _ in range(2):
        delivery = door.deliver(payload)
        assert delivery.result == "withheld" and "starting" in delivery.detail
    assert sent == pressed == [] and captain.record() is None
    holding[0] = False
    assert door.reading().open
    assert door.deliver(payload).result == "delivered"
    assert len(sent) == len(pressed) == 1
    assert captain.harness.inputs(captain.record())[-1].text == payload.strip()


def test_text_in_the_box_is_an_obstruction_and_nothing_more_is_done(captain: seatmod.Seat, tmux: Tmux):
    tmux.paste(captain.pane().id, "a human's half-written thou")
    wait_for(lambda: captain.harness.screen(tmux.capture(captain.pane().id)).box == "a human's half-written thou")
    door = Door(captain)
    reading = door.reading()
    assert not reading.open and str(reading) == "obstructed: text in the box, needs a human"
    delivery = door.deliver(post.typed([order(captain, "never sent")]))
    assert delivery.result == "withheld" and "text in the box" in delivery.detail  # law: one-door
    time.sleep(0.3)
    assert captain.harness.screen(tmux.capture(captain.pane().id)).box == "a human's half-written thou", "untouched"
    assert captain.record() is None, "nothing was submitted"


def test_a_known_notice_is_cleared_and_an_unknown_one_refused(drydock: Path, tmux: Tmux):
    ship = ship_with(drydock, '\n[seats.captain.settings]\ndialog = "daybreak"\nstep_seconds = "0.05"\n'
                                   '\n[seats.worker-1.settings]\ndialog = "unknown"\n')
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    captain.up()
    wait_for(lambda: Door(captain).screen() and Door(captain).screen().interruption)
    door = Door(captain)
    assert str(door.reading()) == "obstructed: daybreak notice, dismissable"
    delivery = door.deliver(post.typed([order(captain, "after the notice")]))
    assert delivery.result == "delivered"
    worker = seatmod.Seat(ship, ship.seat("worker-1"), tmux)
    worker.up()
    wait_for(lambda: Door(worker).screen() and Door(worker).screen().unknown)
    reading = Door(worker).reading()
    assert str(reading) == "obstructed: unknown dialog: Something else, needs a human"
    assert Door(worker).deliver("x").result == "withheld" and Door(worker).screen().unknown == "Something else"


def test_a_message_mid_turn_is_queued_and_the_speeds_reach_the_program(drydock: Path, tmux: Tmux):
    ship = ship_with(drydock, '\n[seats.captain.settings]\nsteps = "4"\nstep_seconds = "0.7"\n')
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    captain.up()
    door = Door(captain)
    wait_for(lambda: door.reading().open)
    assert door.deliver(post.typed([order(captain, "a long one")])).result == "delivered"
    wait_for(lambda: door.screen().working)
    assert str(door.reading()) == "open, queues"
    queued = door.deliver(post.typed([order(captain, "a second, mid-turn")]))
    assert queued.result == "queued", queued
    wait_for(lambda: captain.harness.state(captain.record()).turns == 2, seconds=20)
    wait_for(lambda: door.screen().working or captain.harness.state(captain.record()).turns == 2)
    # interrupt: the turn stops, then the message goes in
    assert door.deliver(post.typed([order(captain, "a third, slow\n")])).result == "delivered"
    wait_for(lambda: door.screen().working)
    stopped = door.deliver(post.typed([order(captain, "stop and take this")]), speed="interrupt")
    assert stopped.result in ("delivered", "queued")
    assert "· interrupted" in tmux.capture(captain.pane().id, history=200)
    assert door.interrupt().startswith("captain interrupted with Escape in %")


def test_a_program_that_does_not_queue_is_obstructed_by_time(drydock: Path, tmux: Tmux):
    ship = ship_with(drydock, '\n[seats.captain.settings]\nqueues = false\nsteps = "4"\nstep_seconds = "0.5"\n')
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    captain.up()
    door = Door(captain)
    wait_for(lambda: door.reading().open)
    assert door.deliver(post.typed([order(captain, "first")])).result == "delivered"
    wait_for(lambda: door.screen().working)
    assert str(door.reading()) == "obstructed: working, program does not queue, time"
    assert door.deliver("second").result == "withheld"
    # --interrupt is for exactly this seat: the turn is stopped first, then the message goes in
    urgent = door.deliver(post.typed([order(captain, "urgent")]), speed="interrupt")
    assert urgent.result == "delivered", urgent
    assert "· interrupted" in tmux.capture(captain.pane().id, history=100)


def test_the_door_never_types_into_a_human_session(drydock: Path, tmux: Tmux):
    ship = shipmod.load(drydock)
    desk = seatmod.Seat(ship, ship.seat("desk"), tmux)
    desk.up()
    door = Door(desk)
    assert str(door.reading()) == "obstructed: a human's session, its watch delivers"  # law: one-door
    assert door.deliver("anything").result == "withheld"
    time.sleep(0.3)
    assert desk.harness.screen(tmux.capture(desk.pane().id)).box == ""


def test_an_empty_box_without_a_record_receipt_stays_unconfirmed(drydock: Path, tmux: Tmux):
    """An emptied box is not acceptance; only the later exact input confirms the single paste."""
    ship = ship_with(drydock, '\n[seats.captain.settings]\nstep_seconds = "0.05"\naccept_seconds = "7"\n')
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    captain.up()
    wait_for(lambda: Door(captain).reading().open)
    door = Door(captain)
    text = post.typed([order(captain, "slow to accept")])
    delivery = door.deliver(text)
    assert delivery.result == "blind", delivery
    assert wait_for(lambda: door.confirm(text, seconds=0).result == "delivered", 15)  # law: one-door
    assert sum(item.text.strip() == text.strip() for item in captain.harness.inputs(captain.record())) == 1


def test_an_echo_longer_than_the_screen_is_read_from_the_scrollback(tmux: Tmux, tmp_path: Path):
    """A long message's echo climbs above a 24-line pane; the adapter can still read its echo from the transcript, scrollback included."""
    from types import SimpleNamespace

    from quarterdeck.door import echoed
    from quarterdeck.harness.codex import CodexHarness

    body = "\n".join(f"line {i} of the long order" for i in range(40))
    shown = [f"filler {i}" for i in range(60)] + ["› qd > order from desk · ord-1234 · Long"] + ["  " + line for line in body.splitlines()]
    shown += ["", "• Working (3s • esc to interrupt)", "", "› Ask Codex to do anything", "", "  GPT-6-Astra low · /work · main"]
    script = tmp_path / "transcript.sh"
    script.write_text("cat <<'EOF'\n" + "\n".join(shown) + "\nEOF\nsleep 30\n")
    tmux.new_session("Crew")
    pane = tmux.new_window("Crew", "scott", tmp_path, ["sh", str(script)])
    wait_for(lambda: "GPT-6-Astra" in tmux.capture(pane.id))
    seat = SimpleNamespace(name="scott", pane=lambda: tmux.pane(pane.id), harness=CodexHarness(), tmux=tmux,
                           session=SimpleNamespace(human=False))
    door = Door(seat)
    wanted = " ".join(f"qd > order from desk · ord-1234 · Long\n{body}".split())
    assert not any(echoed(e, wanted) for e in door.screen().echoes), "the screen alone shows only the echo's tail"
    assert any(echoed(e, wanted) for e in door.echoes()), "the scrollback holds the whole echo"  # law: one-door


def test_a_scrolled_pane_is_delivered_through(captain: seatmod.Seat, tmux: Tmux):
    tmux.run("copy-mode", "-t", captain.pane().id)
    assert tmux.pane(captain.pane().id).in_mode
    delivery = Door(captain).deliver(post.typed([order(captain, "read while scrolled")]))
    assert delivery.result == "delivered", delivery
    assert tmux.pane(captain.pane().id).in_mode, "the reader keeps their place"
