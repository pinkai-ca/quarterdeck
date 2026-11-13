"""The real shuttle declaration and installed resources, without native programs."""

from pathlib import Path
import shutil

from quarterdeck import resources, ship, standing, sync_all


ROOT = Path(__file__).resolve().parents[2]


def test_shuttle_template_is_excluded_but_its_copy_is_a_real_ship(tmp_path):
    ships = tmp_path / "ships"
    ships.mkdir()
    for name in ("shuttle", "mission-copy"):
        shutil.copytree(ROOT / "ships/shuttle", ships / name)
    assert ship.candidates(tmp_path) == ["mission-copy"]
    assert ship.default_ship(tmp_path) == ((ships / "mission-copy").resolve(), "only ship")
    rows = {row["ship"]: row for row in sync_all.plan(tmp_path)}
    assert rows["shuttle"]["disposition"] == "passed over"
    assert rows["shuttle"]["reason"] == "tracked template"
    assert rows["mission-copy"]["disposition"] == "not tried"
    assert rows["mission-copy"]["ship_object"].name == "shuttle"


def test_shuttle_loads_complete_local_briefs():
    folder = ROOT / "ships" / "shuttle"
    crew = ship.load(folder)
    assert set(crew.seats) == {"captain", "engineer", "first-officer"}
    assert not any(session.human for session in crew.sessions.values())
    engineer = crew.seat("engineer")
    officer = crew.seat("first-officer")
    assert crew.pools[engineer.pools[0]].provider != crew.pools[officer.pools[0]].provider
    for seat in crew.seats.values():
        brief = standing.brief(crew, seat)
        local = folder / "roles" / f"{seat.role}.md"
        assert ("role card", local) in brief.sources
        assert local.read_text().strip() in brief.text
        assert (folder / "orders.md").read_text().strip() in brief.text


def test_packaged_shuttle_roundtrips_with_its_cards_and_record_form(tmp_path, monkeypatch):
    files = resources.plan(ROOT)
    assert files is not None
    original = ROOT / "ships" / "shuttle"
    expected = {path.relative_to(ROOT) for path in original.rglob("*") if path.is_file()}
    assert expected <= set(files)
    share = tmp_path / "venv" / "share" / "quarterdeck"
    resources.synchronize(ROOT, share, files)
    installed = share / "ships" / "shuttle"
    monkeypatch.setattr(standing, "FRAMEWORK_DIR", share / "standing-orders")
    crew = ship.load(installed)
    assert (installed / "mission-record.md").read_bytes() == (original / "mission-record.md").read_bytes()
    for seat in crew.seats.values():
        brief = standing.brief(crew, seat)
        assert ("role card", installed / "roles" / f"{seat.role}.md") in brief.sources
        assert ("harness card", share / "standing-orders" / "harnesses" / f"{seat.harness}.md") in brief.sources
