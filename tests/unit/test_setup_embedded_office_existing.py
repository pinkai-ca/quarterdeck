"""A person whose office still lives in a ship, with no office folder, keeps or updates an existing ship with setup;
setup then makes no office and moves nothing. A first setup with no ship named still refuses, naming that ship. With
an office folder, all is as before. A throwaway project under tmp_path; every operation scripted."""
import argparse
from pathlib import Path

import pytest

from quarterdeck import setup, ship
from quarterdeck.ship import ShipError
from .test_setup_flow import ScriptedOperations, answers, execute  # noqa: F401 -- the fixture, by name


def first_step(given, operations=None):
    lines = []
    operations = operations or ScriptedOperations()
    conversation = setup.Conversation(given, interactive=False, write=lines.append)
    return setup.first_step(argparse.Namespace(json=False), operations=operations, conversation=conversation), operations


def tree(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob('*')) if p.is_file()}


@pytest.fixture
def embedded(answers, tmp_path):  # noqa: F811
    """A ship created before offices existed: its desk sits in a human session, and there is no office folder."""
    execute(answers)
    current = ship.load(Path(answers['folder']))
    assert any(seat.role == 'desk' and current.session_of(seat).human for seat in current.seats.values())
    assert not (tmp_path / 'office').exists()
    return answers


def test_keep_on_the_ship_that_holds_the_desk_succeeds_and_writes_nothing(embedded, tmp_path):
    before = tree(tmp_path)
    result, operations = first_step({**embedded, 'existing': 'keep'})
    assert tree(tmp_path) == before, 'byte for byte: no office, nothing moved'
    assert not (tmp_path / 'office').exists()
    assert 'up' in operations.calls and any('Desk access' in line for line in result)


def test_update_on_the_ship_that_holds_the_desk_updates_its_crew_and_makes_no_office(embedded, tmp_path):
    orders = (Path(embedded['folder']) / 'orders.md').read_bytes()
    first_step({**embedded, 'existing': 'update', 'payment': {'codex': 'key'}, 'provider': {'codex': 'openai'},
                'key_env': {'codex': 'example-setup-key'}})
    current = ship.load(Path(embedded['folder']))
    assert current.pool(current.seat('captain').pools[0]).kind == 'key', 'the crew updated'
    assert (current.folder / 'orders.md').read_bytes() == orders
    assert not (tmp_path / 'office').exists(), 'no office made; the desk stays in its ship'


@pytest.mark.parametrize('given', [
    dict(accept='yes', start='yes'),  # a bare first setup
    dict(use_defaults='yes', desk='codex', accept='yes', start='yes'),  # the office's own answers alone
])
def test_a_first_setup_with_no_ship_named_still_refuses_naming_that_ship(embedded, tmp_path, given):
    before = tree(tmp_path)
    with pytest.raises(ShipError, match='office still lives in ship crew'):
        first_step(given)
    assert tree(tmp_path) == before and not (tmp_path / 'office').exists()


def test_a_new_ship_named_still_refuses_while_another_holds_the_desk(embedded, tmp_path):
    with pytest.raises(ShipError, match='office still lives in ship crew'):
        first_step({**embedded, 'folder': str(tmp_path / 'ships' / 'another'), 'name': 'another'})
    assert not (tmp_path / 'ships' / 'another').exists() and not (tmp_path / 'office').exists()


def test_with_an_office_folder_a_ship_holding_a_desk_is_refused_as_before(embedded, tmp_path):
    office, _, _ = setup.template('office')
    (tmp_path / 'office').mkdir()
    (tmp_path / 'office' / 'ship.toml').write_text(office)
    with pytest.raises(ShipError, match='this ship still holds a desk'):
        first_step({**embedded, 'existing': 'keep'})
