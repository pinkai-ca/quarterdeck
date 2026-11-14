"""Ordinary office declarations; every written ship is a throwaway folder."""
from pathlib import Path
import re

import pytest

from quarterdeck import fleet_post, ship, standing


TEMPLATE = Path(__file__).resolve().parents[2] / "ships" / "office"


def test_office_template_is_only_a_desk_with_its_own_local_card():
    office = ship.load(TEMPLATE)
    assert office.kind == "office"
    assert office.workspace == TEMPLATE
    assert list(office.seats) == ["desk"]
    assert office.session_of(office.seat("desk")).human
    assert not office.groups and office.cascade is None
    card, path = standing.card(office, "roles", "desk")
    assert path == TEMPLATE / "roles" / "desk.md"
    assert "fleet" in card


def test_office_desk_cross_ship_example_is_a_valid_cli_target(tmp_path):
    office_folder = tmp_path / "office"
    office_folder.mkdir()
    (office_folder / "ship.toml").write_text(office_text())
    destination = tmp_path / "ships" / "endeavour"
    destination.mkdir(parents=True)
    (destination / "ship.toml").write_text(
        (TEMPLATE.parent / "shuttle" / "ship.toml").read_text()
    )
    card, _ = standing.card(ship.load(TEMPLATE), "roles", "desk")
    example = re.search(r'`qd post order --to ([^ ]+@[^ ]+) "[^"]+"`', card)
    assert example is not None, "desk card must show a concrete cross-ship order"
    assert fleet_post.validate_target(ship.load(office_folder), example[1]) == (
        "captain", "endeavour"
    )
    assert "`seat@ship`" in card
    assert "same file" in card
    assert "own inbox" in card
    assert "`qd thread <id>`" in card


def office_text():
    return '''[ship]
kind = "office"
[pools.example]
provider = "fake"
[sessions.office]
human = true
[seats.desk]
session = "office"
role = "desk"
harness = "fake"
pool = "example"
'''


def test_office_accepts_person_declared_seats_and_uses_its_folder_without_a_workspace(tmp_path):
    text = office_text() + '''
[seats.counsel]
session = "office"
role = "counsel"
harness = "fake"
pool = "example"
up = false
'''
    (tmp_path / "ship.toml").write_text(text)
    office = ship.load(tmp_path)
    assert office.kind == "office"
    assert office.cwd_of(office.seat("counsel")) == tmp_path
    assert set(office.seats) == {"desk", "counsel"}


@pytest.mark.parametrize("before,after,reason", [
    ('kind = "office"', 'kind = "unknown"', 'ship.kind'),
    ('role = "desk"', 'role = "captain"', 'one seat named desk'),
    ('human = true', 'human = false', "person's sessions"),
])
def test_invalid_office_declarations_name_the_problem(tmp_path, before, after, reason):
    (tmp_path / "ship.toml").write_text(office_text().replace(before, after))
    with pytest.raises(ship.ShipError, match=reason):
        ship.load(tmp_path)


@pytest.mark.parametrize("role", ["captain", "engineering", "worker", "first-officer"])
def test_office_has_no_crew(tmp_path, role):
    (tmp_path / "ship.toml").write_text(office_text() + f'''
[seats.extra]
session = "office"
role = "{role}"
harness = "fake"
pool = "example"
''')
    with pytest.raises(ship.ShipError, match="no captain or crew"):
        ship.load(tmp_path)


def test_existing_ship_defaults_to_crew(tmp_path):
    (tmp_path / "ship.toml").write_text(office_text().replace('kind = "office"\n', ''))
    assert ship.load(tmp_path).kind == "crew"
