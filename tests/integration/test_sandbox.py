"""A jailed seat: the fake harness under bubblewrap writes the workspace, posts from inside, and cannot reach the home."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from quarterdeck import post, sandbox, seat as seatmod, ship as shipmod
from quarterdeck.door import Door
from quarterdeck.tmux import Tmux

from .test_seat import wait_for

pytestmark = pytest.mark.skipif(not sandbox.available(), reason="bubblewrap is not installed on this machine")


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
