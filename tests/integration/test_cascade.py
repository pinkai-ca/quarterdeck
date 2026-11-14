"""Private drydock proof: ordinary posts, fake programs and confirmed effort controls."""

from __future__ import annotations

import shutil
import os
import signal
from pathlib import Path

import pytest

from quarterdeck import cli, post, seat as seatmod, service, ship as shipmod
from quarterdeck.harness.synthetic_control import Rendezvous

from .progress import service_progress
from .waiting import wait_for
from tests.conftest import TEST_TICK

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def ring(drydock, tmux, monkeypatch):
    text = f'''[ship]
code = "checkout"
name = "synthetic-ring"
operator = "operator"
tick = {TEST_TICK}
[cascade]
seats = ["alpha", "beta", "gamma"]
orchestrator = "conductor"
[pools.fake]
provider = "synthetic"
[sessions.crew]
title = "Synthetic ring"
'''
    for name, role in (("alpha", "ring"), ("beta", "ring"), ("gamma", "ring"), ("conductor", "orchestrator")):
        text += f'''
[seats.{name}]
session = "crew"
role = "{role}"
harness = "fake"
pool = "fake"
model = "fake-1"
effort = "low"
[seats.{name}.settings]
step_seconds = "0.01"
'''
    (drydock / "ship.toml").write_text(text)
    shutil.copyfile(ROOT / "ships" / "cascade" / "orders.md", drydock / "orders.md")
    shutil.copytree(ROOT / "ships" / "cascade" / "roles", drydock / "roles", dirs_exist_ok=True)
    monkeypatch.setenv("QD_TMUX_SOCKET", tmux.socket)
    monkeypatch.setenv("QD_SHIP", str(drydock))
    monkeypatch.delenv("QD_SEAT", raising=False)
    tmux.new_session("Synthetic bootstrap")
    tmux.kill_session("Synthetic bootstrap")
    ship = shipmod.load(drydock)
    yield ship
    service.stop(ship.runtime)


def qd(capsys, *args):
    assert cli.main(list(args)) == 0
    captured = capsys.readouterr()
    assert not captured.err
    text = captured.out
    if args and args[0] == "post" and "resend" not in args:
        from .waiting import await_drop
        await_drop(text.strip())
    return text


def active_order(ship, store):
    orders = [message for message in store.all() if message.kind == "order" and message.sender == "ship"
              and store.is_open(message)]
    return orders[-1] if orders else None


def idle_after_delivery(ship, tmux, order):
    seat = seatmod.Seat(ship, ship.seat(order.to), tmux)
    attempts = post.Attempts(ship.runtime).for_message(order.id)
    if not any(attempt.result == "delivered" for attempt in attempts):
        return False
    record = seat.record()
    state = seat.harness.state(record) if record else None
    return state is not None and state.turn_open is False


def test_fake_ring_relays_whole_reports_grows_same_file_changes_effort_and_notifies_once(ring, tmux, capsys):
    qd(capsys, "up")
    qd(capsys, "post", "question", "--to", "conductor", "Plan a small garden.")
    store = post.Store(ring.runtime)
    question = store.all()[0]
    started = qd(capsys, "cascade", "--re", question.short)
    notes = ring.runtime / "ring" / question.id / "responses.md"
    assert str(notes) in started and "tail -f" in started
    previous, sizes, inode = question.body, [], None
    alpha = seatmod.Seat(ring, ring.seat("alpha"), tmux)
    # Lap 4 (alpha's second) is held inside its turn: only its order carries "Revision 3.".
    held = Rendezvous(alpha.home, "step", "Revision 3.")
    with held:
        for index in range(9):
            order = wait_for(lambda: active_order(ring, store), seconds=15)
            assert order.to == ("alpha", "beta", "gamma")[index % 3]
            assert order.body.endswith(previous)
            assert not [m for m in store.for_seat("conductor") if m.sender == "ship"]
            report = f"## Cascade note\nRevision {index + 1}.\n## Answer\nWhole plan {index + 1}."
            if index == 3:
                # A restart in the middle of a lap: alpha is mid-turn on this lap's order throughout.
                assert wait_for(held.reached, seconds=15)
                assert alpha.harness.state(alpha.record()).turn_open is True
                assert "service stopped" in service.stop(ring.runtime)
                assert "service started" in service.start(ring)
                assert "running" in qd(capsys, "cascade", "--re", question.id)
                assert current_id(store) == order.id, "restart keeps the same pending order"
                # The ring seat files its report before its turn ends, as a ring seat does.
                qd(capsys, "post", "report", "--from", order.to, "--re", order.id, report)
                held.release()
            wait_for(lambda: idle_after_delivery(ring, tmux, order), seconds=15)
            seat = seatmod.Seat(ring, ring.seat(order.to), tmux)
            expected = ("low", "medium", "high")[index // 3]
            assert wait_for(lambda: seat.harness.screen(tmux.capture(seat.pane().id)).shows["effort"] == expected,
                            seconds=15)
            previous = report
            if index != 3:
                # The fake has consumed this exact order. File its ordinary synthetic closing report.
                qd(capsys, "post", "report", "--from", order.to, "--re", order.id, previous)
            wait_for(lambda: notes.exists() and f"Revision {index + 1}." in notes.read_text(), seconds=15)
            sizes.append(notes.stat().st_size)
            inode = inode or notes.stat().st_ino
            assert notes.stat().st_ino == inode, "tail -f follows the same append-only file"
    final = wait_for(lambda: [m for m in store.for_seat("conductor") if m.sender == "ship"], seconds=15)
    assert len(final) == 1 and final[0].body.endswith(previous)
    assert str(notes) in final[0].body
    assert store.is_open(question)
    service_progress(ring)
    assert len([m for m in store.for_seat("conductor") if m.sender == "ship"]) == 1
    assert sizes == sorted(set(sizes))
    content = notes.read_text()
    assert all(content.count(f"Revision {index}.") == 1 for index in range(1, 10))
    assert "Requested effort: medium; actual: medium" in content
    assert "Requested effort: high; actual: high" in content
    qd(capsys, "post", "answer", "--from", "conductor", "--re", question.id, "The final garden plan.")
    assert not store.is_open(question)


def test_fake_ring_dead_owned_pane_stops_early_with_one_final(ring, tmux, capsys):
    qd(capsys, "up")
    qd(capsys, "post", "question", "--to", "conductor", "A short plan.")
    store = post.Store(ring.runtime)
    question = store.all()[0]
    qd(capsys, "cascade", "--re", question.id, "--efforts", "low")
    order = wait_for(lambda: active_order(ring, store), seconds=15)
    wait_for(lambda: idle_after_delivery(ring, tmux, order), seconds=15)
    seat = seatmod.Seat(ring, ring.seat("alpha"), tmux)
    pane = wait_for(seat.pane, seconds=15)
    os.kill(pane.pid, signal.SIGTERM)  # failed fake process leaves its owned pane visibly dead
    finals = wait_for(lambda: [m for m in store.for_seat("conductor") if m.sender == "ship"], seconds=15)
    assert len(finals) == 1 and "alpha: owned pane is dead" in finals[0].body
    service_progress(ring)
    assert len([m for m in store.for_seat("conductor") if m.sender == "ship"]) == 1
    assert not [m for m in store.all() if m.kind == "order" and m.to in ("beta", "gamma")]
    assert store.is_open(order) and store.is_open(question)


def current_id(store):
    return [m.id for m in store.all() if m.kind == "order" and store.is_open(m)][-1]
