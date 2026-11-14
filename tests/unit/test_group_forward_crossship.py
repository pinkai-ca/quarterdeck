"""A forum's lead forwards an order that came from another ship: the copies file, and every forged forward is held.

Two synthetic ships in one fleet: office, whose desk posts to lead@agora, and agora, the forum, whose lead forwards
the imported order to its reviewers. The imported root keeps its wire form on disk (to lead@agora) while every reader
sees the local view (to lead); the fan-out compares the two as the store presents them. No program is launched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quarterdeck import cli, fleet_post as fleet, group_post, post, ship as shipmod

ROOT = Path(__file__).resolve().parents[2]
BODY = 'Review the synthetic README.\n\nKeep the dissent: α ≠ β.\n'


@pytest.fixture
def fleet_pair(tmp_path, monkeypatch):
    agora_folder = tmp_path / 'ships' / 'agora'
    agora_folder.mkdir(parents=True)
    (agora_folder / 'ship.toml').write_text((ROOT / 'ships/forum/ship.toml').read_text())
    office_folder = tmp_path / 'ships' / 'office'
    office_folder.mkdir(parents=True)
    (office_folder / 'ship.toml').write_text('''[ship]
name="office"
operator="person"
code="checkout"
[pools.fake]
provider="fake"
[sessions.office]
title="Synthetic office"
human=true
[seats.desk]
session="office"
role="desk"
harness="fake"
pool="fake"
''')
    agora, office = shipmod.load(agora_folder), shipmod.load(office_folder)
    monkeypatch.setattr(cli, 'delivery_note', lambda ship, to: 'synthetic delivery pending')
    return office, agora, post.Store(office.runtime), post.Store(agora.runtime)


def qd(ship, monkeypatch, seat, *argv):
    monkeypatch.setattr(cli, 'load_ship', lambda args: ship)
    if seat is None:
        monkeypatch.delenv('QD_SEAT', raising=False)
    else:
        monkeypatch.setenv('QD_SEAT', seat)
    return cli.cmd_post(cli.build_parser().parse_args(['post', *argv]))


def imported(fleet_pair, monkeypatch):
    """The desk's order, filed on office and fetched by agora: agora's lead holds it open."""
    office, agora, office_store, agora_store = fleet_pair
    qd(office, monkeypatch, None, 'order', '--from', 'desk', '--to', 'lead@agora', BODY)
    post.file_drops(office_store, office)
    fleet.run(office, office_store)
    fleet.run(agora, agora_store)
    root, = agora_store.open_for('lead')
    assert root.sender == 'desk@office' and '@' in root.id and root.to == 'lead'
    raw = json.loads((agora_store.dir / f'{root.id}.json').read_text())
    assert raw['to'] == 'lead@agora', 'the stored bytes keep the wire form'
    return root


def copies_of(store, root):
    return sorted((m for m in store.all() if m.kind == 'order' and m.re == root.id), key=lambda m: m.to)


def holds(ship):
    return group_post.holds(ship.runtime) + post.drop_alarms(ship.runtime)


def test_a_leads_forward_of_a_cross_ship_order_files_every_copy(fleet_pair, monkeypatch):
    office, agora, office_store, agora_store = fleet_pair
    root = imported(fleet_pair, monkeypatch)
    qd(agora, monkeypatch, 'lead', 'order', '--to', 'reviewers', '--re', root.id, root.body)
    assert post.file_drops(agora_store, agora) == []
    copies = copies_of(agora_store, root)
    assert [m.to for m in copies] == ['reviewer-1', 'reviewer-2', 'reviewer-3', 'reviewer-4']
    assert all(m.sender == 'lead' and m.body == root.body and m.re == root.id for m in copies)
    assert holds(agora) == []
    assert list((agora.runtime / 'post' / 'drop' / 'lead').iterdir()) == [], 'the forward is filed and gone'
    assert agora_store.get(root.id) == root, 'the root is unchanged'
    assert post.file_drops(agora_store, agora) == [] and len(copies_of(agora_store, root)) == 4, 'filed once'


def intent(ship, seat, root_id, group='reviewers'):
    folder = post.drop_dir(ship.runtime, seat)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f'{root_id}.group.json'
    path.write_text(json.dumps({'format': 1, 'group': group, 'root': root_id}) + '\n')
    return path


def test_a_forward_of_an_order_not_in_the_store_is_held(fleet_pair, monkeypatch):
    office, agora, office_store, agora_store = fleet_pair
    missing = 'ord-20261114-1242-desk@office-ffff'
    path = intent(agora, 'lead', missing)
    assert ('drop/lead', 'not a message') in post.file_drops(agora_store, agora)
    assert path.is_file() and agora_store.all() == []


def test_a_forward_of_a_closed_cross_ship_order_is_held(fleet_pair, monkeypatch):
    office, agora, office_store, agora_store = fleet_pair
    root = imported(fleet_pair, monkeypatch)
    qd(agora, monkeypatch, 'lead', 'report', '--re', root.id, 'closed before any forward')
    post.file_drops(agora_store, agora)
    assert not agora_store.is_open(agora_store.get(root.id))
    path = intent(agora, 'lead', root.id)
    assert ('drop/lead', 'not a message') in post.file_drops(agora_store, agora)
    assert path.is_file() and copies_of(agora_store, root) == []


def test_a_forward_by_a_seat_that_is_not_the_lead_is_held(fleet_pair, monkeypatch):
    office, agora, office_store, agora_store = fleet_pair
    root = imported(fleet_pair, monkeypatch)
    path = intent(agora, 'reviewer-1', root.id)
    assert ('drop/reviewer-1', 'from names another seat') in post.file_drops(agora_store, agora)
    assert path.is_file() and copies_of(agora_store, root) == []


def test_a_planted_root_naming_another_ship_with_its_forward_is_held(fleet_pair, monkeypatch):
    """The lead cannot make a cross-ship order of its own: a root in its drop that names desk@office is not stored."""
    office, agora, office_store, agora_store = fleet_pair
    forged = post.Message('ord-20261114-1242-desk@office-abcd', 'order', 'desk@office', 'lead', post.now(), BODY)
    folder = post.drop_dir(agora.runtime, 'lead')
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f'{forged.id}.json').write_text(forged.to_json())
    path = intent(agora, 'lead', forged.id)
    assert ('drop/lead', 'not a message') in post.file_drops(agora_store, agora)
    assert path.is_file() and agora_store.all() == []


def test_a_forward_whose_stored_root_differs_from_its_intent_is_still_refused(fleet_pair, monkeypatch):
    """The view is the store's own: a journal whose root differs from the stored order in anything but the
    wire-form parties publishes nothing more."""
    office, agora, office_store, agora_store = fleet_pair
    root = imported(fleet_pair, monkeypatch)
    altered = post.Message(root.id, root.kind, root.sender, root.to, root.at, root.body + 'altered')
    with pytest.raises(shipmod.ShipError, match='differs from its saved intent'):
        group_post._publish(agora_store, altered)
    group_post._publish(agora_store, root)  # the store's own view of the same order is the same message
