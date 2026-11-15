"""qd check takes its throwaway seat down on every way out; a seat a killed check left is named until qd down takes it.

Synthetic ship, fake harness, a stand-in tmux; the "program" is a short-lived child process of the test.
"""

import argparse
import signal
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from quarterdeck import check, check_outcome, cli, pending, post, seat as seatmod, watch
from quarterdeck.drill import Check
from quarterdeck.harness.fake import FakeHarness
from quarterdeck.seat import Launched
from quarterdeck.servicectl import process_start
from quarterdeck.ship import ShipError
from .test_check_lifetime import synthetic_ship


class Server:
    """The ship's tmux server as a check sees it: sessions, and one pane per seat whose process the test owns."""

    def __init__(self):
        self.sessions, self.panes, self.programs = {}, {}, {}
        self.legacy, self.socket = False, None

    # sessions
    def has_session(self, name):
        return name in self.sessions

    def sessions_list(self):
        return list(self.sessions)

    def new_session(self, name, **kwargs):
        assert name not in self.sessions
        self.sessions[name] = f"${len(self.sessions) + 1}"
        return self.sessions[name]

    def kill_session_id(self, ident):
        for name in [n for n, value in self.sessions.items() if value == ident]:
            self.end(name)

    def kill_session(self, name):
        self.end(name)

    def end(self, name):
        del self.sessions[name]
        for pane, (session, _) in list(self.panes.items()):
            if session == name:
                self.close(pane)

    def status_line(self, *args):
        pass

    # panes
    def start(self, session, pane, *, stubborn=False):
        program = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.panes[pane] = (session, program)
        self.programs[pane] = (program, stubborn)
        return program

    def pane(self, ident):
        from types import SimpleNamespace
        found = self.panes.get(ident)
        return SimpleNamespace(id=ident, pid=found[1].pid, dead=False, session=found[0]) if found else None

    def kill_window(self, pane_id, expected_pid=None):
        self.close(pane_id)

    def close(self, pane_id):
        session, program = self.panes.pop(pane_id)
        if not self.programs[pane_id][1]:  # a stubborn program outlives its pane, as one still writing does
            stop(program)

    def started(self):
        return None


def stop(program):
    if program.poll() is None:
        program.kill()
    program.wait(timeout=10)


def launch(seat, server, pane="%7", *, stubborn=False):
    """What qd up leaves: a pane in the seat's session running its program, and launch.json naming both."""
    seat.home.mkdir(parents=True, exist_ok=True)
    seat.trusted.mkdir(parents=True, exist_ok=True)
    program = server.start(seat.spec.session, pane, stubborn=stubborn)
    launched = Launched(pane, program.pid, (post.now() - timedelta(seconds=5)).isoformat(), seat.pool.name,
                        None, [seat.harness.name], "synthetic", harness=seat.harness.name,
                        start=process_start(program.pid))
    launched.write(seat.trusted / "launch.json")
    return program


@pytest.fixture
def world(tmp_path, monkeypatch):
    ship, server = synthetic_ship(tmp_path), Server()
    monkeypatch.setattr(FakeHarness, "version", lambda self: "synthetic")
    monkeypatch.setattr(check, "probe_wake", lambda seat, orders: [Check("human-session waking line", True, "stubbed")])
    monkeypatch.setattr(seatmod, "PROCESS_END_SECONDS", 2.0)
    yield ship, server
    for program, _ in server.programs.values():
        stop(program)


def no_seat_left(ship, server):
    assert server.sessions == {} and server.panes == {}
    assert not list((ship.runtime / "seats").glob("check-*")), "no check seat's home is left"
    assert all(program.poll() is not None for program, _ in server.programs.values()), "every program ended"
    assert seatmod.leftover_checks(ship) == []


STEPS = ["launch", "door", "paste", "task", "turn read", "model"]


@pytest.mark.parametrize("step", STEPS)
@pytest.mark.parametrize("how", ["fails", "raises"])
def test_a_check_failing_at_any_step_leaves_no_seat(world, monkeypatch, step, how):
    ship, server = world
    seen = {}

    def probe(checked_ship, seat, orders=None, **kwargs):
        seen["seat"] = seat
        launch(seat, server)
        if how == "raises":
            raise RuntimeError(f"synthetic failure at {step}")
        return [Check(name, name != step, "synthetic") for name in STEPS]

    monkeypatch.setattr(check, "probe", probe)
    if how == "raises":
        with pytest.raises(RuntimeError, match=step):
            check.check(ship, "fake", server)
    else:
        assert any(c.passed is False for c in check.check(ship, "fake", server))
    assert seen["seat"].name.startswith("check-fake-")
    no_seat_left(ship, server)


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGHUP])
def test_a_check_interrupted_by_a_signal_takes_its_seat_down_and_a_second_signal_waits_for_it(world, monkeypatch, number):
    ship, server = world
    real_down = seatmod.Seat.down
    order = []

    def probe(checked_ship, seat, orders=None, **kwargs):
        launch(seat, server)
        signal.raise_signal(number)  # as a terminal closing, or a tool's timeout, would send it
        pytest.fail("the signal ends the check where it stands")

    def down(seat):
        signal.raise_signal(signal.SIGHUP if number == signal.SIGTERM else signal.SIGTERM)  # held until teardown ends
        order.append("down")
        return real_down(seat)

    monkeypatch.setattr(check, "probe", probe)
    monkeypatch.setattr(seatmod.Seat, "down", down)
    with pytest.raises(check.CheckInterrupted) as raised:
        check.check(ship, "fake", server)
    assert isinstance(raised.value.__context__, check.CheckInterrupted), "the held signal acted once teardown ended"
    assert order == ["down"]
    no_seat_left(ship, server)
    events, _ = check_outcome.collect(ship.runtime)
    assert "interrupted" in str(events)


def leaked(ship, server, *, stubborn=False):
    """What a check killed outright leaves: its seat's home, launch, session and running program."""
    name = "check-fake-d93c"
    spec = seatmod.SeatSpec(name, "qd-" + name, "worker", "fake", ("fake",), cwd=ship.workspace)
    from dataclasses import replace
    from quarterdeck.ship import Session
    treated = replace(ship, sessions={**ship.sessions, spec.session: Session(spec.session, spec.session)},
                      seats={**ship.seats, name: spec})
    seat = seatmod.Seat(treated, spec, server)
    seat.folder.mkdir(parents=True)
    seat.trusted.mkdir(parents=True, exist_ok=True)
    server.new_session(spec.session)
    program = launch(seat, server, stubborn=stubborn)
    with pending.transient_lock(ship.runtime, name) as owned:
        assert owned  # its check is gone: the lock it held is free
    return name, program


def test_a_seat_a_killed_check_left_is_named_in_the_status_line_and_qd_down_removes_it(world, monkeypatch):
    ship, server = world
    name, program = leaked(ship, server)
    words = seatmod.leftover_checks(ship)
    assert words == [f"{name} outlived its check: its program still runs; qd down {name}"]
    facts = watch.facts(ship, server, walls=[], login=[])
    assert facts.leftover_checks == words and f"qd down {name}" in facts.line()
    table = watch.render_table({"service": "up", "facts": facts, "seats": [], "usage": [], "open_orders": [],
                                "requests": [], "programs": [], "programs_error": None,
                                "check_outcomes": [], "check_outcome_errors": []})
    assert table[1] == words[0], "qd status names it at the head of its table"
    from quarterdeck import headlines
    crowded = watch.Facts("up", 0, 0, 0, leftover_checks=words, pending=["synthetic " * 30] * 3)
    assert f"qd down {name}" in headlines.status_line(crowded, ["x" * 200]), "kept when the line overflows"

    monkeypatch.setattr(cli.Tmux, "for_ship", classmethod(lambda cls, ship: server))
    monkeypatch.setattr(cli, "load_ship", lambda args: ship)
    proof = cli.cmd_down(argparse.Namespace(ship=None, seat=name))
    assert proof.startswith(f"{name} down: ") and "process ended" in proof and "home removed" in proof
    assert program.poll() is not None
    no_seat_left(ship, server)
    assert not (ship.runtime / "post" / "transient" / f"{name}.check.json").exists()


def test_a_program_that_outlives_qd_down_keeps_its_home_and_its_name_until_it_has_ended(world, monkeypatch):
    ship, server = world
    # The stubborn program never ends while down watches it, so down's watch runs its whole window whatever its
    # length: shortened for this test alone. What is proved, still ending and the launch kept, does not depend on it.
    monkeypatch.setattr(seatmod, "PROCESS_END_SECONDS", 0.2)
    name, program = leaked(ship, server, stubborn=True)
    proof = seatmod.down_leftover_check(ship, server, name)
    assert "still ending" in proof and f"qd down {name} again" in proof
    assert (ship.runtime / "trusted" / name / "launch.json").exists(), "nothing proved its end: the launch stays"
    assert seatmod.leftover_checks(ship) == [f"{name} outlived its check: its program still runs; qd down {name}"]
    stop(program)
    assert seatmod.leftover_checks(ship) == [f"{name} outlived its check: its program has ended; qd down {name}"]
    assert "home removed" in seatmod.down_leftover_check(ship, server, name)
    no_seat_left(ship, server)


def test_a_running_checks_seat_is_neither_named_nor_taken_down(world, monkeypatch):
    ship, server = world
    seen = {}

    def probe(checked_ship, seat, orders=None, **kwargs):
        launch(seat, server)
        seen["line"] = seatmod.leftover_checks(ship)
        with pytest.raises(ShipError, match="its check is still running"):
            seatmod.down_leftover_check(ship, server, seat.name)
        return [Check("synthetic", True)]

    monkeypatch.setattr(check, "probe", probe)
    check.check(ship, "fake", server)
    assert seen["line"] == []
    no_seat_left(ship, server)


@pytest.mark.parametrize("name", ["check-fake-zzzz", "checker", "survivor-1234"])
def test_qd_down_takes_by_name_only_a_check_seat_left_behind(world, name):
    ship, server = world
    with pytest.raises(ShipError, match="no check's seat left behind"):
        seatmod.down_leftover_check(ship, server, name)
