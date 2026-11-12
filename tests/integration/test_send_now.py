"""The real chord and Door's now speed consume one queued input within an open turn."""

from datetime import datetime, timezone
import time

import pytest

from quarterdeck import post, seat as seatmod
from quarterdeck.harness.synthetic_control import Rendezvous, events
from quarterdeck.input_receipt import confirmed

from .test_service import ship, qd
from .test_fakeharness import Seat, wait_for
from .send_now_forcing import record_behind_a_blind_screen

SETTLE = 1.0  # the fake begins a queued turn at once when one ends; a second turn would show well within this


def ends_as_one_turn(harness, record, home, payload):
    """Record first: wait, within a bound, for the record to show the running turn ended with its own reply.
    Then, after a short settle, neither a second turn nor an interrupt may have stood in for consumption."""
    def replies():
        return [turn.reply for turn in harness.turns(record)]

    ended = wait_for(replies)
    time.sleep(SETTLE)
    accepted = [e["text"] for e in events(home) if e["event"] == "accepted"]
    assert len(accepted) == 1 and accepted[0] != payload, "the consumed input never began a turn of its own"
    reply = f"done: {accepted[0].splitlines()[0]}"
    assert ended == [reply] and wait_for(replies) == [reply], "no second turn or interrupt stood in for consumption"
    assert wait_for(lambda: harness.state(record).turn_open is False)


@pytest.mark.parametrize("forced", [False, True], ids=["natural", "forced"])
@pytest.mark.parametrize("shape", ["claude", "codex"])
def test_send_now_chord_consumes_once_at_the_next_tool_boundary(tmux, tmp_path, shape, forced, monkeypatch):
    seat = Seat(tmux, tmp_path, **{"as": shape, "steps": 2, "step_seconds": "0.05"})
    payload = "exact  urgent input"

    def ended():
        ends_as_one_turn(seat.harness, record, seat.home, payload)
        screen = wait_for(lambda: (shown := seat.screen()).box is not None and not shown.working and shown)
        assert screen.queued == 0, "nothing is left queued to run as a second turn"

    with Rendezvous(seat.home, "step", "running turn") as boundary, \
            Rendezvous(seat.home, "consumed", payload) as consumed:
        seat.send("running turn")
        wait_for(boundary.reached)
        record = seat.record()
        assert seat.harness.state(record).open_tool is not None
        since = datetime.now(timezone.utc)
        seat.send(payload)
        wait_for(lambda: seat.screen().queued == 1)
        assert confirmed(seat.harness, record, payload, since) != "delivered"
        tmux.keys(seat.pane.id, *seat.harness.keys.send_now)
        wait_for(lambda: "· sent now:" in tmux.capture(seat.pane.id))
        assert not consumed.reached(), "the chord requests consumption; the tool boundary performs it"
        assert confirmed(seat.harness, record, payload, since) != "delivered"
        boundary.release()
        wait_for(consumed.reached)
        state = seat.harness.state(record)
        assert state.turn_open and state.turns == 0 and state.consumed == (payload,)
        assert state.open_tool is None, "the running tool ended before consumption"
        evidence = [i for i in seat.harness.inputs(record) if i.text == payload]
        assert len(evidence) == 1 and evidence[0].kind == "consumed"
        assert evidence[0].at >= since and evidence[0].consumed_at >= evidence[0].at
        assert confirmed(seat.harness, record, payload, since) == "delivered"
        tmux.keys(seat.pane.id, *seat.harness.keys.send_now)  # no remaining entry to consume twice
        if forced:  # the hosted race, forced at the fake's own consumed rendezvous as in the BEFORE proof
            record_behind_a_blind_screen(tmux, seat, record, consumed, monkeypatch)
            ended()
        consumed.release()
    if not forced:
        ended()
    assert sum(e == {"event": "consumed", "text": payload} for e in events(seat.home)) == 1
    assert sum(e == {"event": "submitted", "text": payload} for e in events(seat.home)) == 1


def test_post_now_consumes_during_the_running_turn_through_the_service(ship, tmux, capsys):
    qd(capsys, "up", "captain")
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    store, attempts = post.Store(ship.runtime), post.Attempts(ship.runtime)
    with Rendezvous(captain.home, "step", "running initial turn") as boundary, \
            Rendezvous(captain.home, "consumed", "urgent during this turn") as consumed:
        qd(capsys, "post", "order", "--to", "captain", "running initial turn")
        wait_for(boundary.reached)
        assert captain.harness.state(captain.record()).open_tool is not None
        ref = qd(capsys, "post", "question", "--to", "captain", "--now", "urgent during this turn").split()[0]
        message = store.get(ref)
        payload = post.typed([message]).strip()
        assert message.speed == "now"
        assert wait_for(lambda: attempts.latest("captain").get(message.id) == "queued")
        assert captain.harness.state(captain.record()).queued == (payload,)
        assert not consumed.reached()
        boundary.release()
        wait_for(consumed.reached)
        state = captain.harness.state(captain.record())
        assert state.turn_open and state.turns == 0 and state.consumed == (payload,)
        assert state.open_tool is None
        assert wait_for(lambda: attempts.latest("captain").get(message.id) == "delivered")
        assert len(attempts.for_message(message.id)) == 1
        consumed.release()
    ends_as_one_turn(captain.harness, captain.record(), captain.home, payload)
    assert not captain.is_working()
    assert sum(e == {"event": "consumed", "text": payload} for e in events(captain.home)) == 1
    assert sum(e == {"event": "submitted", "text": payload} for e in events(captain.home)) == 1
