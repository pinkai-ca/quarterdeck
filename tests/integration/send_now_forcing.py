"""Test-owned forcing of the race GitHub's slower runner showed in the send-now tests: a screen reading that
cannot see the fake's status line, taken while the fake's record has not yet ended the running turn.

Nothing of the fake changes. The fake is held at its own consumed rendezvous, after consumption and before its
turn can end, and the turn's end check runs there. tmux resets the pane's view, which the held fake does not
redraw, so no status line can be seen. The rendezvous is released by the first record read after that. A check
that trusts the screen therefore reads the record while it is behind, every time. A check that waits on the
record sees the turn end. Each step's effect is asserted before the check runs, so the forcing cannot pass for
done when it was not. (A process stop cannot hold the fake instead: tmux continues a stopped pane process.)
"""


def record_behind_a_blind_screen(tmux, seat, record, consumed, monkeypatch) -> None:
    assert consumed.reached() and consumed.request.exists(), "the fake is held at its consumed rendezvous"
    tmux.keys(seat.pane.id, "-R")
    shown = seat.screen()
    assert shown.box is None and shown.working is None, "the screen reading cannot see the status line: unknown, never idle"
    assert seat.harness.state(record).turn_open, "the turn is still open in the record"
    assert seat.harness.turns(record) == [], "the record has no ended turn"
    assert consumed.request.exists(), "the fake is still held"
    read = seat.harness.turns

    def turns(path):
        try:
            return read(path)
        finally:
            consumed.release()

    monkeypatch.setattr(seat.harness, "turns", turns)
