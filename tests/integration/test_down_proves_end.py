"""Down and restart observe the fake program outliving its pane on a private tmux server."""

from __future__ import annotations

import os
from pathlib import Path
import select
import signal
from types import SimpleNamespace

import pytest

from quarterdeck import credential, seat as seatmod, service, ship as shipmod

from .waiting import start_with_conversation, wait_for


GATED_PROGRAM = """
import os, signal, sys, time
from pathlib import Path
from quarterdeck.fakeharness import main

waiting, release = map(Path, sys.argv[1:3])

def hangup(*_):
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    waiting.touch()
    while not release.exists():
        time.sleep(.01)
    os._exit(0)

signal.signal(signal.SIGHUP, hangup)
sys.exit(main(sys.argv[3:]))
"""


@pytest.fixture
def lingering(drydock: Path, tmux, monkeypatch, tmp_path):
    ship = shipmod.load(drydock)
    seat = seatmod.Seat(ship, ship.seat("captain"), tmux)
    waiting, release = tmp_path / "waiting", tmp_path / "release"
    reseeded = []
    descriptor = None
    open_window = seat._open_window

    def open_gated(argv, env, path):
        nonlocal descriptor
        if descriptor is not None:
            return open_window(argv, env, path)
        assert argv[1:3] == ["-m", "quarterdeck.fakeharness"]
        pane = open_window([argv[0], "-c", GATED_PROGRAM, str(waiting), str(release), *argv[3:]], env, path)
        descriptor = os.pidfd_open(pane.pid)
        return pane

    def hangup():
        signal.pidfd_send_signal(descriptor, signal.SIGHUP)

    def allow_exit():
        release.touch()
        assert select.select([descriptor], [], [], 10)[0], "the gated program did not exit"

    def reseed(_ship, seats, **bounds):
        reseeded.append([s.name for s in seats])
        if seats:
            assert select.select([descriptor], [], [], 0)[0], "reseed ran before the program exited"
            assert seats[0].launched() == original, "reseed ran after the launch was removed"
        return []

    original = None

    def start(*, conversation=False):
        nonlocal original
        if conversation:
            start_with_conversation(seat)
        else:
            seat.up(resume=False)
        original = seat.launched()
        assert seatmod.process_alive(original.pid, original.start)
        return original

    monkeypatch.setattr(seat, "_open_window", open_gated)
    monkeypatch.setattr(credential, "reseed", reseed)
    try:
        yield SimpleNamespace(ship=ship, seat=seat, reseeded=reseeded, waiting=waiting,
                              start=start, hangup=hangup, allow_exit=allow_exit)
    finally:
        release.touch()
        if descriptor is not None:
            try:
                if not select.select([descriptor], [], [], 0)[0]:
                    try:
                        hangup()  # also end a launch whose pane a failed test never closed
                    except ProcessLookupError:
                        pass
                allow_exit()
            finally:
                os.close(descriptor)


def observe_waits(monkeypatch, world, launched, *, release_on=None):
    """Advance the deadline in the observer only. The fake program and its exit are real."""
    observed = []
    real_wait = seatmod.wait_for_end

    def wait(pid, start, seconds):
        assert (pid, start) == (launched.pid, launched.start)
        assert world.seat.tmux.pane(launched.pane) is None, "down closes the pane before waiting"
        wait_for(world.waiting.exists)
        assert seatmod.process_alive(pid, start), "the program outlived its pane"
        assert world.seat.launched() == launched and world.seat.retiring()["pid"] == pid
        assert not world.reseeded
        now = 0.0

        def sleep(step):
            nonlocal now
            if len(observed) + 1 == release_on:
                world.allow_exit()  # reached only after the real observer has seen the process alive
            now += step

        with monkeypatch.context() as clock:
            clock.setattr(seatmod, "time", SimpleNamespace(monotonic=lambda: now, sleep=sleep))
            ended = real_wait(pid, start, seconds)
        observed.append((seconds, ended))
        return ended

    monkeypatch.setattr(seatmod, "wait_for_end", wait)
    return observed


def test_down_waits_for_a_lingering_program_then_reseeds_and_removes_the_launch(lingering, monkeypatch):
    w = lingering
    launched = w.start()
    observed = observe_waits(monkeypatch, w, launched, release_on=1)
    proof = w.seat.down()
    assert proof.endswith("closed · process ended"), proof
    assert observed == [(seatmod.PROCESS_END_SECONDS, True)]
    assert seatmod.process_start(launched.pid) is None, "the program had ended before down said so"
    assert w.reseeded == [["captain"]]
    assert w.seat.launched() is None and w.seat.retiring() is None


def test_a_program_that_outlasts_the_bound_is_reported_and_retired_by_the_tick(lingering, monkeypatch):
    monkeypatch.setattr(seatmod, "PROCESS_END_SECONDS", 0.3)
    w = lingering
    launched = w.start()
    observed = observe_waits(monkeypatch, w, launched)
    proof = w.seat.down()
    assert proof.endswith("closed · process still ending after 0.3s; credential and launch left for the next tick"), proof
    assert observed == [(0.3, False)]
    assert w.reseeded == [] and w.seat.launched() == launched and w.seat.retiring()["pid"] == launched.pid
    with pytest.raises(shipmod.ShipError, match="still ending"):
        w.seat.up(resume=False)
    monkeypatch.setattr(service, "seats", lambda *args: [w.seat])
    svc = service.Service(w.ship, w.seat.tmux)
    try:
        svc.credentials()
        assert w.reseeded == [[]], "while the program lives nothing of that seat is reseeded"
        assert w.seat.retiring() is not None and w.seat.launched() == launched
        assert seatmod.process_alive(launched.pid, launched.start)
        w.allow_exit()
        svc.credentials()
        assert w.seat.retiring() is None and w.seat.launched() is None, "the tick finished the retirement"
        assert w.reseeded == [[], ["captain"], []], "reseeded once, by the tick, after the program had ended"
    finally:
        svc.ledger.close()


def test_a_program_already_gone_reads_ended_at_once(lingering, monkeypatch):
    w = lingering
    launched = w.start()
    w.hangup()
    w.allow_exit()
    wait_for(lambda: (pane := w.seat.tmux.pane(launched.pane)) is not None and pane.dead)
    monkeypatch.setattr(seatmod, "wait_for_end", lambda *_: pytest.fail("down waited for an already exited program"))
    assert w.seat.down().endswith("closed · process ended")
    assert w.reseeded == [["captain"]]
    assert w.seat.launched() is None and w.seat.retiring() is None


def test_a_restart_whose_program_ends_past_the_first_bound_comes_back_up(lingering, monkeypatch):
    monkeypatch.setattr(seatmod, "PROCESS_END_SECONDS", 0.3)
    monkeypatch.setattr(seatmod, "RELAUNCH_END_SECONDS", 6.0)
    w = lingering
    old = w.start(conversation=True)
    observed = observe_waits(monkeypatch, w, old, release_on=2)
    proof = w.seat.restart()
    assert "still ending after 0.3s, ended after" in proof and "the relaunch follows" in proof, proof
    assert observed == [(0.3, False), (5.7, True)]
    assert w.seat.is_up and w.seat.launched().pid != old.pid and w.seat.retiring() is None
    assert w.seat.relaunch_refused() is None


def test_a_restart_whose_program_outlasts_the_longer_bound_is_refused_and_named_until_up(lingering, monkeypatch):
    from quarterdeck import watch
    monkeypatch.setattr(seatmod, "PROCESS_END_SECONDS", 0.2)
    monkeypatch.setattr(seatmod, "RELAUNCH_END_SECONDS", 0.6)
    w = lingering
    old = w.start(conversation=True)
    observed = observe_waits(monkeypatch, w, old)
    with pytest.raises(shipmod.ShipError, match="still ending"):
        w.seat.restart()
    assert observed == [(0.2, False), (seatmod.RELAUNCH_END_SECONDS - seatmod.PROCESS_END_SECONDS, False)]
    assert not w.seat.is_up and w.reseeded == []
    line = watch.facts(w.ship, w.seat.tmux).line()
    assert "captain: its program is still ending; its relaunch was refused" in line, line
    assert seatmod.process_alive(old.pid, old.start)
    monkeypatch.setattr(service, "seats", lambda *args: [w.seat])
    svc = service.Service(w.ship, w.seat.tmux)
    try:
        w.allow_exit()
        svc.credentials()  # the tick retires the ended program; it never relaunches the seat
        assert w.seat.retiring() is None and not w.seat.is_up
        line = watch.facts(w.ship, w.seat.tmux).line()
        assert "captain: its relaunch was refused while its program was ending; qd up captain" in line, line
        w.seat.up(resume=False)
        assert w.seat.relaunch_refused() is None and "relaunch was refused" not in watch.facts(w.ship, w.seat.tmux).line()
    finally:
        svc.ledger.close()


def test_an_unreadable_retiring_marker_counts_as_still_ending(lingering):
    w = lingering
    w.start()
    (w.seat.trusted / seatmod.RETIRING).write_text("{not json")
    assert w.seat.relaunch_refused().startswith("captain: its program is still ending")
    assert w.seat.finish_retirement() is None
