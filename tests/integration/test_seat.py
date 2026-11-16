"""Seats on the example ship: up, down, restart, fresh; homes, briefs, records; identity."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from quarterdeck import post, post_threads, seat as seatmod, ship as shipmod
from quarterdeck.ship import ShipError
from quarterdeck.tmux import Tmux

from .waiting import wait_for


@pytest.fixture
def ship(drydock: Path) -> shipmod.Ship:
    text = (drydock / "ship.toml").read_text() + '\n[seats.captain.settings]\nstep_seconds = "0.05"\n'
    (drydock / "ship.toml").write_text(text)
    return shipmod.load(drydock)


def environ(pid: int) -> dict[str, str]:
    raw = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    return dict(item.decode().partition("=")[::2] for item in raw if item)


def test_up_brings_up_every_declared_seat_in_its_session(ship: shipmod.Ship, tmux: Tmux):
    proofs = seatmod.bring_up(ship, tmux)
    assert proofs[0] == "runtime/ fenced: nested repository created"
    assert "not its own repository" in proofs[1]
    assert [p.split(" ")[0] for p in proofs[2:]] == ["captain", "desk", "worker-1"]
    assert all("· fake fake · provider Synthetic · pool fake · fresh" in p for p in proofs[2:])
    assert sorted(tmux.sessions()) == ["Bridge", "Crew", "Office"]
    assert [p.window for p in tmux.panes("Bridge")] == ["Picard"]
    assert [p.window for p in tmux.panes("Office")] == ["Maeve"]
    assert [p.window for p in tmux.panes("Crew")] == ["worker-1"]
    runtime = ship.runtime
    assert (runtime / ".git").is_dir() and (runtime / "brief" / "captain.md").is_file()
    assert (runtime / "seats" / "captain" / "home" / "settings.json").is_file()
    launched = json.loads((runtime / "trusted" / "captain" / "launch.json").read_text())
    assert launched["pane"].startswith("%") and launched["pool"] == "fake" and launched["resume"] is None
    env = environ(launched["pid"])
    assert env["QD_SHIP"] == str(ship.folder) and env["QD_SEAT"] == "captain"
    assert env["QD_FAKE_HOME"] == str(runtime / "seats" / "captain" / "home")
    assert env["PATH"].split(":")[0] == str(runtime / "bin")
    assert (runtime / "bin" / "qd").resolve() == (seatmod.qd_bin() / "qd").resolve()
    status = tmux.run("show-option", "-v", "-t", "=Bridge:", "status-right").strip()
    assert status.endswith(f"--ship {ship.folder} status --line)"), status
    assert subprocess.run(status[2:-1], shell=True, capture_output=True, text=True).stdout.startswith("SERVICE DOWN")
    assert seatmod.bring_up(ship, tmux)[2:] == [f"{s} already up in {seatmod.Seat(ship, ship.seat(s), tmux).pane().id}"
                                                for s in ("captain", "desk", "worker-1")]


def test_a_seat_already_up_is_refused_with_the_way_out(ship: shipmod.Ship, tmux: Tmux):
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    captain.up()
    with pytest.raises(ShipError, match="qd restart captain"):
        captain.up()


def test_restart_resumes_the_record_and_fresh_does_not(ship: shipmod.Ship, tmux: Tmux):
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    captain.up()
    wait_for(lambda: captain.harness.screen(tmux.capture(captain.pane().id)).box is not None)
    tmux.paste(captain.pane().id, "hello")
    tmux.keys(captain.pane().id, "Enter")
    record = wait_for(captain.record)
    wait_for(lambda: captain.harness.state(record).turns == 1)
    standing_order = post.Store(ship.runtime).new("order", "desk", "captain", "still open across the restart")
    proof = captain.restart()
    assert proof.splitlines()[0].startswith("captain down · pane %") and " · resumed " in proof
    assert proof.endswith("1 thread indexed"), "every open thread, on a restart too"  # law: context-is-cache
    assert captain.record() == record, "the same conversation, in a new process"
    wait_for(lambda: standing_order.id in " ".join(t.input for t in captain.harness.turns(record)))
    assert "· 1 turns" in wait_for(lambda: tmux.capture(captain.pane().id) if "resumed" in tmux.capture(captain.pane().id) else None)
    proof = captain.fresh()
    assert "· fresh · 1 thread indexed" in proof, "the still-open thread is indexed in the new conversation"
    assert captain.join().reason in ("no record yet", None), "a fresh start begins a new record"
    assert record.exists(), "the old record stays for the Ledger"


def test_a_fresh_seat_indexes_its_open_messages_and_reads_each_thread_whole(ship: shipmod.Ship, tmux: Tmux):
    store = post.Store(ship.runtime)
    order = store.new('order', 'desk', 'captain', 'Count the stars.\n$ echo counted', subject='Stars')
    store.new('report', 'captain', 'desk', 'an old report, not open', re=None)
    captain = seatmod.Seat(ship, ship.seat('captain'), tmux)
    captain.up(resume=False)
    record = wait_for(captain.record)
    wait_for(lambda: state if (state := captain.harness.state(record)).turns else None)  # law: context-is-cache
    first = captain.harness.turns(record)[0]
    assert first.input.startswith('qd > briefed as captain, captain · 1 order open\n')
    rows = {row['id']: row for row in (json.loads(line) for line in first.input.splitlines() if line.startswith('{'))}
    assert rows[order.id]['subject'] == 'Stars' and rows[order.id]['thread'] == f'qd thread {order.id}'
    assert order.body not in first.input
    printed = post_threads.render(post_threads.whole(store, order.id, captain.name))
    assert order.body in printed and f'qd > reply: qd post report --re {order.short}' in printed
    assert first.tools and all(tool.name == 'Read' for tool in first.tools), 'an index uses only the simulator default Read steps, never the stored body command'
    assert 'briefed: ' in tmux.capture(captain.pane().id, history=100)


def test_a_launch_longer_than_tmux_allows_keeps_full_self_handoff_and_indexes_other_work(ship: shipmod.Ship, tmux: Tmux):
    """A full self-handoff over tmux's 16 KiB command limit still uses launch.sh; ordinary work stays indexed."""
    store = post.Store(ship.runtime)
    body = 'A long self handoff.\n' + '\n'.join(f'paragraph {n}: ' + ' '.join(['words'] * 40) for n in range(80))
    assert len(body) > 16384
    handoff = store.new('order', 'captain', 'captain', body)
    ordinary = store.new('order', 'desk', 'captain', 'A distinct long ordinary task.\n' + body)
    captain = seatmod.Seat(ship, ship.seat('captain'), tmux)
    assert captain.up(resume=False).endswith('2 threads indexed')
    script = (ship.runtime / 'trusted' / 'captain' / 'launch.sh').read_text()
    assert script.startswith('#!/bin/sh\n') and '\nexec env -u TMUX -u TMUX_PANE ' in script and ' PATH=' in script
    assert len(script.encode()) > 16384 and handoff.short in script and body[-200:] in script
    assert ordinary.id in script and ordinary.body not in script
    record = wait_for(captain.record)
    wait_for(lambda: captain.harness.state(record).turns)
    first = captain.harness.turns(record)[0]
    assert handoff.short in first.input and body.splitlines()[-1] in first.input, 'the complete handoff reached the program'  # law: context-is-cache
    rows = {row['id']: row for row in (json.loads(line) for line in first.input.splitlines() if line.startswith('{'))}
    assert rows[ordinary.id]['thread'] == f'qd thread {ordinary.id}' and ordinary.body not in first.input
    assert ordinary.body in post_threads.render(post_threads.whole(store, ordinary.id, captain.name))


def test_a_program_missing_from_the_path_is_refused_before_anything_opens(ship: shipmod.Ship, tmux: Tmux, monkeypatch):
    monkeypatch.setattr(seatmod.shutil, "which", lambda *args, **kwargs: None)
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    with pytest.raises(ShipError, match="is not on the PATH qd runs with; install it"):
        captain.up()
    assert captain.launched() is None and tmux.sessions() == [], "nothing opened"


def test_a_program_that_exits_at_once_is_a_refusal_not_an_up(ship: shipmod.Ship, tmux: Tmux, monkeypatch):
    from quarterdeck.harness.base import Launch

    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    monkeypatch.setattr(captain.harness, "launch", lambda *args, **kwargs: Launch(("sh", "-c", "echo no such model; exit 7")))
    with pytest.raises(ShipError, match="sh exited 7 at once; its pane stays for a look.*It printed: no such model"):
        captain.up()
    pane = captain.pane()
    assert pane is not None and pane.dead and pane.exit_status == 7, "the pane stays, and the seat knows whose it is"
    monkeypatch.undo()
    assert "captain up in %" in captain.up()
    assert [p.window for p in tmux.panes("Bridge")] == ["Picard"], "the dead pane went when the seat came back"


def test_identity_is_pane_and_process_not_name(ship: shipmod.Ship, tmux: Tmux):
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    captain.up()
    launched = captain.launched()
    assert captain.pane() is not None and captain.is_up
    path = ship.runtime / "trusted" / "captain" / "launch.json"
    data = json.loads(path.read_text())
    data["pid"] = launched.pid + 1
    path.write_text(json.dumps(data))
    assert captain.pane() is None, "the same pane with another process is not this seat"  # law: identity
    path.write_text(json.dumps({**data, "pid": launched.pid}))
    assert captain.down().startswith("captain down · pane %")
    assert captain.down() == "captain was not up" and not captain.is_up


def test_a_session_owned_by_another_ship_is_refused(ship: shipmod.Ship, tmux: Tmux, tmp_path: Path):
    seatmod.Seat(ship, ship.seat("captain"), tmux).up()
    other_folder = tmp_path / "ships" / "other"
    shutil.copytree(ship.folder, other_folder, ignore=shutil.ignore_patterns("runtime"))
    other = shipmod.load(other_folder)
    with pytest.raises(ShipError, match="belongs to .*drydock.*another title"):
        seatmod.Seat(other, other.seat("captain"), tmux).up()
    assert [p.window for p in tmux.panes("Bridge")] == ["Picard"], "nothing of the other ship landed here"


def test_a_missing_working_directory_is_refused_not_created(ship: shipmod.Ship, tmux: Tmux, tmp_path: Path):
    nowhere = tmp_path / "nowhere"
    text = (ship.folder / "ship.toml").read_text().replace('[seats.captain]\n', f'[seats.captain]\ncwd = "{nowhere}"\n')
    (ship.folder / "ship.toml").write_text(text)
    ship = shipmod.load(ship.folder)
    with pytest.raises(ShipError, match="does not exist; qd creates nothing outside its folder"):  # law: one-folder
        seatmod.Seat(ship, ship.seat("captain"), tmux).up()
    assert not nowhere.exists()


def test_bring_down_closes_the_sessions(ship: shipmod.Ship, tmux: Tmux):
    seatmod.bring_up(ship, tmux)
    proofs = seatmod.bring_down(ship, tmux)
    assert all("down · pane" in p for p in proofs) and tmux.sessions() == []


def test_remote_control_is_refused_by_name_on_a_program_without_it(ship: shipmod.Ship, tmux: Tmux):
    seat = seatmod.Seat(ship, replace(ship.seat("captain"), remote_control=True), tmux)
    with pytest.raises(ShipError, match="fake has no Remote Control; select a harness that offers it"):
        seat.up()
    assert not seat.is_up and tmux.sessions() == [], "refused before anything opened"


def _talked(seat, tmux) -> Path:
    """The seat up, one turn taken: a conversation a resume could continue."""
    seat.up()
    wait_for(lambda: seat.harness.screen(tmux.capture(seat.pane().id)).box is not None)
    tmux.paste(seat.pane().id, "hello")
    tmux.keys(seat.pane().id, "Enter")
    record = wait_for(seat.record)
    wait_for(lambda: seat.harness.state(record).turns == 1)
    return record


@pytest.fixture
def handed(ship: shipmod.Ship) -> shipmod.Ship:
    """The example ship with a hand on the fake program beside its worker."""
    file = ship.folder / "ship.toml"
    file.write_text(file.read_text() + '\n[seats.hand-1]\nsession = "crew"\nrole = "hand"\nharness = "fake"\n'
                    'pool = "fake"\nmodel = "fake-1"\n')
    return shipmod.load(ship.folder)


def test_a_hand_is_never_resumed_whatever_relaunches_it(handed: shipmod.Ship, tmux: Tmux):
    """A hand takes each order in a fresh context: a restart, and the launch every other path shares (the
    service's relaunch, a recovery, a failover), start it fresh with its open messages, never on its old record."""
    hand = seatmod.Seat(handed, handed.seat("hand-1"), tmux)
    record = _talked(hand, tmux)
    order = post.Store(handed.runtime).new("order", "worker-1", "hand-1", "still open across the restart")
    proof = hand.restart()
    assert " · resumed " not in proof and "· fresh" in proof and "a hand starts fresh on every launch" in proof
    assert "· fresh · 1 thread indexed" in proof, "its open message is carried, as any fresh start carries it"
    launched = json.loads((hand.trusted / "launch.json").read_text())
    assert launched["resume"] is None, "the launch names no conversation to resume"
    assert hand.record() != record and record.exists(), "a new conversation; the old record stays for the Ledger"
    second = wait_for(hand.record)
    wait_for(lambda: order.id in " ".join(t.input for t in hand.harness.turns(second)))
    hand.down()
    with hand._lifecycle():
        proof = hand._up(resume=True)  # what the service's relaunch, a recovery and a failover call
    assert " · resumed " not in proof and "· fresh" in proof
    assert json.loads((hand.trusted / "launch.json").read_text())["resume"] is None
    assert hand.record() != second


def test_a_worker_beside_a_hand_still_resumes(handed: shipmod.Ship, tmux: Tmux):
    worker = seatmod.Seat(handed, handed.seat("worker-1"), tmux)
    record = _talked(worker, tmux)
    proof = worker.restart()
    assert " · resumed " in proof and "a hand starts fresh" not in proof
    assert worker.record() == record, "the same conversation, in a new process"
    assert json.loads((worker.trusted / "launch.json").read_text())["resume"] is not None


class _Book:
    """A ledger reading: the named pools walled, room known on the rest unless named unknown."""
    def __init__(self, walled=(), unknown=()):
        self._walled, self._unknown = set(walled), set(unknown)
        self.quota = type('Quota', (), {'room_known': lambda _, name, *, scope=None: name not in self._unknown})()

    def walled(self, name, ship=None, *, scope=None):
        return name in self._walled

    def usage_known(self, name, *, scope=None):
        return name not in self._unknown

    def catching_up(self):
        return False

    def close(self):
        pass


@pytest.fixture
def listed(ship: shipmod.Ship) -> shipmod.Ship:
    """The example ship's worker declared on a plain list of two pools, as the office seats are."""
    file = ship.folder / "ship.toml"
    text = file.read_text().replace('[pools.fake]\nprovider = "synthetic"',
                                    '[pools.fake]\nprovider = "synthetic"\n\n[pools.spare]\nprovider = "synthetic"', 1)
    start = text.index('[seats.worker-1]')
    text = text[:start] + text[start:].replace('pool = "fake"', 'pool = ["fake", "spare"]', 1)
    file.write_text(text)
    return shipmod.load(ship.folder)


def _saved_on_spare(ship, tmux):
    seat = seatmod.Seat(ship, ship.seat("worker-1"), tmux, launching=True)
    assert seat.spec.pools == ("fake", "spare") and not seat.spec.pool_choices, "a plain list, no per-pool table"
    seat.move_to("spare")  # as the service's failover leaves a seat it moved
    return seatmod.Seat(ship, ship.seat("worker-1"), tmux, launching=True)


def test_a_down_seat_on_a_plain_list_leaves_its_spent_saved_pool_for_the_first_with_room(
        listed: shipmod.Ship, tmux: Tmux, monkeypatch):
    from quarterdeck import ledger
    monkeypatch.setattr(ledger.Ledger, "reading", classmethod(lambda cls, path: _Book(walled={"spare"})))
    seat = _saved_on_spare(listed, tmux)
    assert seat.pool.name == "spare"
    proof = seat.up(resume=False)
    assert proof.startswith("worker-1: launched on fake (the first declared choice with room, before its saved "
                            "spare); its saved choice was spare"), proof
    assert json.loads((seat.trusted / "launch.json").read_text())["pool"] == "fake"
    assert (seat.trusted / "pool").read_text() == "fake\n"


def test_a_down_seat_keeps_its_saved_pool_where_the_first_ones_room_is_unknown(
        listed: shipmod.Ship, tmux: Tmux, monkeypatch):
    from quarterdeck import harness, ledger
    seat = _saved_on_spare(listed, tmux)
    monkeypatch.setattr(ledger.Ledger, "reading", classmethod(lambda cls, path: None))  # no ledger at all
    assert seat.launch_choice() == ("spare", "its saved choice")
    # A ledger that shows the first pool not walled, but whose room on it is not known: the provider's usage unread.
    class Reads:
        """The program's own reader, but one that reads its provider's usage, so room is known only from it."""
        background_usage, reads_usage, model_scoped_quota = False, True, False

        def __init__(self, real):
            self._real = real

        def __getattr__(self, name):
            return getattr(self._real, name)
    native = harness.native_for
    monkeypatch.setattr(harness, "native_for", lambda provider: Reads(native(provider)))
    monkeypatch.setattr(ledger.Ledger, "reading", classmethod(lambda cls, path: _Book(unknown={"fake"})))
    assert seat.launch_choice() == ("spare", "its saved choice")
    proof = seat.up(resume=False)
    assert proof.startswith("worker-1 up in") and "launched on" not in proof, proof
    assert json.loads((seat.trusted / "launch.json").read_text())["pool"] == "spare"
    seat.down()
    # With its saved pool spent too, it still never moves onto a pool whose room is unknown.
    monkeypatch.setattr(ledger.Ledger, "reading", classmethod(
        lambda cls, path: _Book(walled={"spare"}, unknown={"fake"})))
    assert seat.launch_choice() == ("spare", "saved choice retained; room unknown on fake")


def test_a_saved_pool_no_longer_declared_gives_way_to_the_first_declared_even_with_no_ledger(
        listed: shipmod.Ship, tmux: Tmux, monkeypatch):
    from quarterdeck import ledger
    _saved_on_spare(listed, tmux)
    file = listed.folder / "ship.toml"  # the Admiral takes spare out of the list, and declares another
    text = file.read_text().replace('pool = ["fake", "spare"]', 'pool = ["fake", "other"]', 1)
    file.write_text(text.replace('[pools.spare]', '[pools.other]\nprovider = "synthetic"\n\n[pools.spare]', 1))
    changed = shipmod.load(listed.folder)
    monkeypatch.setattr(ledger.Ledger, "reading", classmethod(lambda cls, path: None))  # room known nowhere
    seat = seatmod.Seat(changed, changed.seat("worker-1"), tmux, launching=True)
    assert seat.launch_choice() == ("fake", "its saved spare is no longer declared; the first declared choice not "
                                            "shown walled, its room unknown")
    proof = seat.up(resume=False)
    assert proof.startswith("worker-1: launched on fake (its saved spare is no longer declared"), proof
    assert "its saved choice was spare" in proof
    assert json.loads((seat.trusted / "launch.json").read_text())["pool"] == "fake"


def test_qd_up_pool_on_a_plain_list_takes_a_declared_pool_and_refuses_another_in_true_words(
        listed: shipmod.Ship, tmux: Tmux):
    seat = seatmod.Seat(listed, listed.seat("worker-1"), tmux, launching=True)
    with pytest.raises(ShipError, match=r"--pool 'elsewhere' is not one of its declared choices \(fake, spare\)"):
        seat.up(resume=False, pool="elsewhere")
    proof = seat.up(resume=False, pool="spare")
    assert proof.startswith("worker-1: launched on spare (chosen for this launch by qd up --pool)"), proof
    assert json.loads((seat.trusted / "launch.json").read_text())["pool"] == "spare"
