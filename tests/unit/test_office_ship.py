"""Ordinary office declarations; every written ship is a throwaway folder."""
from pathlib import Path

import pytest

from quarterdeck import ship, standing


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


@pytest.mark.parametrize("name", ["roles/desk.md", "orders.md"])
def test_office_template_labels_the_example_and_points_to_the_current_addresses(name):
    text = (TEMPLATE / name).read_text()
    assert 'placeholder example' in text
    assert '`qd post order --to seat-name@ship-folder "Review the README."`' in text
    assert "`qd brief desk`" in text
    assert "`seat@ship`" in text
    assert "same file" in text
    assert "own inbox" in text
    assert "`qd thread <id>`" in text


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
