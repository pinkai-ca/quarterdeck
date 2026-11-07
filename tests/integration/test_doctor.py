"""qd doctor --fix on the drydock: the failures handed to a throwaway seat, its report awaited, nothing left behind."""

from __future__ import annotations

from pathlib import Path

from quarterdeck import doctor, post
from quarterdeck.drill import Check
from quarterdeck.ship import load
from quarterdeck.tmux import Tmux


def quick(drydock: Path):
    (drydock / "ship.toml").write_text((drydock / "ship.toml").read_text() + '\n[seats.captain.settings]\nstep_seconds = "0.05"\n')
    return load(drydock)


def test_the_failures_go_to_a_throwaway_seat_as_its_first_message(drydock: Path, tmux: Tmux, monkeypatch):
    ship = quick(drydock)
    monkeypatch.setattr(doctor, "PATIENCE", 2.0)
    found = doctor.fix(ship, [Check("seat captain", False, "working directory /nowhere does not exist")], tmux)
    assert found[0].name == "seat opened" and found[0].passed and " up in %" in found[0].detail and "McCoy" not in found[0].detail
    assert found[1].name == "seat reported" and not found[1].passed and found[1].detail == "no report within 2s"
    store = post.Store(ship.runtime)
    orders = [m for m in store.all() if m.kind == "order" and m.subject == "qd doctor"]
    assert len(orders) == 1 and orders[0].to.startswith("doctor-") and orders[0].sender == post.SHIP
    assert "- seat captain: working directory /nowhere does not exist" in orders[0].body and "report on this order" in orders[0].body
    assert not store.is_open(orders[0]), "the doctor closes what it opened"
    assert tmux.sessions() == [] and not (ship.runtime / "seats" / orders[0].to).exists(), "nothing of the throwaway seat remains"


def test_the_seats_report_is_what_the_doctor_waits_for(drydock: Path, tmux: Tmux, monkeypatch):
    ship = quick(drydock)
    store = post.Store(ship.runtime)

    def the_seat_reports(predicate, seconds):
        for order in store.all():
            if order.kind == "order" and order.subject == "qd doctor" and store.is_open(order):
                store.new("report", order.to, post.SHIP, "made the folder; nothing else was missing", re=order.id)
        return predicate()

    monkeypatch.setattr(doctor, "wait_for", the_seat_reports)
    found = doctor.fix(ship, [Check("seat captain", False, "working directory /nowhere does not exist")], tmux)
    assert found[1].passed and found[1].detail == "made the folder; nothing else was missing"
    assert tmux.sessions() == []
