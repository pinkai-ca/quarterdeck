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


def test_a_restart_whose_program_ends_past_the_first_bound_comes_back_up(lingering, monkeypatch):
    monkeypatch.setattr(seatmod, "PROCESS_END_SECONDS", 0.3)
    monkeypatch.setattr(seatmod, "RELAUNCH_END_SECONDS", 6.0)
    w = lingering(1.5)
    w.seat.up(resume=False)
    old = w.seat.launched()
    proof = w.seat.restart()
    assert "still ending after 0.3s, ended after" in proof and "the relaunch follows" in proof, proof
    assert w.seat.is_up and w.seat.launched().pid != old.pid and w.seat.retiring() is None
    assert w.seat.relaunch_refused() is None


def test_a_restart_whose_program_outlasts_the_longer_bound_is_refused_and_named_until_up(lingering, monkeypatch):
    from quarterdeck import watch
    monkeypatch.setattr(seatmod, "PROCESS_END_SECONDS", 0.2)
    monkeypatch.setattr(seatmod, "RELAUNCH_END_SECONDS", 0.6)
    w = lingering(3)
    w.seat.up(resume=False)
    old = w.seat.launched()
    with pytest.raises(shipmod.ShipError, match="still ending"):
        w.seat.restart()
    assert not w.seat.is_up and w.reseeded == []
    line = watch.facts(w.ship, w.seat.tmux).line()
    assert "captain: its program is still ending; its relaunch was refused" in line, line
    monkeypatch.setattr(service, "seats", lambda *args: [w.seat])
    svc = service.Service(w.ship, w.seat.tmux)
    assert wait_for(lambda: seatmod.process_start(old.pid) is None, 10)
    svc.credentials()  # the tick retires the ended program; it never relaunches the seat
    assert w.seat.retiring() is None and not w.seat.is_up
    line = watch.facts(w.ship, w.seat.tmux).line()
    assert "captain: its relaunch was refused while its program was ending; qd up captain" in line, line
    w.seat.up(resume=False)
    assert w.seat.relaunch_refused() is None and "relaunch was refused" not in watch.facts(w.ship, w.seat.tmux).line()
    svc.ledger.close()


def test_an_unreadable_retiring_marker_counts_as_still_ending(lingering):
    w = lingering(0)
    w.seat.up(resume=False)
    (w.seat.folder / seatmod.RETIRING).write_text("{not json")
    assert w.seat.relaunch_refused().startswith("captain: its program is still ending")
    assert w.seat.finish_retirement() is None
