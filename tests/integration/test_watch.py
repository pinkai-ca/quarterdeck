"""The Watch on the example ship: activity, door, the table, the line, the saved screen."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from quarterdeck import cli, post, seat as seatmod, ship as shipmod, watch
from quarterdeck.tmux import Tmux

from .test_seat import wait_for


@pytest.fixture
def ship(drydock: Path, tmux: Tmux, monkeypatch) -> shipmod.Ship:
    text = (drydock / "ship.toml").read_text()
    text += '\n[seats.captain.settings]\nsteps = "3"\nstep_seconds = "0.8"\n\n[seats.worker-1.settings]\ndialog = "unknown"\n'
    (drydock / "ship.toml").write_text(text)
    monkeypatch.setenv("QD_TMUX_SOCKET", tmux.socket)
    monkeypatch.setenv("QD_SHIP", str(drydock))
    return shipmod.load(drydock)


def qd(capsys, *argv: str) -> str:
    assert cli.main(list(argv)) == 0
    return capsys.readouterr().out.rstrip("\n")


def test_activity_and_door_from_record_and_screen(ship: shipmod.Ship, tmux: Tmux):
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    assert str(watch.activity(captain)) == "off, down"
    captain.up()
    wait_for(lambda: watch.reading(captain).door.open)
    assert str(watch.activity(captain)) == "idle, no record yet", "a seat that has never moved is idle, not stalled"
    tmux.paste(captain.pane().id, "work")
    tmux.keys(captain.pane().id, "Enter")
    wait_for(lambda: watch.activity(captain).state == "working")
    act = wait_for(lambda: a if (a := watch.activity(captain)).detail.startswith("in Read") else None)
    assert act.state == "working" and act.detail.startswith("in Read ")  # law: record-first
    later = datetime.now(timezone.utc) + timedelta(seconds=watch.STALL_SECONDS + 1)
    assert watch.activity(captain, now=later).state == "stalled"
    wait_for(lambda: watch.activity(captain).state == "idle", seconds=15)
    assert watch.activity(captain) == watch.Activity("idle")
    r = watch.reading(captain)
    assert r.today.known > 0 and r.today.unmeasured == 0 and r.model == "fake-1" and r.pool == "fake" and r.harness == "fake fake"
    captain.down()
    tmux.new_session("Bridge") if not tmux.has_session("Bridge") else None
    pane = tmux.new_window("Bridge", "Pike", ship.folder, ["sh", "-c", "exit 7"])
    (ship.runtime / "seats" / "captain" / "launch.json").write_text(
        '{"pane": "%s", "pid": %d, "at": "2026-11-07T00:00:00+00:00", "pool": "fake", "resume": null, "argv": [], "version": "fake"}'
        % (pane.id, pane.pid))
    wait_for(lambda: tmux.pane(pane.id) and tmux.pane(pane.id).dead)
    assert str(watch.activity(captain)) == "off, exited 7"


def test_the_table_and_the_line(ship: shipmod.Ship, tmux: Tmux, capsys):
    seatmod.bring_up(ship, tmux)
    worker = seatmod.Seat(ship, ship.seat("worker-1"), tmux)
    wait_for(lambda: "unknown dialog" in watch.reading(worker).door.reason)
    assert watch.reading(worker).needs_human
    store = post.Store(ship.runtime)
    order = store.new("order", "desk", "worker-1", "waits behind the dialog")
    store.new("question", "operator", "desk", "no watch armed")
    rows = qd(capsys, "status").splitlines()
    assert rows[0].startswith("service down")
    table = {row.split()[0]: row for row in rows[2:5]}
    assert "idle" in table["captain"] and "open" in table["captain"]
    assert "obstructed: unknown dialog: Something else, needs a human" in table["worker-1"] and order.short in table["worker-1"]
    assert "obstructed: a human's session, its watch delivers" in table["desk"]
    assert rows[5] == "open orders: 1" and rows[6].startswith(f"  {order.short} desk → worker-1")
    line = qd(capsys, "status", "--line")
    assert line.startswith("SERVICE DOWN · 2 waiting, oldest 0m · worker-1 needs a human · desk: no watch armed")  # law: nothing-waits-silently
    facts = watch.facts(ship, tmux)
    assert facts.needs_human == ["worker-1"] and facts.unarmed == ["desk"] and facts.blind == 0
    full = qd(capsys, "watch", "worker-1")
    assert full.startswith("worker-1: idle, no record yet · door obstructed: unknown dialog") and "notice: unknown: Something else" in full
    saved = qd(capsys, "watch", "worker-1", "--save-screen")
    path = Path(saved.split("screen saved to ")[1])
    assert path.parent == ship.runtime / "screens" and "┌ Something else" in path.read_text()
