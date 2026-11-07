"""Seats on the example ship: up, down, restart, fresh; homes, briefs, records; identity."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from quarterdeck import post, seat as seatmod, ship as shipmod
from quarterdeck.ship import ShipError
from quarterdeck.tmux import Tmux


def wait_for(predicate, seconds: float = 10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


@pytest.fixture
def ship(example_ship: Path) -> shipmod.Ship:
    text = (example_ship / "ship.toml").read_text() + '\n[seats.captain.settings]\nstep_seconds = "0.05"\n'
    (example_ship / "ship.toml").write_text(text)
    return shipmod.load(example_ship)


def environ(pid: int) -> dict[str, str]:
    raw = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    return dict(item.decode().partition("=")[::2] for item in raw if item)


def test_up_brings_up_every_declared_seat_in_its_session(ship: shipmod.Ship, tmux: Tmux):
    proofs = seatmod.bring_up(ship, tmux)
    assert proofs[0] == "runtime/ fenced: nested repository created"
    assert "not its own repository" in proofs[1]
    assert [p.split(" ")[0] for p in proofs[2:]] == ["captain", "desk", "worker-1"]
    assert all("· fake fake · pool fake · fresh" in p for p in proofs[2:])
    assert sorted(tmux.sessions()) == ["Bridge", "Crew", "Office"]
    assert [p.window for p in tmux.panes("Bridge")] == ["Pike"]
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
    assert tmux.run("show-option", "-v", "-t", "=Bridge:", "status-right").strip().endswith(f"status --line --ship {ship.folder})")
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
    proof = captain.restart()
    assert proof.splitlines()[0].startswith("captain down · pane %") and " · resumed " in proof
    assert captain.record() == record, "the same conversation, in a new process"
    assert "· 1 turns" in wait_for(lambda: tmux.capture(captain.pane().id) if "resumed" in tmux.capture(captain.pane().id) else None)
    proof = captain.fresh()
    assert proof.endswith("· fresh")
    assert captain.join().reason == "no record yet", "a fresh start begins a new record"
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


def test_bring_down_closes_the_sessions(ship: shipmod.Ship, tmux: Tmux):
    seatmod.bring_up(ship, tmux)
    proofs = seatmod.bring_down(ship, tmux)
    assert all("down · pane" in p for p in proofs) and tmux.sessions() == []
