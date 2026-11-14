"""The service as a process: heartbeat, deliveries, inboxes, requests, fresh after order, re-brief."""

from __future__ import annotations

import json
import os
import signal
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from quarterdeck import cli, ledger, post, seat as seatmod, service, servicectl, ship as shipmod
from quarterdeck.door import CONFIRM_SECONDS
from quarterdeck.harness.base import Usage, Window
from quarterdeck.tmux import Tmux

from .waiting import wait_for
from .progress import service_progress
from quarterdeck.harness.synthetic_control import Rendezvous, events


@pytest.fixture
def ship(drydock: Path, tmux: Tmux, monkeypatch) -> shipmod.Ship:
    """The example ship with quick fakes, the CLI pointed at the private tmux server."""
    text = (drydock / "ship.toml").read_text()
    text += '\n[seats.captain.settings]\nstep_seconds = "0.05"\n\n[seats.worker-1.settings]\nstep_seconds = "0.05"\n'
    (drydock / "ship.toml").write_text(text)
    monkeypatch.setenv("QD_TMUX_SOCKET", tmux.socket)
    monkeypatch.setenv("QD_SHIP", str(drydock))
    monkeypatch.delenv("QD_SEAT", raising=False)
    ship = shipmod.load(drydock)
    # The creator starts the server; CLI/service receive only an existing transport.
    tmux.new_session('Synthetic bootstrap')
    tmux.kill_session('Synthetic bootstrap')
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
    inbox_path = ship.runtime / "post" / "inbox" / "captain.md"
    def rendered_order():
        text = inbox_path.read_text() if inbox_path.exists() else ""
        return text if f"order {message.short} from operator" in text else None
    inbox = wait_for(rendered_order)
    assert f"order {message.short} from operator" in inbox and "## Open (1)" in inbox
    # the addressee's report closes it, and the inbox follows
    qd(capsys, "post", "report", "--from", "captain", "--re", message.short, "counted: many")
    wait_for(lambda: "## Open (0)" in (ship.runtime / "post" / "inbox" / "captain.md").read_text())
    assert "service stopped" in qd(capsys, "down").splitlines()[-1] and not service.alive(ship.runtime)


def test_a_seat_in_a_human_session_is_reached_by_its_watch_only(ship: shipmod.Ship, tmux: Tmux, capsys):
    """Reached by its watch, and only the guarded fixed waking line otherwise."""
    qd(capsys, "up", "desk")
    proof = qd(capsys, "post", "question", "--to", "desk", "Which branch?")
    assert proof.endswith("no watch armed on desk; waits in the inbox")
    service_progress(ship)
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


def test_controls_use_the_declaration_written_after_the_service_pass_loaded(ship, tmux, capsys, monkeypatch):
    from .control_dispatch_forcing import force_edit_between_load_and_dispatch

    force_edit_between_load_and_dispatch(monkeypatch)
    test_controls_go_through_the_service_by_slash_command(ship, tmux, capsys)
    assert (ship.runtime / "declaration-race-loaded").read_text() == "fake-1"
    observed = json.loads((ship.runtime / "declaration-race-observed").read_text())
    assert observed == {"started_from": "fake-2", "continued_from": "fake-2"}


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
    inputs = [item.text for item in captain.harness.inputs(captain.record())]
    first, second = post.delivered_forms(message)
    assert inputs.count(first.strip()) == 1 and inputs[-1].strip() == second.strip(), \
        "the resend says it is a second delivery, and its receipt read that form"


def test_a_worker_starts_fresh_after_its_order_closes(ship: shipmod.Ship, tmux: Tmux, capsys):
    qd(capsys, "up", "worker-1")
    worker = seatmod.Seat(ship, ship.seat("worker-1"), tmux)
    first_launch = worker.launched()
    qd(capsys, "set", "worker-1", "model=fake-2")  # written to the ship file while the seat runs
    # the fake follows the "$ " line, which posts the closing report from inside the seat (law: can-post)
    qd(capsys, "post", "order", "--to", "worker-1", "Do it and report.\n$ qd post report --re {this} done")
    wait_for(lambda: not post.Store(ship.runtime).open_for("worker-1"), seconds=20)
    report = post.Store(ship.runtime).all()[-1]
    assert report.kind == "report" and report.sender == "worker-1" and report.to == "operator"
    wait_for(lambda: (launched := worker.launched()) and launched.at != first_launch.at, seconds=20)  # law: context-is-cache
    assert worker.launched().resume is None, "fresh: a new conversation"
    assert "fake-2" in worker.launched().argv, "the service read the ship file afresh, not what it loaded at start"  # law: ship-file
    book_path = ship.runtime / "ledger.sqlite"
    wait_for(lambda: book_path.exists() and ledger.Ledger(book_path).rollup("seat"), seconds=15)
    rows = ledger.Ledger(book_path).rollup("seat")
    assert rows and rows[0].key == "worker-1" and rows[0].turns >= 1, "the closing turn is accounted though its record was left behind"  # law: every-turn-accounted
    wait_for(lambda: '"event": "fresh"' in (ship.runtime / "log" / "service.jsonl").read_text())


def test_changed_orders_rebrief_every_seat_with_a_message_from_ship(ship: shipmod.Ship, tmux: Tmux, capsys):
    qd(capsys, "up", "captain")
    # The service captures its brief baseline before publishing its start event.
    log = ship.runtime / "log" / "service.jsonl"
    wait_for(lambda: log.exists() and '"event": "start"' in log.read_text(), seconds=1.2)
    (ship.folder / "orders.md").write_text("# Ship's orders: drydock\n\nNew orders.\n")
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


def test_a_busy_service_reads_busy_not_dead_and_qd_down_still_stops_it(ship: shipmod.Ship, tmux: Tmux, capsys, monkeypatch):
    monkeypatch.setattr(servicectl, "ANSWER_SECONDS", 1.0)
    qd(capsys, "up", "captain")
    pid, _ = service.read_heartbeat(ship.runtime)
    from quarterdeck.tmux_socket import locked
    # Model actor work that never ends, between native namespace operations.
    # Holding an actual namespace lease is separately tested as ownership uncertainty.
    with locked(tmux.socket, tmux._owned.namespace):
        os.kill(pid, signal.SIGSTOP)  # the service lock stays held while the heartbeat ages
    try:
        stamp = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
        service.heartbeat_path(ship.runtime).write_text(f"{pid} {stamp}\n")
        assert service.alive(ship.runtime) and 29 < service.stale(ship.runtime) < 40, "the lock says alive, the heartbeat says busy"
        assert qd(capsys, "status", "--line").startswith("service busy 3")  # law: nothing-waits-silently
        assert qd(capsys, "status").splitlines()[0].startswith("service busy 3")
        assert qd(capsys, "up").splitlines()[-1].startswith(f"service alive · pid {pid} · busy, last tick 3"), "nobody is told to qd down"
        filed = qd(capsys, "set", "captain", "effort=low").splitlines()[1]
        assert filed.startswith("  pending: the service has not answered within 1s") and "; it last ticked 3" in filed, "filed, not refused"
    finally:
        os.kill(pid, signal.SIGCONT)
    wait_for(lambda: service.stale(ship.runtime) is None)
    assert qd(capsys, "status", "--line").startswith("service ok")
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    wait_for(lambda: captain.harness.screen(tmux.capture(captain.pane().id)).shows.get("effort") == "low", seconds=15)
    os.kill(pid, signal.SIGSTOP)
    proof = service.stop(ship.runtime)
    assert proof == f"service killed · pid {pid} · it did not answer SIGTERM within 3s" and not service.alive(ship.runtime)


def test_a_watch_attempt_is_judged_only_after_the_confirm_window(ship: shipmod.Ship, tmux: Tmux, capsys):
    qd(capsys, "up", "captain")
    store, attempts = post.Store(ship.runtime), post.Attempts(ship.runtime)
    question = store.new("question", "operator", "desk", "still printing")
    opened = attempts.open(question, "desk", None, None, None)  # a watch that is printing right now
    service_progress(ship)
    assert (post.now() - opened.when).total_seconds() < CONFIRM_SECONDS
    assert attempts.for_message(question.id)[0].result is None, "left to the watch within the confirm window"
    post.printed(ship.runtime, [opened])
    wait_for(lambda: (post.now() - opened.when).total_seconds() > CONFIRM_SECONDS)
    service_progress(ship)
    assert attempts.for_message(question.id)[0].result == "watch", "the watch's own result stands"  # law: one-door
    lost = store.new("question", "operator", "desk", "the watch died")
    attempts.open(lost, "desk", None, None, None)
    wait_for(lambda: attempts.for_message(lost.id)[0].result == "blind", seconds=CONFIRM_SECONDS + 5)


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


def test_a_seat_that_needs_a_human_is_reported_to_the_desk_once(ship: shipmod.Ship, tmux: Tmux, capsys):
    text = (ship.folder / "ship.toml").read_text().replace('[seats.worker-1.settings]\n', '[seats.worker-1.settings]\ndialog = "unknown"\n')
    (ship.folder / "ship.toml").write_text(text)
    qd(capsys, "up", "worker-1")
    store = post.Store(ship.runtime)
    report = wait_for(lambda: [m for m in store.all() if m.sender == "ship" and m.to == "desk"], seconds=15)[0]
    assert report.kind == "report" and report.body == "worker-1 obstructed, unknown dialog: Something else"
    service_progress(ship)
    assert len([m for m in store.all() if m.sender == "ship"]) == 1, "told once, not every tick"


def test_set_adopt_writes_what_the_seat_runs_into_the_file(ship: shipmod.Ship, tmux: Tmux, capsys):
    qd(capsys, "up", "captain")
    qd(capsys, "set", "captain", "effort=low")
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    wait_for(lambda: captain.harness.screen(tmux.capture(captain.pane().id)).shows.get("effort") == "low")
    (ship.folder / "ship.toml").write_text((ship.folder / "ship.toml").read_text().replace('effort = "low"', 'effort = "high"'))
    assert shipmod.load(ship.folder).seat("captain").effort == "high", "the file and the seat disagree"
    lines = qd(capsys, "set", "captain", "--adopt").splitlines()
    assert any(line.startswith('ship.toml: seats.captain.effort = "low"') for line in lines), lines
    assert shipmod.load(ship.folder).seat("captain").effort == "low"


def test_every_turn_is_accounted_by_pool_seat_and_order(ship: shipmod.Ship, tmux: Tmux, capsys):
    qd(capsys, "up", "captain")
    proof = qd(capsys, "post", "order", "--to", "captain", "count\n$ qd post report --re {this} counted")
    short = proof.split(" ")[0]
    store = post.Store(ship.runtime)
    order = store.get(short)
    wait_for(lambda: not store.open_for("captain"), seconds=20)
    book_path = ship.runtime / "ledger.sqlite"
    wait_for(lambda: book_path.exists() and ledger.Ledger(book_path).rollup("seat"), seconds=15)
    book = ledger.Ledger(book_path)
    rows = book.rollup("seat")
    assert rows[0].key == "captain" and rows[0].turns == 1 and rows[0].read_cached > 0 and rows[0].tool_calls == 1
    by_order = book.rollup("order")
    assert [r.key for r in by_order] == [order.id], "the closing turn is charged to the order it closes"  # law: every-turn-accounted
    assert book.rollup("pool")[0].key == "fake"
    book.close()
    out = qd(capsys, "ledger", "--by", "seat")
    assert out.splitlines()[1].startswith("captain") and " 1 " in out.splitlines()[1]
    assert order.id in qd(capsys, "ledger", "--by", "order")
    assert "no usage read yet" in qd(capsys, "ledger"), "the fake has no subscription windows"
    assert qd(capsys, "ledger", "--tools").splitlines()[1].startswith("Bash")


def test_a_seat_moves_on_at_a_wall_and_stays(ship: shipmod.Ship, tmux: Tmux, capsys):
    text = (ship.folder / "ship.toml").read_text()
    text = text.replace('[pools.fake]\nprovider = "synthetic"', '[pools.fake]\nprovider = "synthetic"\n\n[pools.fake-2]\nprovider = "synthetic"')
    text = text.replace('[seats.captain]\nsession = "bridge"\nrole = "captain"\npersona = "Picard"\nharness = "fake"\npool = "fake"',
                        '[seats.captain]\nsession = "bridge"\nrole = "captain"\npersona = "Picard"\nharness = "fake"\npool = ["fake", "fake-2"]')
    (ship.folder / "ship.toml").write_text(text)
    ship = shipmod.load(ship.folder)
    assert ship.seat("captain").pools == ("fake", "fake-2")
    qd(capsys, "up", "captain")
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    first = captain.launched()
    assert captain.pool.name == "fake"
    post.Store(ship.runtime).new('order', 'desk', 'captain', 'complete one synthetic turn before the wall')
    def recorded_idle():
        record = captain.record()
        state = captain.harness.state(record) if record else None
        return state is not None and state.turns and state.turn_open is False and not state.queued
    wait_for(recorded_idle)
    book = ledger.Ledger(ship.runtime / "ledger.sqlite")
    book.record_usage("fake", Usage((Window("5h", 1.0, datetime.now(timezone.utc) + timedelta(hours=1)),), datetime.now(timezone.utc)))
    book.close()
    wait_for(lambda: captain.launched() and captain.launched().at != first.at, seconds=20)
    assert captain.pool.name == "fake-2" and captain.launched().pool == "fake-2"
    notice = wait_for(lambda: [m for m in post.Store(ship.runtime).all() if m.sender == "ship" and "moved to pool" in m.body], seconds=10)[0]
    assert notice.body == "moved to pool fake-2 from fake at its wall"
    assert "held at limit" not in qd(capsys, "status", "--line")  # law: nothing-waits-silently
    service_progress(ship)
    assert captain.pool.name == "fake-2", "it stays, even though the first pool is the one declared first"


def test_a_blocked_delivery_goes_in_on_the_tick_after_the_human_clears_the_box(ship: shipmod.Ship, tmux: Tmux, capsys):
    qd(capsys, "up", "captain")
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    wait_for(lambda: captain.harness.screen(tmux.capture(captain.pane().id)).box == "")
    tmux.paste(captain.pane().id, "a human's half-written thought")  # the human at this keyboard
    store, attempts = post.Store(ship.runtime), post.Attempts(ship.runtime)
    draft_report = wait_for(lambda: next((m for m in store.for_seat("desk") if m.sender == "ship"
                                         and m.body == "captain obstructed, text in the box"), None))
    proof = qd(capsys, "post", "order", "--to", "captain", "waits behind the human's text")
    order = store.get(proof.split()[0])
    assert store.all().index(draft_report) < store.all().index(order)
    service_progress(ship)
    assert attempts.all() == [], "nothing typed, nothing attempted while a human's text is in the box"  # law: one-door
    assert "captain needs a human" in qd(capsys, "status", "--line")
    tmux.keys(captain.pane().id, "C-u")  # the human clears their own box
    done = wait_for(lambda: [a for a in attempts.all() if a.accepted], seconds=6)  # law: events-not-clocks
    assert done[0].message == order.id and done[0].result == "delivered"


def test_a_message_once_queued_is_not_carried_again_once_read(ship: shipmod.Ship, tmux: Tmux, capsys):
    qd(capsys, "up", "captain")
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    store, attempts = post.Store(ship.runtime), post.Attempts(ship.runtime)
    with Rendezvous(captain.home, "step", "first held turn") as first_step, \
            Rendezvous(captain.home, "turn", "second, mid-turn") as second_turn:
        qd(capsys, "post", "order", "--to", "captain", "first held turn\n$ qd post report --re {this} done-1")
        wait_for(first_step.reached)
        record = captain.record()
        assert captain.harness.state(record).turn_open
        assert captain.harness.state(record).open_tool is not None
        ref = qd(capsys, "post", "order", "--to", "captain", "second, mid-turn\n$ qd post report --re {this} done-2").split(" ")[0]
        message = store.get(ref)
        payload = post.typed([message]).strip()
        assert wait_for(lambda: attempts.latest("captain").get(message.id) == "queued")
        assert captain.harness.state(record).queued == (payload,)
        first_step.release()
        wait_for(second_turn.reached)
        service_progress(ship)
        assert captain.harness.state(record).turns == 1, "preceding turn ended"
        assert attempts.latest("captain")[message.id] == "queued", "completion is not consumption"
        assert captain.harness.state(record).queued == (payload,)
        second_turn.release()
        assert wait_for(lambda: attempts.latest("captain").get(message.id) == "delivered")
        wait_for(lambda: not store.open_for("captain"))
    assert len(attempts.for_message(message.id)) == 1
    assert sum(e == {"event": "submitted", "text": payload} for e in events(captain.home)) == 1
    assert "queued behind a turn" not in qd(capsys, "status", "--line")
    proof = qd(capsys, "fresh", "captain")
    assert "carried" not in proof, f"nothing is open or unread, so nothing is carried: {proof}"  # law: context-is-cache


def test_interrupt_reaches_a_working_seat_that_does_not_queue_through_the_service(ship: shipmod.Ship, tmux: Tmux, capsys):
    text = (ship.folder / "ship.toml").read_text().replace('step_seconds = "0.05"', 'queues = false\nsteps = "8"\nstep_seconds = "0.6"', 1)
    (ship.folder / "ship.toml").write_text(text)
    qd(capsys, "up", "captain")
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    qd(capsys, "post", "order", "--to", "captain", "a long turn")
    wait_for(lambda: captain.harness.screen(tmux.capture(captain.pane().id)).working, seconds=15)
    urgent = qd(capsys, "post", "order", "--to", "captain", "--interrupt", "urgent").split(" ")[0]
    store, attempts = post.Store(ship.runtime), post.Attempts(ship.runtime)
    done = wait_for(lambda: [a for a in attempts.for_message(store.get(urgent).id) if a.result], seconds=15)
    assert done[0].result in ("delivered", "queued"), done[0]
    assert "· interrupted" in tmux.capture(captain.pane().id, history=100)


def test_an_ack_wakes_nobody_and_a_resend_to_the_office_goes_to_its_watch(ship: shipmod.Ship, tmux: Tmux, capsys):
    qd(capsys, "up", "captain")
    qd(capsys, "up", "desk")
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    order = qd(capsys, "post", "order", "--from", "captain", "--to", "worker-1", "build it").split(" ")[0]
    qd(capsys, "post", "ack", "--from", "worker-1", "--re", order)
    service_progress(ship)
    assert post.Attempts(ship.runtime).all() == [], "an ack is never typed into the captain's pane"  # law: open-until-closed
    assert captain.record() is None, "and the captain was never woken"
    question = qd(capsys, "post", "question", "--to", "desk", "for the desk").split(" ")[0]
    store, attempts = post.Store(ship.runtime), post.Attempts(ship.runtime)
    message = store.get(question)
    attempts.close(attempts.open(message, "desk", None, None, None), "blind", "a watch that died mid-print")
    proof = qd(capsys, "post", "resend", question)
    assert proof.startswith(f"{question} re-offered to desk's watch") and "no watch is armed yet" in proof
    assert attempts.undelivered(store, "desk") == [message], "offered to the watch again"
    desk = seatmod.Seat(ship, ship.seat("desk"), tmux)
    assert desk.harness.screen(tmux.capture(desk.pane().id)).box == "", "and no key was pressed in the human's session"  # law: one-door
    printed = qd(capsys, "inbox", "--wait", "desk")
    assert printed.startswith(f"qd > second delivery of {message.id}; you may have it already: act on it once\n")
    assert post.typed([message]).strip() in printed, "the watch printed it once more, saying it is a second delivery"


def test_status_shows_what_the_seat_runs_and_never_the_ship_file_as_a_fact(ship: shipmod.Ship, tmux: Tmux, capsys):
    rows = {r.split()[0]: r for r in qd(capsys, "status").splitlines()[2:5]}
    assert "- (ship file: fake-1)" in rows["captain"], "down: nothing observed, the declaration shown as such"
    qd(capsys, "up", "captain")
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    wait_for(lambda: captain.harness.screen(tmux.capture(captain.pane().id)).shows.get("model") == "fake-1")
    rows = {r.split()[0]: r for r in qd(capsys, "status").splitlines()[2:5]}
    assert "fake · fake-1" in rows["captain"] and "ship file" not in rows["captain"], "observed on the screen and agreeing"
    tmux.paste(captain.pane().id, "/model fake-9")
    tmux.keys(captain.pane().id, "Enter")
    wait_for(lambda: captain.harness.screen(tmux.capture(captain.pane().id)).shows.get("model") == "fake-9")
    rows = {r.split()[0]: r for r in qd(capsys, "status").splitlines()[2:5]}
    assert "fake-9 (ship file: fake-1)" in rows["captain"], "a hand changed it in the pane: the difference is shown"  # law: record-first


def test_the_service_reconciles_an_attempt_it_died_in_the_middle_of(ship: shipmod.Ship, tmux: Tmux, capsys):
    qd(capsys, "up", "captain")
    service.stop(ship.runtime)
    wait_for(lambda: not service.alive(ship.runtime))
    store, attempts = post.Store(ship.runtime), post.Attempts(ship.runtime)
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    launched = captain.launched()
    typed = store.new("order", "desk", "captain", "this one was typed")
    lost = store.new("order", "desk", "captain", "this one never was")
    attempts.open(typed, "captain", launched.pane, launched.pid, launched.at)
    attempts.open(lost, "captain", launched.pane, launched.pid, launched.at)
    tmux.paste(captain.pane().id, post.typed([typed]))
    tmux.keys(captain.pane().id, "Enter")
    wait_for(lambda: captain.record() and captain.harness.state(captain.record()).turns)
    assert "service started" in qd(capsys, "up").splitlines()[-1], "qd up restarts a dead service"
    wait_for(lambda: all(a.result for a in attempts.all()), seconds=10)
    results = {a.message: a.result for a in attempts.all()}
    assert results[typed.id] == "delivered" and results[lost.id] == "blind"  # law: one-door
    service_progress(ship)
    assert len(attempts.for_message(lost.id)) == 1, "a blind message is never resent by the service"
    assert attempts.undelivered(store, "captain") == []
