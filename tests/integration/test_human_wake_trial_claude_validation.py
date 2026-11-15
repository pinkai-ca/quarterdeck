"""The newcomer's trial on fake seats: a fresh crew, an order by hand, a captain posting to an unwatched desk.

The service reaches the desk's known-empty box once with the fixed waking line; the test then
stands in for the desk's agent exactly as the line tells it to, by running the real
``qd inbox --wait`` and reporting, and the order closes (law: one-door, nothing-waits-silently).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import quarterdeck
from quarterdeck import cli, human_wake, post, seat as seatmod, service, ship as shipmod, watch
from quarterdeck.tmux import Tmux

from .progress import service_progress
from .waiting import wait_for

ROOT = Path(__file__).resolve().parents[2]
QD = Path(sys.executable).parent / 'qd'  # this checkout's own qd, never an installed copy on the PATH

EXACT = 'qd > ship: messages are waiting. Run qd inbox --wait now to read them.'  # the captain's ruled line, 70 characters


def test_the_qd_these_gates_run_is_this_checkouts_own():
    """Provenance: the qd beside this interpreter runs this interpreter, which imports this checkout."""
    assert QD.is_file()
    assert QD.read_text().splitlines()[0] == f'#!{sys.executable}'
    assert Path(quarterdeck.__file__).resolve().is_relative_to(ROOT / 'src')
    assert seatmod.qd_bin() == QD.parent, 'the seats run the same qd'


@pytest.fixture
def ship(drydock: Path, tmux: Tmux, monkeypatch) -> shipmod.Ship:
    assert QD.is_file(), 'no installed-copy fallback'
    monkeypatch.setenv('PATH', f"{QD.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    text = (drydock / "ship.toml").read_text()
    text += ('\n[seats.captain.settings]\nstep_seconds = "0.05"\n\n[seats.desk.settings]\nstep_seconds = "0.05"\n'
             '\n[seats.worker-1.settings]\nstep_seconds = "0.05"\n')
    (drydock / "ship.toml").write_text(text)
    monkeypatch.setenv("QD_TMUX_SOCKET", tmux.socket)
    monkeypatch.setenv("QD_SHIP", str(drydock))
    monkeypatch.delenv("QD_SEAT", raising=False)
    ship = shipmod.load(drydock)
    tmux.new_session('Synthetic bootstrap')
    tmux.kill_session('Synthetic bootstrap')
    yield ship
    service.stop(ship.runtime)


def qd(capsys, *argv: str) -> str:
    code = cli.main(list(argv))
    out = capsys.readouterr()
    assert code == 0, out.err
    text = out.out.rstrip("\n")
    if argv and argv[0] == "post" and "resend" not in argv:
        from .waiting import await_drop
        await_drop(text)
    return text


def as_desk() -> dict:
    return dict(os.environ, QD_SEAT='desk')


def inputs(seat: seatmod.Seat) -> list[str]:
    record = seat.record()
    return [i.text.strip() for i in seat.harness.inputs(record)] if record else []


def box(seat: seatmod.Seat, tmux: Tmux) -> str | None:
    pane = seat.pane()
    return seat.harness.screen(tmux.capture(pane.id)).box if pane else None


def test_a_fresh_crew_order_by_hand_reaches_the_unwatched_desk_by_one_wake_and_closes(ship, tmux, capsys):
    qd(capsys, 'up')
    desk = seatmod.Seat(ship, ship.seat('desk'), tmux)
    store = post.Store(ship.runtime)
    body = ("Synthetic work, then tell the desk.\n"
            "$ qd post order --to desk --subject 'Synthetic landing' 'Synthetic: the change is ready; report on this order.'\n"
            "$ qd post report --re {this} told-the-desk")
    qd(capsys, 'post', 'order', '--to', 'captain', '--subject', 'Synthetic work', body)
    by_hand = store.all()[0]
    to_desk = wait_for(lambda: next((m for m in post.Store(ship.runtime).all() if m.to == 'desk' and m.kind == 'order'),
                                    None), seconds=60)
    assert to_desk.sender == 'captain'
    assert wait_for(lambda: post.Store(ship.runtime).closing_report(by_hand), seconds=60), 'the crew seat is unaffected'
    assert not post.watch_armed(ship.runtime, 'desk')

    woken = wait_for(lambda: inputs(desk) or None, seconds=60)
    assert woken == [EXACT], 'the fixed line, never the waiting order'
    assert all(to_desk.body not in text for text in inputs(desk))

    # The desk's agent does what the line says: it runs the watch, which prints the waiting order.
    done = subprocess.run([str(QD), 'inbox', '--wait'], env=as_desk(), capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert to_desk.short in done.stdout and 'Synthetic: the change is ready' in done.stdout
    assert [a.result for a in post.Attempts(ship.runtime).for_message(to_desk.id)] == ['watch']
    qd(capsys, 'post', 'report', '--from', 'desk', '--re', to_desk.short, 'synthetic: received')
    assert post.Store(ship.runtime).closing_report(to_desk) is not None

    marker = (post.watch_file(ship.runtime, 'desk').parent / 'watch-generation').read_text().strip()
    assert len(marker) == 32, 'the checkout\'s own watch registered a generation'
    service_progress(ship)
    service_progress(ship)
    assert inputs(desk) == [EXACT], 'one wake for the episode, never repeated'
    assert box(desk, tmux) == ''
    assert human_wake.status(desk) is None


def test_a_draft_in_the_desk_box_is_never_typed_into_and_the_wait_is_visible(ship, tmux, capsys):
    qd(capsys, 'up', 'desk')
    desk = seatmod.Seat(ship, ship.seat('desk'), tmux)
    wait_for(lambda: box(desk, tmux) == '', seconds=30)
    tmux.paste(desk.pane().id, 'operator draft, half written')  # the person is typing
    wait_for(lambda: box(desk, tmux) == 'operator draft, half written', seconds=30)
    qd(capsys, 'post', 'question', '--to', 'desk', 'Synthetic question while the person types?')
    service_progress(ship)
    service_progress(ship)
    assert box(desk, tmux) == 'operator draft, half written', 'untouched: not cleared, not submitted, nothing added'
    assert inputs(desk) == []
    assert human_wake.status(desk) == 'human wake box not empty; untouched'


def test_nothing_waiting_means_nothing_is_typed_after_up(ship, tmux, capsys):
    qd(capsys, 'up', 'desk')
    desk = seatmod.Seat(ship, ship.seat('desk'), tmux)
    wait_for(lambda: box(desk, tmux) == '', seconds=30)
    service_progress(ship)
    service_progress(ship)
    assert inputs(desk) == [] and not (desk.trusted / 'human-wake.json').exists()


def test_an_armed_watch_delivers_and_nothing_is_typed(ship, tmux, capsys):
    qd(capsys, 'up', 'desk')
    desk = seatmod.Seat(ship, ship.seat('desk'), tmux)
    wait_for(lambda: box(desk, tmux) == '', seconds=30)
    watch = subprocess.Popen([str(QD), 'inbox', '--wait'], env=as_desk(), stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    try:
        wait_for(lambda: post.watch_armed(ship.runtime, 'desk'), seconds=30)
        qd(capsys, 'post', 'question', '--to', 'desk', 'Synthetic question for an armed watch?')
        out, err = watch.communicate(timeout=60)
    finally:
        if watch.poll() is None:
            watch.kill()
            watch.wait()
    assert watch.returncode == 0, err
    assert 'Synthetic question for an armed watch?' in out
    service_progress(ship)
    assert inputs(desk) == [] and box(desk, tmux) == ''


def test_a_down_human_seat_gets_no_wake_pass_and_leaves_a_named_alert_on_the_line(ship, tmux, capsys):
    """The captain's draft alarm waits for a desk that is not up: the desk is shown unarmed and
    nothing else, so the captain's named alert keeps its place on the status line."""
    qd(capsys, 'up', 'captain')
    captain = seatmod.Seat(ship, ship.seat('captain'), tmux)
    desk = seatmod.Seat(ship, ship.seat('desk'), tmux)
    wait_for(lambda: box(captain, tmux) == '', seconds=30)
    tmux.paste(captain.pane().id, "a person's half-written thought")
    store = post.Store(ship.runtime)
    wait_for(lambda: next((m for m in store.for_seat('desk') if m.sender == 'ship'
                           and m.body == 'captain obstructed, text in the box'), None), seconds=30)
    assert not desk.is_up and post.Attempts(ship.runtime).undelivered(store, 'desk')
    service_progress(ship)
    service_progress(ship)
    assert not (desk.trusted / 'human-wake-status.json').exists(), 'no wake observation for a seat that is not up'
    assert not (desk.trusted / 'human-wake.json').exists(), 'no wake pass for a seat that is not up'
    facts = watch.facts(ship, tmux)
    assert facts.needs_human == ['captain'], facts.pending
    assert 'desk' in facts.unarmed, facts.pending
    assert not [p for p in facts.pending if p.startswith('desk') or 'wake' in p], facts.pending
    line = qd(capsys, 'status', '--line')
    assert 'captain needs a human' in line, (line, facts.pending)
    assert 'human wake' not in line, line
    assert box(captain, tmux) == "a person's half-written thought"


def test_a_wake_wait_shows_in_detailed_status_and_watch_but_not_on_the_line(ship, tmux, capsys):
    """Only states a person must act on reach the status line; every wake state stays in the detail."""
    qd(capsys, 'up', 'desk')
    desk = seatmod.Seat(ship, ship.seat('desk'), tmux)
    wait_for(lambda: box(desk, tmux) == '', seconds=30)
    tmux.paste(desk.pane().id, 'operator draft, still typing')
    wait_for(lambda: box(desk, tmux) == 'operator draft, still typing', seconds=30)
    qd(capsys, 'post', 'question', '--to', 'desk', 'Synthetic question while the person types?')
    service_progress(ship)
    service_progress(ship)
    wait = 'human wake box not empty; untouched'
    assert human_wake.status(desk) == wait and not human_wake.action_required(desk, wait)
    line = qd(capsys, 'status', '--line')
    assert 'human wake' not in line, line
    assert wait in qd(capsys, 'status'), 'qd status detail shows every wake state'
    assert wait in qd(capsys, 'watch', 'desk'), 'qd watch shows every wake state'
    assert box(desk, tmux) == 'operator draft, still typing' and inputs(desk) == []


def test_a_seat_proved_down_has_no_wake_state_and_an_unreadable_launch_is_not_silenced(ship, tmux, capsys):
    """With the service down, an up desk with a waiting question truly awaits observation, in detail;
    taken down, it has no wake state at all and shows only that no watch is armed; and launch evidence
    that cannot be read is not taken for down."""
    seatmod.bring_up(ship, tmux)
    desk = seatmod.Seat(ship, ship.seat('desk'), tmux)
    wait_for(lambda: box(desk, tmux) == '', seconds=30)
    qd(capsys, 'post', 'question', '--to', 'desk', 'Synthetic question while the service is down?')
    # The service is down, so the drop waits. File it here; the service stays down.
    post.file_drops(post.Store(ship.runtime))
    observing = 'human wake awaiting service observation'
    assert desk.is_up and human_wake.status(desk) == observing
    assert observing in qd(capsys, 'status') and observing in qd(capsys, 'watch', 'desk')
    assert 'human wake' not in qd(capsys, 'status', '--line')

    qd(capsys, 'down', 'desk')
    wait_for(lambda: not desk.is_up, seconds=30)
    assert post.Attempts(ship.runtime).undelivered(post.Store(ship.runtime), 'desk'), 'the question still waits'
    assert human_wake.status(desk) is None, 'a seat proved down has no wake state'
    assert 'human wake' not in qd(capsys, 'status'), 'no wake row for a seat proved down'
    assert 'human wake' not in qd(capsys, 'watch', 'desk')
    assert 'desk: no watch armed' in qd(capsys, 'status', '--line'), 'its waiting message still shows'
    assert not (desk.trusted / 'human-wake-status.json').exists() and inputs(desk) == []

    (desk.trusted / 'launch.json').write_text('{ unreadable synthetic launch')
    try:
        said = human_wake.status(desk)
    except (OSError, ValueError, shipmod.ShipError) as error:
        said = f'raised: {error}'
    assert said, 'unknown ownership evidence is never read as down'
