"""The systems seat's guard on a Grok seat: a question shown above Grok's own composer is read and refused.

Grok draws its composer as a rounded frame ('  ╭─…─╮'), the form the guard takes for a dialog's top. The guard
passes over that one frame, which the adapter's own screen reader names, and reads the words above it. The screens
are the measured 1.0.77 idle composer (tests/fixtures/grok/1.0.77/idle-mono.json) with security's dialog samples
drawn above it, synthetic, through a stand-in tmux; nothing is sent to any program.
"""

import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import acts, door as doormod, post
from quarterdeck.harness import grok_screen
from quarterdeck.harness.grok import GrokHarness
from quarterdeck.harness.types import Screen
from quarterdeck.service import Service
from quarterdeck.ship import Pool, SeatSpec, Session, Ship
from quarterdeck.tmux import Pane
from tests.unit import test_systems_acts

DIALOGS = test_systems_acts.LAAN_DIALOGS  # security's samples of real programs' forms

IDLE = json.loads((Path(__file__).parents[1] / "fixtures/grok/1.0.77/idle-mono.json").read_text())["lines"]
TOP = grok_screen.composer_rows(GrokHarness().screen("\n".join(IDLE) + "\n").text.split("\n"))[0]


def above_composer(dialog: str) -> str:
    """The measured idle screen with ``dialog`` drawn in the rows right above Grok's composer."""
    rows = list(IDLE)
    rows[TOP:TOP] = ["  " + row for row in dialog.split("\n")] + [""]
    return "\n".join(rows) + "\n"


def screen(text: str) -> Screen:
    return GrokHarness().screen(text)


@pytest.fixture
def ship(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pool = Pool("test", "xai")
    seats = {"worker": SeatSpec("worker", "bridge", "worker", "grok", ("test",), cwd=tmp_path),
             "systems-seat": SeatSpec("systems-seat", "bridge", "systems", "grok", ("test",), cwd=tmp_path),
             "desk": SeatSpec("desk", "office", "desk", "grok", ("test",), cwd=tmp_path)}
    ship = Ship(tmp_path, "example", "operator", tmp_path, {"test": pool},
                {"bridge": Session("bridge", "Test"), "office": Session("office", "Office", human=True)}, seats)
    live = {"pane": Pane("%1", "Test", "worker", 123, 80, 24, False, False, None), "screen": above_composer(""),
            "sent": []}
    tmux = SimpleNamespace(pane=lambda p: live["pane"], capture=lambda *a, **k: live["screen"],
                           paste=lambda pane, text, bracketed=True: live["sent"].append(("paste", text)),
                           keys=lambda pane, *keys: live["sent"].append(("keys", keys)),
                           click=lambda pane, x, y: live["sent"].append(("click", (x, y))),
                           mouse_modes=lambda pane, pid: None)
    svc = Service(ship, tmux)
    seat = svc.seat("worker")
    seat.folder.mkdir(parents=True)
    seat.trusted.mkdir(parents=True, exist_ok=True)
    launch = {"pane": "%1", "pid": 123, "at": (post.now() - timedelta(minutes=1)).isoformat(),
              "pool": "test", "argv": ["grok"], "harness": "grok", "resume": None, "version": "1.0.77"}
    (seat.trusted / "launch.json").write_text(json.dumps(launch))
    monkeypatch.setattr(doormod, "BOX_SECONDS", 0.3)
    monkeypatch.setattr(doormod.Door, "confirm", lambda self, text, seconds=0: doormod.Delivery("blind", "no record"))
    yield svc, seat, live
    svc.ledger.close()


def test_the_measured_composer_is_read_as_grok_reads_it():
    plain = screen(above_composer(""))
    assert plain.box is not None and not plain.box.strip() and plain.unknown is None
    rows = plain.text.split("\n")
    top, bottom = grok_screen.composer_rows(rows)
    assert rows[top].lstrip().startswith("╭") and "Grok 4.7 (high)" in rows[bottom]


@pytest.mark.parametrize("name", sorted(DIALOGS))
def test_each_security_dialog_above_a_grok_composer_is_refused_to_a_typed_line(ship, name):
    svc, seat, live = ship
    live["screen"] = above_composer(DIALOGS[name])
    shown = screen(live["screen"])
    assert shown.box is not None and not shown.box.strip(), "the composer is still read, and empty"
    assert acts.guard(shown, line="continue", program="grok") is not None
    line = svc.answer(dict(kind="type", seat="worker", line="continue", caller="systems-seat"))
    assert "its screen reads as a question" in line and live["sent"] == [], line


def test_a_plain_grok_composer_with_no_dialog_still_takes_a_line(ship):
    svc, seat, live = ship
    assert acts.guard(screen(live["screen"]), line="continue", program="grok") is None
    line = svc.answer(dict(kind="type", seat="worker", line="continue", caller="systems-seat"))
    assert ("paste", "continue") in live["sent"], line


def test_a_dialog_framed_exactly_like_the_composer_is_still_read():
    """A dialog drawn in a frame of the composer's own form, above the composer: the adapter names the lower
    frame, the one right above the footer, as its own; the dialog's frame above it is where the words start."""
    framed = "╭────────────────────╮\n│ Run npm test?      │\n│ 1. Yes   2. No     │\n╰────────────────────╯"
    shown = screen(above_composer(framed))
    rows = shown.text.split("\n")
    own = grok_screen.composer_rows(rows)
    assert own is not None and "Run npm test?" not in "\n".join(rows[own[0]:own[1] + 1])
    assert "the dialog's frame says 'Run'" in acts.guard(shown, line="continue", program="grok")


def test_where_the_adapter_did_not_read_its_composer_its_rows_are_read_too():
    """A composer the reader could not read as its own (an overlaid row): its top is still passed over, and its
    rows are read with everything else, so nothing drawn inside it is left out."""
    rows = above_composer("").split("\n")
    inner = next(i for i in range(TOP + 1, len(rows)) if "│ ❯" in rows[i])
    rows[inner] = rows[inner].replace("│ ❯ ", "│ Allow access? ")
    shown = screen("\n".join(rows))
    assert shown.box is None
    reason = acts.guard(shown, line="continue", program="grok")
    assert reason is not None and "the whole screen says" in reason


def test_every_other_program_reads_from_the_nearest_frame_as_before():
    text = above_composer(DIALOGS["hooks"])
    plain = screen(text).text
    for program in (None, "codex", "claude", "pi", "opencode", "antigravity", "fake"):
        assert acts.own_frame(program, plain.split("\n")) is None
        rows = plain.split("\n")
        at = max(y for y, row in enumerate(rows) if row.strip())
        assert acts.region(rows, at) == (rows[TOP + len(DIALOGS["hooks"].split("\n")) + 1:], "the dialog's frame")
