"""qd down proves the seat's program has ended before it reseeds or removes the launch.

Real starts on the drydock's private tmux server: the fake program, told to outlive its closed pane for a while
(``linger_seconds``), as Claude Code did at a real ``qd down``. Nothing is signalled by qd; it only observes.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import credential, seat as seatmod, service, ship as shipmod

from .waiting import wait_for


@pytest.fixture
def lingering(drydock: Path, tmux, monkeypatch):
    def make(seconds: float):
        text = (drydock / "ship.toml").read_text() + f'\n[seats.captain.settings]\nlinger_seconds = "{seconds}"\n'
        (drydock / "ship.toml").write_text(text)
        ship = shipmod.load(drydock)
        seat = seatmod.Seat(ship, ship.seat("captain"), tmux)
        reseeded = []
        monkeypatch.setattr(credential, "reseed", lambda ship, seats: reseeded.append([s.name for s in seats]) or [])
        return SimpleNamespace(ship=ship, seat=seat, reseeded=reseeded)
    return make


def test_down_waits_for_a_lingering_program_then_reseeds_and_removes_the_launch(lingering):
    w = lingering(1.5)
    w.seat.up(resume=False)
    pid = w.seat.launched().pid
    started = time.monotonic()
    proof = w.seat.down()
    assert proof.endswith("closed · process ended"), proof
    assert time.monotonic() - started >= 1.0, "down waited for the program, not only for the pane"
    assert seatmod.process_start(pid) is None, "the program had ended before down said so"
    assert w.reseeded == [["captain"]], "reseeded once, after the program ended, while its launch was still named"
    assert w.seat.launched() is None and w.seat.retiring() is None


def test_a_program_that_outlasts_the_bound_is_reported_and_retired_by_the_tick(lingering, monkeypatch):
    monkeypatch.setattr(seatmod, "PROCESS_END_SECONDS", 0.3)
    w = lingering(2.5)
    w.seat.up(resume=False)
    launched = w.seat.launched()
    proof = w.seat.down()
    assert proof.endswith("closed · process still ending after 0.3s; credential and launch left for the next tick"), proof
    assert w.reseeded == [] and w.seat.launched() == launched and w.seat.retiring()["pid"] == launched.pid
    with pytest.raises(shipmod.ShipError, match="still ending"):
        w.seat.up(resume=False)
    monkeypatch.setattr(service, "seats", lambda *args: [w.seat])
    svc = service.Service(w.ship, w.seat.tmux)
    svc.credentials()
    assert w.reseeded == [[]], "while the program lives nothing of that seat is reseeded"
    assert w.seat.retiring() is not None and w.seat.launched() == launched
    assert wait_for(lambda: seatmod.process_start(launched.pid) is None, 10)
    svc.credentials()
    assert w.seat.retiring() is None and w.seat.launched() is None, "the tick finished the retirement"
    assert w.reseeded == [[], ["captain"], []], "reseeded once, by the tick, after the program had ended"
    svc.ledger.close()


def test_a_program_already_gone_reads_ended_at_once(lingering):
    w = lingering(0)
    w.seat.up(resume=False)
    started = time.monotonic()
    assert w.seat.down().endswith("closed · process ended")
    assert time.monotonic() - started < seatmod.PROCESS_END_SECONDS
