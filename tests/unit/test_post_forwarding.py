"""Forward legacy addresses without replacing a post or losing its closing report."""

import json

import pytest

from quarterdeck import fleet_post as fleet, post, post_threads, ship as shipmod
from tests.unit.test_fleet_post import pair, wire


def forward(ship, seat='captain', target='writer@beta'):
    with ship.file.open('a') as stream:
        stream.write(f'\n[forward]\n{seat}="{target}"\n')
    return shipmod.load(ship.folder)


def test_old_bare_address_reaches_new_seat_once_and_report_closes_original(pair):
    left, right, source, destination = pair
    original = source.new('order', 'writer', 'captain', 'Old address, one immutable file.')
    raw = wire(left, original)
    left = forward(left)
    assert source.for_seat('captain') == [], 'the declaration ends old-address dispatch before a fetch'
    assert fleet.run(left, source) == []
    assert fleet.run(right, destination) == []
    received = destination.get(original.id)
    assert received.to == 'writer' and received.sender == 'writer@alpha'
    assert destination.open_for('writer') == [received]
    assert wire(right, original) == raw == wire(left, original)
    assert json.loads(raw)['to'] == 'captain'
    fleet.run(left, source); fleet.run(right, destination)
    assert len(destination.for_seat('writer')) == 1
    report = destination.new('report', 'writer', received.sender, 'Done after forwarding.', re=received.id)
    fleet.run(right, destination); fleet.run(left, source)
    assert source.closing_report(source.get(original.id)).id == report.id
    assert source.open_for('captain') == []
    rendered = post_threads.render(post_threads.whole(source, original.id, 'writer'))
    assert 'captain@alpha forwarded to writer@beta' in rendered
    assert len(source.all()) == len(destination.all()) == 2


def test_new_messages_to_an_old_name_need_no_live_old_seat(pair):
    left, right, source, destination = pair
    left = forward(left, 'retired-seat')
    message = source.new('question', 'writer', 'retired-seat', 'Forward this old name.')
    assert message.to == 'writer@beta'
    assert 'retired-seat' not in left.seats
    fleet.run(left, source); fleet.run(right, destination)
    assert destination.get(message.id).to == 'writer'
    assert source.for_seat('retired-seat') == []


def test_changed_route_keeps_the_first_offer_and_records_a_hold(pair):
    left, right, source, destination = pair
    left = forward(left)
    order = source.new('order', 'writer', 'captain', 'Pin this route.')
    assert fleet.run(left, source) == []
    offer = left.runtime / 'post' / 'outbound' / 'beta' / (order.id + '.json')
    before = offer.read_bytes()
    left.file.write_text(left.file.read_text().replace('writer@beta', 'captain@beta'))
    left = shipmod.load(left.folder)
    assert fleet.run(left, source), 'a changed route is held instead of rewriting an offer'
    assert offer.read_bytes() == before
    assert source.get(order.id).to == 'writer@beta', 'historical identity follows its pinned route'


def test_forwarding_a_previously_fetched_message_preserves_the_original_origin(pair):
    left, right, source, destination = pair
    order = source.new('order', 'writer', 'captain@beta', 'Already at its first destination.')
    fleet.run(left, source); fleet.run(right, destination)
    # Forward within the second ship to an address back on the first ship.
    right = forward(right, target='writer@alpha')
    assert fleet.run(right, destination) == []
    assert fleet.run(left, source) == []
    assert source.get(order.id).to == 'writer'
    assert source.for_seat('writer')[0].id == order.id
    assert json.loads(wire(left, order))['to'] == 'captain@beta'


@pytest.mark.parametrize('table', ['[forward]\ncaptain=42', '[forward]\ncaptain="bare"',
                                  '[forward]\ncaptain="a@b@c"'])
def test_forward_declaration_requires_a_qualified_target(pair, table):
    left, _, _, _ = pair
    with left.file.open('a') as stream:
        stream.write('\n' + table + '\n')
    with pytest.raises(shipmod.ShipError, match='forward'):
        shipmod.load(left.folder)
