"""The service as a process: heartbeat, deliveries, inboxes, requests, fresh after order, re-brief."""

from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path

import pytest

from quarterdeck import cli, post, seat as seatmod, service, ship as shipmod
from quarterdeck.tmux import Tmux

from .test_seat import wait_for


@pytest.fixture
def ship(example_ship: Path, tmux: Tmux, monkeypatch) -> shipmod.Ship:
    """The example ship with quick fakes, the CLI pointed at the private tmux server."""
    text = (example_ship / "ship.toml").read_text()
    text += '\n[seats.captain.settings]\nstep_seconds = "0.05"\n\n[seats.worker-1.settings]\nstep_seconds = "0.05"\n'
    (example_ship / "ship.toml").write_text(text)
    monkeypatch.setenv("QD_TMUX_SOCKET", tmux.socket)
    monkeypatch.setenv("QD_SHIP", str(example_ship))
    monkeypatch.delenv("QD_SEAT", raising=False)
    ship = shipmod.load(example_ship)
    yield ship
    service.stop(ship.runtime)


def qd(capsys, *argv: str) -> str:
    code = cli.main(list(argv))
    out = capsys.readouterr()
    assert code == 0, out.err
    return out.out.rstrip("\n")


def test_up_starts_the_service_and_the_service_delivers(ship: shipmod.Ship, tmux: Tmux, capsys):
    out = qd(capsys, "up")
    assert "service started · pid" in out and service.alive(ship.runtime)
    assert "service ok" in qd(capsys, "status").splitlines()[0]
    proof = qd(capsys, "post", "order", "--to", "captain", "--subject", "Stars", "Count the stars.\n$ echo counted")
    assert proof.endswith("· stored · the service types it into captain's pane")
    store = post.Store(ship.runtime)
    message = store.all()[0]
    assert message.sender == "operator"
    attempts = post.Attempts(ship.runtime)
    done = wait_for(lambda: [a for a in attempts.for_message(message.id) if a.result], seconds=15)
    assert done[0].result == "delivered" and done[0].pane.startswith("%")
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    wait_for(lambda: captain.record() and captain.harness.turns(captain.record()))
    assert "⎿ counted" in tmux.capture(captain.pane().id, history=100)
    inbox = wait_for(lambda: (ship.runtime / "post" / "inbox" / "captain.md").exists() and (ship.runtime / "post" / "inbox" / "captain.md").read_text())
    assert f"order {message.short} from operator" in inbox and "## Open (1)" in inbox
    # the addressee's report closes it, and the inbox follows
    qd(capsys, "post", "report", "--from", "captain", "--re", message.short, "counted: many")
    wait_for(lambda: "## Open (0)" in (ship.runtime / "post" / "inbox" / "captain.md").read_text())
    assert "service stopped" in qd(capsys, "down").splitlines()[-1] and not service.alive(ship.runtime)


def test_a_seat_in_a_human_session_is_reached_by_its_watch_only(ship: shipmod.Ship, tmux: Tmux, capsys):
    qd(capsys, "up", "desk")
    proof = qd(capsys, "post", "question", "--to", "desk", "Which branch?")
    assert proof.endswith("no watch armed on desk; waits in the inbox")
    time.sleep(2.5)
    desk = seatmod.Seat(ship, ship.seat("desk"), tmux)
    assert desk.harness.screen(tmux.capture(desk.pane().id)).box == "", "never typed into"  # law: one-door
    text, opened = post.wait(ship.runtime, "desk", interval=0.1, stop_after=3)
    assert text.startswith("qd > question from operator · que-") and "Which branch?" in text
    assert [a.result for a in opened] == [None], "open until printed"
    post.printed(ship.runtime, opened)
    assert post.Attempts(ship.runtime).for_message(post.Store(ship.runtime).all()[0].id)[0].result == "watch"
    assert post.wait(ship.runtime, "desk", interval=0.1, stop_after=0.3) is None, "nothing new, nothing printed"


def test_controls_go_through_the_service_by_slash_command(ship: shipmod.Ship, tmux: Tmux, capsys):
    qd(capsys, "up", "captain")
    lines = qd(capsys, "set", "captain", "model=fake-2", "pool=fake", "effort=low").splitlines()
    assert lines[0].startswith('ship.toml: seats.captain.model = "fake-2" · written')
    assert lines[1].startswith("  model = fake-2 · applied by /model fake-2 in %") and lines[1].endswith("confirmed by screen")
    assert lines[3] == "  pool written; qd restart captain applies it"
    assert lines[5].startswith("  effort = low · applied by /effort low in %")
    assert shipmod.load(ship.folder).seat("captain").model == "fake-2"
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    assert captain.harness.screen(tmux.capture(captain.pane().id)).shows == {"model": "fake-2", "effort": "low"}
    assert "· model set to fake-2" in tmux.capture(captain.pane().id, history=50), "the change stands in the pane"
    table = qd(capsys, "controls", "captain")
    assert "model      --model <value>        /model {value}     screen" in table


def test_stop_and_resend_are_requests_to_the_service(ship: shipmod.Ship, tmux: Tmux, capsys):
    text = (ship.folder / "ship.toml").read_text().replace('step_seconds = "0.05"', 'steps = "6"\nstep_seconds = "0.5"', 1)
    (ship.folder / "ship.toml").write_text(text)
    qd(capsys, "up", "captain")
    qd(capsys, "post", "order", "--to", "captain", "slow work")
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    wait_for(lambda: captain.harness.screen(tmux.capture(captain.pane().id)).working, seconds=15)
    assert qd(capsys, "stop", "captain").startswith("captain interrupted with Escape in %")
    wait_for(lambda: not captain.harness.screen(tmux.capture(captain.pane().id)).working)
    message = post.Store(ship.runtime).all()[0]
    proof = qd(capsys, "post", "resend", message.short)
    assert proof.startswith(f"{message.short} to captain · delivered") and proof.endswith("· resend")
    assert [a.result for a in post.Attempts(ship.runtime).for_message(message.id)] == ["delivered", "delivered"]


def test_a_worker_starts_fresh_after_its_order_closes(ship: shipmod.Ship, tmux: Tmux, capsys):
    qd(capsys, "up", "worker-1")
    worker = seatmod.Seat(ship, ship.seat("worker-1"), tmux)
    first_launch = worker.launched()
    # the fake follows the "$ " line, which posts the closing report from inside the seat (law: can-post)
    qd(capsys, "post", "order", "--to", "worker-1", "Do it and report.\n$ qd post report --re {this} done")
    wait_for(lambda: not post.Store(ship.runtime).open_for("worker-1"), seconds=20)
    report = post.Store(ship.runtime).all()[-1]
    assert report.kind == "report" and report.sender == "worker-1" and report.to == "operator"
    wait_for(lambda: worker.launched() and worker.launched().at != first_launch.at, seconds=20)  # law: context-is-cache
    assert worker.launched().resume is None, "fresh: a new conversation"
    log = (ship.runtime / "log" / "service.jsonl").read_text()
    assert '"event": "fresh"' in log


def test_changed_orders_rebrief_every_seat_with_a_message_from_ship(ship: shipmod.Ship, tmux: Tmux, capsys):
    qd(capsys, "up", "captain")
    time.sleep(1.2)
    (ship.folder / "orders.md").write_text("# Ship's orders: example\n\nNew orders.\n")
    wait_for(lambda: "New orders." in (ship.runtime / "brief" / "captain.md").read_text(), seconds=10)
    notice = wait_for(lambda: [m for m in post.Store(ship.runtime).all() if m.sender == "ship"], seconds=10)[0]
    assert notice.to == "captain" and notice.body == "ship's orders changed · read runtime/brief/captain.md"
    assert wait_for(lambda: [a for a in post.Attempts(ship.runtime).for_message(notice.id) if a.result], seconds=15)[0].result == "delivered"


def test_the_service_clears_a_known_notice_and_delivers_behind_it(ship: shipmod.Ship, tmux: Tmux, capsys):
    text = (ship.folder / "ship.toml").read_text().replace('[seats.captain.settings]\n', '[seats.captain.settings]\ndialog = "daybreak"\n')
    (ship.folder / "ship.toml").write_text(text)
    qd(capsys, "up", "captain")
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    wait_for(lambda: captain.harness.screen(tmux.capture(captain.pane().id)).interruption is not None)
    qd(capsys, "post", "order", "--to", "captain", "behind the notice")
    message = post.Store(ship.runtime).all()[0]
    done = wait_for(lambda: [a for a in post.Attempts(ship.runtime).for_message(message.id) if a.result], seconds=15)
    assert done[0].result == "delivered"
    assert "· notice dismissed" in tmux.capture(captain.pane().id, history=50)


def test_one_service_per_ship_by_lock_not_by_heartbeat(ship: shipmod.Ship, tmux: Tmux, capsys):
    qd(capsys, "up", "captain")
    pid, _ = service.read_heartbeat(ship.runtime)
    os.kill(pid, signal.SIGSTOP)  # hung: the lock stays held while the heartbeat ages
    try:
        service.heartbeat_path(ship.runtime).write_text(f"{pid} 2026-01-01T00:00:00+00:00\n")
        assert not service.alive(ship.runtime) and service.locked(ship.runtime)
        code = cli.main(["up"])
        err = capsys.readouterr().err
        assert code == 2 and "holds" in err and "qd down stops it" in err
    finally:
        os.kill(pid, signal.SIGCONT)
    wait_for(lambda: service.alive(ship.runtime))


def test_a_restart_waits_for_the_turn_to_end(ship: shipmod.Ship, tmux: Tmux, capsys):
    text = (ship.folder / "ship.toml").read_text().replace('step_seconds = "0.05"', 'steps = "6"\nstep_seconds = "0.5"', 1)
    (ship.folder / "ship.toml").write_text(text)
    qd(capsys, "up", "captain")
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    first = captain.launched()
    qd(capsys, "post", "order", "--to", "captain", "slow work\n$ echo first-turn-marker")
    wait_for(lambda: captain.record() and captain.harness.state(captain.record()).turn_open, seconds=15)
    proof = qd(capsys, "restart", "captain")
    assert proof.startswith("pending: the service has not answered") or "resumed" in proof
    wait_for(lambda: captain.launched() and captain.launched().at != first.at, seconds=30)
    turns = captain.harness.turns(captain.record())
    assert turns and all(t.ended for t in turns), "the running turn ended before the relaunch"
    assert captain.launched().resume is not None, "a restart resumes"


def test_the_service_reconciles_an_attempt_it_died_in_the_middle_of(ship: shipmod.Ship, tmux: Tmux, capsys):
    qd(capsys, "up", "captain")
    service.stop(ship.runtime)
    wait_for(lambda: not service.alive(ship.runtime))
    store, attempts = post.Store(ship.runtime), post.Attempts(ship.runtime)
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    launched = captain.launched()
    typed = store.new("order", "desk", "captain", "this one was typed")
    lost = store.new("order", "desk", "captain", "this one never was")
    tmux.paste(captain.pane().id, post.typed([typed]))
    tmux.keys(captain.pane().id, "Enter")
    attempts.open(typed, "captain", launched.pane, launched.pid, launched.at)
    attempts.open(lost, "captain", launched.pane, launched.pid, launched.at)
    wait_for(lambda: captain.record() and captain.harness.state(captain.record()).turns)
    assert "service started" in qd(capsys, "up").splitlines()[-1], "qd up restarts a dead service"
    wait_for(lambda: all(a.result for a in attempts.all()), seconds=10)
    results = {a.message: a.result for a in attempts.all()}
    assert results[typed.id] == "delivered" and results[lost.id] == "blind"  # law: one-door
    time.sleep(2.5)
    assert len(attempts.for_message(lost.id)) == 1, "a blind message is never resent by the service"
    assert attempts.undelivered(store, "captain") == []
