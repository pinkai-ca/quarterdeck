"""Captured startup screens hold the door without changing record-first activity."""

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from quarterdeck import post, service, ship as shipmod, watch
from quarterdeck.door import Door
from quarterdeck.harness.base import Join, State
from quarterdeck.seat import Launched, Seat
from tests.conftest import FIXTURES


@pytest.fixture(params=[("pi", "0.74.2", "startup-blank", "fresh"),
                       ("opencode", "1.19.5", "startup-blank", "fresh"),
                       ("codex", "0.166.0", "startup-logo", "startup-ready"),
                       ("codex", "0.166.0", "startup-default", "startup-ready")])
def starting(request, drydock, monkeypatch):
    program, version, initial, ready = request.param
    folder = FIXTURES / program / version
    shown = [(folder / f"{initial}.txt").read_text()]
    ship = shipmod.load(drydock)
    spec = replace(ship.seat("worker-1"), harness=program)
    ship.seats[spec.name] = spec
    pane = SimpleNamespace(id="%1", pid=123, dead=False, width=320, in_mode=False)

    def forbidden(*args, **kwargs):
        pytest.fail("no paste, submit, interrupt or clearing key during startup/refusal")

    tmux = SimpleNamespace(capture=lambda *args, **kwargs: shown[0], pane=lambda _: pane,
                           keys=forbidden, paste=forbidden)
    seat = Seat(ship, spec, tmux)
    monkeypatch.setattr(seat, "pane", lambda: pane)
    monkeypatch.setattr(seat, "join", lambda: Join(None, "no record yet"))
    monkeypatch.setattr(seat, "record", lambda: None)
    monkeypatch.setattr(seat, "launched", lambda: Launched("%1", 123, datetime.now(timezone.utc).isoformat(),
                                                         spec.pools[0], None, [program], version, program))
    monkeypatch.setattr(watch, "seats", lambda *_: [seat])
    monkeypatch.setattr(service, "seats", lambda *_: [seat])
    svc = service.Service(ship, tmux)
    try:
        yield SimpleNamespace(seat=seat, shown=shown, svc=svc, ready=(folder / f"{ready}.txt").read_text())
    finally:
        svc.ledger.close()


def test_startup_waits_without_a_human_alarm_and_ready_releases_the_door(starting):
    w = starting
    door = Door(w.seat)
    read = door.reading()
    assert not read.open and read.clears == "time" and "starting" in read.reason
    for speed in (None, "now", "interrupt"):
        assert door.deliver("Do the work", speed=speed).result == "withheld"  # law: one-door
    order = w.svc.store.new("order", "operator", w.seat.name, "Do the work")
    for _ in range(2):
        w.svc.deliveries()
    assert w.svc.attempts.all() == [], "a drawing hold does not create a transport attempt or a receipt"
    assert w.svc.store.all() == [order], "no false needs-human report to the desk"
    row = watch.reading(w.seat, w.svc.store)
    assert not row.needs_human and "starting" in str(row.door)
    assert row.activity.state == "idle", "screen startup does not replace record-first activity"
    facts = watch.facts(w.seat.ship, w.seat.tmux, walls=[], login=[])
    assert facts.needs_human == [] and "needs a human" not in facts.line() and facts.waiting == 1
    assert "starting" in "\n".join(watch.table(w.seat.ship, w.seat.tmux))
    w.shown[0] = w.ready
    assert door.reading().open and w.svc._due(order, w.seat)
    assert w.svc.store.is_open(order), "drawing or readiness never closes an order"


def test_unknown_nonblank_screen_still_requires_a_human(starting):
    w = starting
    w.shown[0] = "An unfamiliar loading dialog\n⠋ ⠙ ⠹\n"
    read = Door(w.seat).reading()
    assert not read.open and read.clears == "needs a human"
    assert Door(w.seat).deliver("Do the work").result == "withheld"
    assert watch.reading(w.seat).needs_human
    assert w.seat.name in watch.facts(w.seat.ship, w.seat.tmux, walls=[], login=[]).needs_human
    w.svc.needs_human(w.seat)
    assert len(w.svc.store.all()) == 1 and "obstructed" in w.svc.store.all()[0].body


def test_startup_screen_never_overrides_readable_or_unknown_record_state(starting, monkeypatch, tmp_path):
    w = starting
    record = tmp_path / "synthetic.record"
    monkeypatch.setattr(w.seat, "join", lambda: Join(record))
    monkeypatch.setattr(w.seat.harness, "state", lambda _: State(turn_open=True, last_write=datetime.now(timezone.utc)))
    assert watch.activity(w.seat).state == "working"  # law: record-first
    monkeypatch.setattr(w.seat.harness, "state", lambda _: State(turn_open=None))
    assert watch.activity(w.seat).state == "unknown"
    assert Door(w.seat).reading().clears == "time"


def test_text_in_a_ready_box_remains_an_untouched_human_obstruction(starting):
    w = starting
    draft = "a human draft ⠋ ⠙ ⠹"
    lines = w.ready.splitlines()
    program = w.seat.harness.name
    if program == "pi":
        rules = [i for i, line in enumerate(lines) if line.startswith("─" * 20)]
        lines[rules[-2] + 1] = draft
    elif program == "opencode":
        index = next(i for i, line in enumerate(lines) if "┃  Ask anything" in line)
        lines[index] = lines[index].split("┃")[0] + "┃  " + draft
    else:
        index = lines.index("› Ask Codex to do anything")
        lines[index] = "› " + draft
    raw = "\n".join(lines) + "\n"
    w.shown[0] = raw
    screen = w.seat.harness.screen(raw)
    assert not screen.starting and screen.box.strip() == draft
    door = Door(w.seat)
    assert door.reading().reason == "text in the box" and door.reading().clears == "needs a human"
    assert door.deliver("Do the work").result == "withheld"
    assert w.shown[0] == raw
