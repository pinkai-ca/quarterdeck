"""Standing orders: the laws page, the cards and the brief."""

from __future__ import annotations

from pathlib import Path

from quarterdeck import ship as shipmod, standing


def test_the_laws_are_few_grouped_and_identified():
    laws = standing.laws()
    ids = [law.id for law in laws]
    assert len(ids) == len(set(ids)) == 14
    assert {law.part for law in laws} == {"Ship", "Harness", "Seat", "Watch", "Post", "Ledger"}
    one_door = next(law for law in laws if law.id == "one-door")
    assert one_door.part == "Seat" and one_door.text.startswith("Everything typed into a seat goes through one path")


def test_the_brief_is_built_from_files_the_user_can_edit(drydock: Path):
    ship = shipmod.load(drydock)
    brief = standing.brief(ship, ship.seat("captain"))
    assert brief.text.startswith("# Standing orders for Picard: captain, captain, on drydock")
    assert "`ship-file`" in brief.text and "# Ship's orders: drydock" in brief.text
    assert "# Role: captain" in brief.text and "# Program: the fake harness" in brief.text
    assert "You are seat `captain`" in brief.text and "typed into your pane in full" in brief.text
    names = [name for name, _ in brief.sources]
    assert names == ["laws", "ship's orders", "role card", "harness card", "your seat"]
    assert dict(brief.sources)["ship's orders"] == drydock / "orders.md"
    assert dict(brief.sources)["role card"] == standing.FRAMEWORK_DIR / "roles" / "captain.md"
    assert brief.words < standing.BRIEF_WORDS


def test_a_human_session_seat_is_told_about_its_watch(drydock: Path):
    ship = shipmod.load(drydock)
    brief = standing.brief(ship, ship.seat("desk"))
    assert "qd inbox --wait" in brief.text and "nothing is ever typed into your pane" in brief.text
    assert "typed into your pane in full" not in brief.text


def test_a_ship_replaces_a_card_at_the_same_path(drydock: Path):
    (drydock / "roles").mkdir()
    (drydock / "roles" / "worker.md").write_text("# Role: worker\n\nThis ship's own worker card.\n")
    ship = shipmod.load(drydock)
    brief = standing.brief(ship, ship.seat("worker-1"))
    assert "This ship's own worker card." in brief.text
    assert dict(brief.sources)["role card"] == drydock / "roles" / "worker.md"
    path = standing.render(ship, ship.seat("worker-1"))
    assert path == drydock / "runtime" / "brief" / "worker-1.md" and path.read_text() == brief.text


def test_a_law_named_in_prose_is_mentioned_not_carried(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "a.py").write_text('"""The door (law: one-door) is one path."""\nx = 1  # law: identity\n')
    (tmp_path / "tests" / "b.py").write_text("assert x  # law: identity\n")
    found = standing.carriers(tmp_path)
    assert set(found) == {"identity"}, "a tag counts only in a comment"
    assert found["identity"] == [(Path("src/a.py"), 2), (Path("tests/b.py"), 1)]


def test_a_missing_card_is_said_not_hidden(drydock: Path):
    text = (drydock / "ship.toml").read_text().replace('role = "worker"', 'role = "navigator"')
    (drydock / "ship.toml").write_text(text)
    ship = shipmod.load(drydock)
    brief = standing.brief(ship, ship.seat("worker-1"))
    assert "(no role card for navigator)" in brief.text and dict(brief.sources)["role card"] is None
