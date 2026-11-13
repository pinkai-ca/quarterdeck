"""Seats on the example ship: up, down, restart, fresh; homes, briefs, records; identity."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from quarterdeck import post, seat as seatmod, ship as shipmod
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
    launched = json.loads((runtime / "seats" / "captain" / "launch.json").read_text())
    assert launched["pane"].startswith("%") and launched["pool"] == "fake" and launched["resume"] is None
    env = environ(launched["pid"])
    assert env["QD_SHIP"] == str(ship.folder) and env["QD_SEAT"] == "captain"
    assert env["QD_FAKE_HOME"] == str(runtime / "seats" / "captain" / "home")
    assert env["PATH"].startswith(str(seatmod.qd_bin()))
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
    assert proof.endswith("1 open message carried"), "every open message, on a restart too"  # law: context-is-cache
    assert captain.record() == record, "the same conversation, in a new process"
    wait_for(lambda: standing_order.short in " ".join(t.input for t in captain.harness.turns(record)))
    assert "· 1 turns" in wait_for(lambda: tmux.capture(captain.pane().id) if "resumed" in tmux.capture(captain.pane().id) else None)
    proof = captain.fresh()
    assert "· fresh · 1 open message carried" in proof, "the still-open order comes along into the new conversation"
    assert captain.join().reason in ("no record yet", None), "a fresh start begins a new record"
    assert record.exists(), "the old record stays for the Ledger"


def test_a_fresh_seat_receives_its_open_messages_in_full(ship: shipmod.Ship, tmux: Tmux):
    store = post.Store(ship.runtime)
    order = store.new("order", "desk", "captain", "Count the stars.\n$ echo counted", subject="Stars")
    store.new("report", "captain", "desk", "an old report, not open", re=None)
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    captain.up(resume=False)
    record = wait_for(captain.record)
    state = wait_for(lambda: s if (s := captain.harness.state(record)).turns else None)  # law: context-is-cache
    first = captain.harness.turns(record)[0]
    assert first.input.startswith("qd > briefed as captain, captain · 1 order open\n")
    assert f"qd > order from desk · {order.short} · Stars\nCount the stars." in first.input
    assert f"qd > reply: qd post report --re {order.short}" in first.input
    assert [t.name for t in first.tools] == ["Bash"]
    assert "briefed: " in tmux.capture(captain.pane().id, history=100)


def test_a_launch_longer_than_tmux_allows_still_carries_every_open_message(ship: shipmod.Ship, tmux: Tmux):
    """tmux refuses a window command over 16 KiB; a brief or an open order can be longer, so the command goes in a file."""
    store = post.Store(ship.runtime)
    body = "A long order.\n" + "\n".join(f"paragraph {i}: " + " ".join(["words"] * 40) for i in range(80))
    assert len(body) > 16_384
    order = store.new("order", "desk", "captain", body)
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    assert captain.up(resume=False).endswith("1 open message carried")
    script = (ship.runtime / "seats" / "captain" / "launch.sh").read_text()
    assert script.startswith("#!/bin/sh\n") and "\nexec env -u TMUX -u TMUX_PANE " in script and " PATH=" in script and order.short in script and body[-200:] in script
    record = wait_for(captain.record)
    wait_for(lambda: captain.harness.state(record).turns)
    first = captain.harness.turns(record)[0]
    assert order.short in first.input and body.splitlines()[-1] in first.input, "the whole order reached the program"  # law: context-is-cache


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
    path = ship.runtime / "seats" / "captain" / "launch.json"
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
