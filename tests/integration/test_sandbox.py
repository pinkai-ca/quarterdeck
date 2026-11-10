"""A jailed seat: the fake harness under bubblewrap writes the workspace, posts from inside, and cannot reach the home."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from quarterdeck import post, sandbox, seat as seatmod, ship as shipmod
from quarterdeck.door import Door
from quarterdeck.tmux import Tmux

from .waiting import wait_for



def namespaces_allowed() -> bool:
    """Whether bubblewrap can make a namespace here: not inside a jail, whose kernel view denies nested ones."""
    import subprocess

    if not sandbox.available():
        return False
    try:
        return subprocess.run(["bwrap", "--ro-bind", "/", "/", "true"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


pytestmark = pytest.mark.skipif(not namespaces_allowed(), reason="bubblewrap is not installed, or cannot make a namespace here (inside a jail)")


def test_a_jailed_seat_writes_its_workspace_posts_from_inside_and_cannot_see_the_operators_home(drydock: Path, tmux: Tmux, tmp_path: Path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    text = (drydock / "ship.toml").read_text()
    text = shipmod.edit(text, "seats.worker-1", "sandbox", True)
    text = shipmod.edit(text, "seats.worker-1", "cwd", str(workspace))
    (drydock / "ship.toml").write_text(text + '\n[seats.worker-1.settings]\nstep_seconds = "0.05"\n')
    ship = shipmod.load(drydock)
    seat = seatmod.Seat(ship, ship.seat("worker-1"), tmux)
    secret = Path.home() / f".qd-test-secret-{os.getpid()}"
    marker = Path.home() / f".qd-test-escape-{os.getpid()}"
    secret.write_text("the operator's own\n")
    try:
        proof = seat.up(resume=False)
        assert "jailed: the operator's home hidden, the workspace writable" in proof
        launched = seat.launched()
        assert launched.argv[0] == "bwrap" and "--unshare-pid" in launched.argv and launched.harness == "fake"
        assert str(Path.home()) in launched.argv[launched.argv.index("--tmpfs", 12) + 1:], "the home is an empty tmpfs"
        wait_for(lambda: Door(seat).reading().open)
        store = post.Store(ship.runtime)
        body = ("Prove the jail.\n"
                f"$ echo inside > {workspace}/ok.txt\n"
                f"$ cat {secret} && echo secret-readable || echo secret-hidden\n"
                f"$ touch {marker}; echo touched=$?\n"
                "$ qd post report --re {this} jail proven")
        message = store.new("order", "desk", "worker-1", body, subject="Jail")
        delivery = Door(seat).deliver(post.typed([message]))
        assert delivery.result == "delivered", delivery.detail
        report = wait_for(lambda: next((m for m in store.all() if m.kind == "report" and m.re == message.id), None), 30)
        assert report.sender == "worker-1" and report.body == "jail proven", "qd post works from inside the jail"  # law: can-post
        assert (workspace / "ok.txt").read_text().strip() == "inside", "the workspace is writable"
        assert not marker.exists(), "a write into the operator's home landed in the jail's empty one, not on the machine"
        screen = tmux.capture(seat.pane().id, history=200)
        assert "⎿ secret-hidden" in screen and "⎿ secret-readable" not in screen, "the operator's files are absent, not merely unwritable"
        record = seat.record()
        assert record is not None and record.is_relative_to(seat.home), "the program's record is in the seat home, readable from outside"
        turns = wait_for(lambda: seat.harness.turns(record) or None)
        assert any(t.name == "Bash" for t in turns[0].tools), "the turn is accounted like any other"
    finally:
        secret.unlink(missing_ok=True)
        marker.unlink(missing_ok=True)
        seat.down()


def test_nested_synthetic_home_stays_hidden_while_allowed_mounts_work(tmp_path):
    import json
    import subprocess

    home = tmp_path/'operator-home'
    workspace = home/'project'
    folder = workspace/'ships'/'test'
    ship = shipmod.Ship(folder, 'synthetic', 'operator', workspace, {}, {}, {})
    own = ship.runtime/'seats'/'worker'
    program_home = own/'home'
    other = ship.runtime/'seats'/'other'
    for directory in (program_home, other):
        directory.mkdir(parents=True)
    (home/'operator-marker').write_text('synthetic operator marker')
    (other/'other-marker').write_text('synthetic other seat marker')
    jail = sandbox.plan(ship, 'worker', own, program_home, workspace, '/usr/bin/python3', '/usr/bin', home=home)
    probe = '''
import json, sys
from pathlib import Path
home, workspace, own, other, ship, post = map(Path, sys.argv[1:])
def write(path):
    try:
        path.write_text('synthetic')
        return True
    except OSError:
        return False
print(json.dumps(dict(operator_read=(home/'operator-marker').exists(),
                      other_read=(other/'other-marker').exists(),
                      workspace=write(workspace/'written'), own=write(own/'written'),
                      post=write(post/'written'), ship=write(ship/'written'),
                      hidden_home=write(home/'escape-marker'))))
'''
    result = subprocess.run(sandbox.command(jail, ['/usr/bin/python3', '-c', probe, *map(str, (
        home, workspace, own, other, folder, ship.runtime/'post'))]), capture_output=True, text=True, check=True)
    values = json.loads(result.stdout)
    assert not values['operator_read'] and not values['other_read']
    assert values['workspace'] and values['own'] and values['post'] and not values['ship']
    assert not (home/'escape-marker').exists(), 'the writable empty home must never reach the host home'
    assert (workspace/'written').exists() and (own/'written').exists() and (ship.runtime/'post'/'written').exists()
