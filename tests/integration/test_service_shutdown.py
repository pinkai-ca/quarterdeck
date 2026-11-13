"""Ordinary SIGTERM phase boundaries on private synthetic programs."""

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from quarterdeck import install, pending, servicectl, ship as shipmod, shutdown
from quarterdeck.door import Door
from quarterdeck.harness.synthetic_control import events
from quarterdeck.post import Attempts, Store
from quarterdeck.seat import Seat
from quarterdeck.tmux import Tmux


MARKER = "synthetic-stop-payload-zzzz"
DRIVER = r'''
import os, signal, sys, time
from pathlib import Path
from quarterdeck import door, pending, service, ship, shutdown
from quarterdeck.tmux import Tmux
folder, phase = Path(sys.argv[1]), sys.argv[2]
runtime = folder / 'runtime'
used = False
def pause(where, proof):
    global used
    if used or where != phase or 'synthetic-stop-payload-zzzz' not in proof.data.get('payload', ''):
        return
    used = True
    (runtime / 'phase-hit').write_text(where)
    while not shutdown.requested():
        time.sleep(.005)
    (runtime / 'term-seen').touch()
    while phase == 'forced' or not (runtime / 'phase-release').exists():
        time.sleep(.005)
save = pending.Proof.save
def saved(self, **fields):
    save(self, **fields)
    if fields.get('phase') in ('prepared', 'pasting', 'submitting', 'submitted'):
        pause(fields['phase'], self)
pending.Proof.save = saved
paste = Tmux.paste
def pasted(self, *args, **kwargs):
    result = paste(self, *args, **kwargs)
    if phase in ('pasted', 'forced') and not used:
        # The current door's journal is the one for this exact synthetic payload.
        from quarterdeck.shutdown import _controller, inflight
        current = inflight(runtime, _controller.owner)
        if current:
            proof = pending.Proof(runtime / current['journal'], __import__('json').loads((runtime / current['journal']).read_text()))
            pause(phase, proof)
    return result
Tmux.paste = pasted
shows = door.Door.box_shows
def checked(self, *args, **kwargs):
    result = shows(self, *args, **kwargs)
    if self.proof is not None:
        pause('checked', self.proof)
    return result
door.Door.box_shows = checked
confirm = door.Door.confirm
def confirming(self, *args, **kwargs):
    if shutdown.requested():
        (runtime / 'confirmation-after-stop').touch()
    return confirm(self, *args, **kwargs)
door.Door.confirm = confirming
service.Service(ship.load(folder), Tmux.default()).run()
'''


def wait_for(predicate, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.01)
    raise AssertionError("synthetic shutdown observation did not arrive")


@pytest.fixture
def world(tmp_path, monkeypatch):
    folder = tmp_path / "ship"
    (folder / "roles").mkdir(parents=True)
    (folder / "roles/captain.md").write_text("Synthetic shutdown exercise.\n")
    (folder / "ship.toml").write_text('''[ship]
name = "synthetic-stop-zzzz"
operator = "synthetic-operator-zzzz"
[pools.synthetic]
provider = "synthetic"
[sessions.crew]
title = "Synthetic shutdown"
[seats.synthetic-seat-zzzz]
session = "crew"
role = "captain"
harness = "fake"
pool = "synthetic"
model = "fake-1"
effort = "medium"
[seats.synthetic-seat-zzzz.settings]
steps = 0
step_seconds = 0
''')
    ship = shipmod.load(folder)
    shipmod.fence(ship)
    tmux = Tmux.private(tmp_path / "socket")
    monkeypatch.setenv("QD_TMUX_SOCKET", str(tmux.socket))
    monkeypatch.setenv("QD_SHIP", str(folder))
    monkeypatch.setenv("QD_SEAT", "synthetic-seat-zzzz")
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.setenv("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    monkeypatch.setenv("PIP_CACHE_DIR", str(tmp_path / "pip-cache"))
    seat = Seat(ship, ship.seat("synthetic-seat-zzzz"), tmux)
    children = []
    seat.up(resume=False)
    wait_for(lambda: Door(seat).reading().open)

    class World:
        def start(self, phase=None):
            if phase is None:
                return servicectl.start(ship, qd=Path(sys.executable).parent / "qd")
            with (folder / "driver-output.txt").open("w") as output:
                child = subprocess.Popen([sys.executable, "-c", DRIVER, str(folder), phase],
                    stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            children.append(child)
            wait_for(lambda: servicectl.started(ship.runtime))

        def post(self, body=MARKER):
            return Store(ship.runtime).new("order", "ship", seat.name, body)

        def journal(self):
            files = list((ship.runtime / "post/pending" / seat.name).glob("*.json"))
            return json.loads(files[0].read_text()) if files else None

        def submitted(self):
            return sum(row["event"] == "submitted" and MARKER in row["text"] for row in events(seat.home))

        def progress(self, original):
            wait_for(lambda: Attempts(ship.runtime).latest().get(original.id) == "delivered")
            followup = self.post("synthetic-followup-zzzz")
            wait_for(lambda: Attempts(ship.runtime).latest().get(followup.id) == "delivered")
            assert self.submitted() == 1

    result = World()
    result.ship, result.seat, result.tmux = ship, seat, tmux
    try:
        yield result
    finally:
        (ship.runtime / "phase-release").touch()
        if servicectl.alive(ship.runtime):
            servicectl.stop(ship.runtime)
        for child in children:
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)
        tmux.close()


def stopping(call):
    results, errors = [], []
    def run():
        try:
            results.append(call())
        except BaseException as error:
            errors.append(error)
    thread = threading.Thread(target=run)
    thread.start()
    return thread, results, errors


@pytest.mark.parametrize("phase", ["prepared", "pasting", "pasted", "checked", "submitting", "submitted"])
def test_sigterm_finishes_exact_phase_then_restart_delivers_once(world, phase):
    world.start(phase)
    original = world.post()
    runtime = world.ship.runtime
    wait_for(lambda: (runtime / "phase-hit").exists())
    thread, results, errors = stopping(lambda: servicectl.stop(runtime))
    wait_for(lambda: (runtime / "term-seen").exists())
    assert thread.is_alive()
    (runtime / "phase-release").touch()
    thread.join(timeout=10)
    assert not thread.is_alive() and not errors
    journal = world.journal()
    assert journal["phase"] == ("cancelled" if phase == "prepared" else "submitted")
    assert "delivery" in results[0] and "waited" in results[0]
    assert not (runtime / "confirmation-after-stop").exists()
    assert world.submitted() == 0 if phase == "prepared" else world.submitted() <= 1
    world.start()
    world.progress(original)


def test_forced_cut_is_named_and_remains_unsubmitted(world, monkeypatch):
    monkeypatch.setattr(shutdown, "ORDINARY_GRACE", .1)
    monkeypatch.setattr(shutdown, "DELIVERY_GRACE", .3)
    world.start("forced")
    world.post()
    runtime = world.ship.runtime
    wait_for(lambda: (runtime / "phase-hit").exists())
    proof = servicectl.stop(runtime)
    journal = world.journal()
    assert "killed" in proof and "cut at pasting" in proof and "0.3s" in proof
    assert journal["phase"] == "pasting" and journal["service_stop"]["kind"] == "forced"
    assert world.submitted() == 0
    world.start()
    wait_for(lambda: servicectl.started(runtime))
    assert pending.active(world.seat)
    assert world.submitted() == 0


def test_upgrade_stop_waits_midpaste_and_new_service_does_not_repeat(world):
    world.start("pasted")
    original = world.post()
    runtime = world.ship.runtime
    wait_for(lambda: (runtime / "phase-hit").exists())
    thread, results, errors = stopping(lambda: install.upgrade(world.ship, "HEAD", world.tmux))
    wait_for(lambda: (runtime / "term-seen").exists())
    assert thread.is_alive()
    (runtime / "phase-release").touch()
    thread.join(timeout=90)
    assert not thread.is_alive() and not errors
    assert any("waited" in line and "delivery boundary" in line for line in results[0])
    world.progress(original)
