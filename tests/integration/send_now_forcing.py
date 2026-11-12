"""Test-owned forcing of the race GitHub's slower runner showed in the send-now tests: a screen reading that
cannot see the fake's status line, taken while the fake's record has not yet ended the running turn.

Nothing of the fake changes. The fake is stopped where it waits at its own reached rendezvous, after consumption
and before its turn can end; tmux resets the pane's view, which the stopped fake cannot redraw; and the fake is
continued only after the next record read. A check that trusts the screen then reads the record while it is
behind, every time; a check that waits on the record sees the turn end. Each step's effect is asserted before
either check runs, so the forcing cannot pass for done when it was not.
"""

import os
from pathlib import Path
import signal

from .test_fakeharness import wait_for


def stopped(pid: int) -> bool:
    stat = Path(f"/proc/{pid}/stat").read_text()
    return stat[stat.rindex(")") + 2] == "T"


def resume(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGCONT)
    except ProcessLookupError:
        pass


def park(rendezvous, request) -> int:
    """Stop the fake waiting at a reached rendezvous; it is always continued by the end of the test."""
    pid = rendezvous.reached()["pid"]
    os.kill(pid, signal.SIGSTOP)
    request.addfinalizer(lambda: resume(pid))
    wait_for(lambda: stopped(pid))
    return pid


def record_behind_a_blind_screen(tmux, seat, record, pid: int, monkeypatch) -> None:
    tmux.keys(seat.pane.id, "-R")
    shown = seat.screen()
    assert shown.box is None and not shown.working, "the screen reading cannot see the status line"
    assert stopped(pid) and seat.harness.state(record).turn_open, "the turn is still open in the record"
    assert seat.harness.turns(record) == [], "the record has no ended turn"
    read = seat.harness.turns

    def turns(path):
        try:
            return read(path)
        finally:
            resume(pid)

    monkeypatch.setattr(seat.harness, "turns", turns)
