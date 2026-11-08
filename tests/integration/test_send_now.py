"""The real chord and Door's now speed consume one queued input within an open turn."""

from datetime import datetime, timezone

import pytest

from quarterdeck import post, seat as seatmod
from quarterdeck.harness.synthetic_control import Rendezvous, events
from quarterdeck.input_receipt import confirmed

from .test_service import ship, qd
from .test_fakeharness import Seat, wait_for


@pytest.mark.parametrize("shape", ["claude", "codex"])
def test_send_now_chord_consumes_once_at_the_next_tool_boundary(tmux, tmp_path, shape):
    seat = Seat(tmux, tmp_path, **{"as": shape, "steps": 2, "step_seconds": "0.05"})
    payload = "exact  urgent input"
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
        consumed.release()
    wait_for(lambda: not seat.screen().working)
    assert len(seat.harness.turns(record)) == 1, "no second turn or interrupt stood in for consumption"
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
    wait_for(lambda: not captain.is_working())
    assert len(captain.harness.turns(captain.record())) == 1
    assert sum(e == {"event": "consumed", "text": payload} for e in events(captain.home)) == 1
    assert sum(e == {"event": "submitted", "text": payload} for e in events(captain.home)) == 1
