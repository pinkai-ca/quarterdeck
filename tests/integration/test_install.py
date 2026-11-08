"""qd upgrade on the drydock: the checkout installed as the ship's own copy, and every qd handing off to it."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from quarterdeck import cli, doctor, install
from quarterdeck.ship import load
from quarterdeck.tmux import Tmux

ROOT = Path(__file__).resolve().parents[2]


def test_upgrade_installs_the_checkout_as_the_ships_own_copy_and_qd_hands_off_to_it(drydock: Path, tmux: Tmux, monkeypatch):
    ship = load(drydock)
    head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    assert "runs the checkout at" in doctor.framework_check(ship).detail
    proofs = install.upgrade(ship, "HEAD", tmux)
    installed_qd = drydock / "runtime" / "venv" / "bin" / "qd"
    assert installed_qd.exists() and proofs[0].startswith(f"installed {head[:7]} (HEAD) into runtime/venv · qd ")
    assert "the ship ran the checkout before" in proofs[0] and "removing runtime/venv returns it to the checkout" in proofs[0]
    assert proofs[1] == "service not running; qd up starts it on the installed copy"
    assert proofs[-1] == "every qd typed against this ship now runs the installed copy"
    record = json.loads((drydock / "runtime" / "installed.json").read_text())
    assert record["commit"] == head and record["previous"] is None
    assert not (drydock / "runtime" / "upgrade").exists(), "the staged source is gone once installed"
    assert subprocess.run([str(installed_qd), "--version"], capture_output=True, text=True).stdout.startswith("qd ")
    assert f"runs its installed copy, commit {head[:7]}" in doctor.framework_check(ship).detail

    # the installed copy answers for the ship itself, and a qd typed from this checkout reaches it and answers too
    env = {**os.environ, "QD_TMUX_SOCKET": tmux.socket}
    env.pop("QD_SHIP", None)
    own = subprocess.run([str(installed_qd), "--ship", str(drydock), "status", "--line"], env=env, capture_output=True, text=True, timeout=60)
    assert own.returncode == 0 and own.stdout.startswith("SERVICE DOWN"), own.stderr
    typed = subprocess.run([str(ROOT / ".venv" / "bin" / "qd"), "--ship", str(drydock), "doctor"], env=env, capture_output=True, text=True, timeout=120)
    assert f"runs its installed copy, commit {head[:7]}" in typed.stdout, typed.stderr

    # qd typed from this checkout hands off to the installed copy before doing anything
    calls = []

    def execv(path, argv):
        calls.append((path, argv))
        raise SystemExit(0)

    monkeypatch.setattr(install.os, "execv", execv)
    with pytest.raises(SystemExit):
        cli.main(["--ship", str(drydock), "status"])
    assert calls == [(str(installed_qd), [str(installed_qd), "--ship", str(drydock), "status"])]
    assert not install.running_from(installed_qd.parent), "the test process is not the installed copy"

    # upgrade itself never hands off, and a second upgrade names the commit it replaced
    calls.clear()
    proofs = install.upgrade(ship, "HEAD", tmux)
    assert calls == [] and f"was {head[:7]}" in proofs[0] and f"qd upgrade {head[:7]} rolls back" in proofs[0]
    assert json.loads((drydock / "runtime" / "installed.json").read_text())["previous"] == head


def test_a_ship_without_a_repository_beside_it_is_told_where_to_run_upgrade(tmp_path: Path, monkeypatch):
    from quarterdeck.ship import Pool, SeatSpec, Session, Ship, ShipError

    ship = Ship(tmp_path / "ships" / "lone", "lone", "operator", tmp_path, {"fake": Pool("fake", "fake")},
                {"bridge": Session("bridge", "Bridge")}, {"captain": SeatSpec("captain", "bridge", "captain", "fake", ("fake",))})
    monkeypatch.setattr(install, "source_repo", lambda ship: (_ for _ in ()).throw(ShipError("no framework repository beside the ship")))
    with pytest.raises(ShipError, match="no framework repository"):
        install.upgrade(ship, "HEAD")
    assert "the ship runs this qd; no framework repository" in doctor.framework_check(ship).detail
