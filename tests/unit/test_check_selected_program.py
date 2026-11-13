"""qd check names the program an adapter settles once, proves its seat ran that path, and marks it proven."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import check
from quarterdeck.drill import Check
from quarterdeck.harness.fake import FakeHarness
from quarterdeck.seat import Seat
from quarterdeck.tmux import Pane
from .test_check_lifetime import synthetic_ship
from .test_removed_delivery import launch


@pytest.mark.parametrize("ran", [True, False])
def test_the_check_proves_its_seat_ran_the_selected_program(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ran):
    ship = synthetic_ship(tmp_path)
    selected = tmp_path / "bin" / "fake"
    sessions = set()
    tmux = SimpleNamespace(has_session=lambda name: name in sessions, kill_session=lambda name: sessions.discard(name),
                           new_session=lambda name, **kwargs: (sessions.add(name) or name), status_line=lambda *a: None,
                           kill_session_id=lambda name: sessions.discard(name),
                           pane=lambda ident: Pane(ident, "Synthetic", "worker", 222, 80, 24, False, False, None))
    monkeypatch.setattr(FakeHarness, "version", lambda self: "synthetic")
    monkeypatch.setattr(FakeHarness, "selected_program", lambda self: selected, raising=False)

    def probe(checked, seat, orders):
        launched = launch(seat, "%2", 222)
        argv = [str(selected), "--synthetic"] if ran else ["fake", "--synthetic"]
        replace(launched, argv=argv).write(seat.folder / "launch.json")
        return [Check("synthetic probe", True)]

    monkeypatch.setattr(check, "probe", probe)
    monkeypatch.setattr(check, "probe_wake", lambda seat, orders: [Check("synthetic wake probe", True)])
    monkeypatch.setattr(Seat, "down", lambda seat: "synthetic stop")
    results = check.check(ship, "fake", tmux)
    assert f"runs {selected}" in results[0].detail, "the installed row names the program, as doctor does"
    row = next(r for r in results if r.name == "the seat ran the program doctor shows")
    assert row.passed is ran
    mark = ship.runtime / "proven" / "fake-synthetic"
    if ran:
        assert mark.read_text().splitlines()[1] == str(selected), "the proven mark names the program it proved"
    else:
        assert not mark.exists(), "a seat that ran another program proves nothing"


def test_an_adapter_without_one_selection_keeps_its_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ship = synthetic_ship(tmp_path)
    sessions = set()
    tmux = SimpleNamespace(has_session=lambda name: name in sessions, kill_session=lambda name: sessions.discard(name),
                           new_session=lambda name, **kwargs: (sessions.add(name) or name), status_line=lambda *a: None,
                           kill_session_id=lambda name: sessions.discard(name),
                           pane=lambda ident: Pane(ident, "Synthetic", "worker", 222, 80, 24, False, False, None))
    monkeypatch.setattr(FakeHarness, "version", lambda self: "synthetic")
    monkeypatch.setattr(check, "probe", lambda checked, seat, orders: [Check("synthetic probe", True)])
    monkeypatch.setattr(check, "probe_wake", lambda seat, orders: [Check("synthetic wake probe", True)])
    monkeypatch.setattr(Seat, "down", lambda seat: "synthetic stop")
    results = check.check(ship, "fake", tmux)
    assert "runs " not in results[0].detail
    assert not any(r.name == "the seat ran the program doctor shows" for r in results)
    assert (ship.runtime / "proven" / "fake-synthetic").read_text().count("\n") == 1
