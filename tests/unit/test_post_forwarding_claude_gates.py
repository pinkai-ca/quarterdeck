"""Claude gates for forwarding an old seat address to a seat of another ship, on synthetic ships in a throwaway
project: what settled at the old address stays there, what is unsettled holds, a route is the origin's to give, and
each ship writes only its own runtime. No real ship, seat or provider is touched.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from quarterdeck import compaction, fleet_post as fleet, post, post_threads, ship as shipmod, thread_context
from tests.unit.test_fleet_post import pair  # noqa: F401  (alpha and beta, each with captain and writer)


def forward(ship, seat='captain', target='captain@beta'):
    with ship.file.open('a') as stream:
        stream.write(f'\n[forward]\n{seat}="{target}"\n')
    return shipmod.load(ship.folder)


def everything(folder: Path) -> dict[str, tuple]:
    found = {}
    for root, folders, files in os.walk(folder):
        for name in folders + files:
            path = Path(root) / name
            info = path.lstat()
            found[str(path.relative_to(folder))] = (info.st_mode, info.st_ino, info.st_size, info.st_mtime_ns,
                                                    info.st_ctime_ns)
    return found


def delivered(ship, message, seat):
    attempts = post.Attempts(ship.runtime)
    attempts.close(attempts.open(message, seat, None, None, None), 'delivered')


def offered(ship, message):
    return list((ship.runtime / 'post' / 'outbound').rglob(message.id + '.json'))


# -- same-name history: what the old seat settled stays with it ----------------------------------------------------

def test_messages_settled_at_the_old_address_before_the_forward_do_not_move_to_the_new_seat(pair):
    left, right, source, destination = pair
    closed_order = source.new('order', 'writer', 'captain', 'Synthetic order closed before the forward.')
    delivered(left, closed_order, 'captain')
    source.new('report', 'captain', 'writer', 'Synthetic closing report.', re=closed_order.id)
    claimed = source.new('ack', 'writer', 'captain', 'Synthetic ack, delivered long ago.')
    assert not source.is_open(source.get(closed_order.id))

    left = forward(left)
    fleet.run(left, source)
    fleet.run(right, destination)
    moved = [m.id for m in (closed_order, claimed) if offered(left, m) or (destination.dir / f'{m.id}.json').exists()]
    assert moved == [], 'settled history of the old address moved to the new seat'
    assert destination.for_seat('captain') == [] and destination.open_for('captain') == []


# -- unsettled attempts and lifecycle hold the transfer ------------------------------------------------------------

def test_an_unsettled_delivery_at_the_old_address_holds_the_transfer_until_it_settles(pair):
    left, right, source, destination = pair
    order = source.new('order', 'writer', 'captain', 'Synthetic order mid-delivery at the old address.')
    attempts = post.Attempts(left.runtime)
    attempt = attempts.open(order, 'captain', None, None, None)  # the door has it in hand: no result yet
    left = forward(left)
    errors = fleet.run(left, source)
    assert any('outbound held' in e for e in errors), errors
    assert offered(left, order) == [] and not (destination.dir / f'{order.id}.json').exists()
    fleet.run(right, destination)
    assert destination.all() == []
    attempts.close(attempt, 'blind', 'synthetic: the door could not confirm it')
    assert any('outbound held' in e for e in fleet.run(left, source)), 'an uncertain delivery still holds'
    assert offered(left, order) == []


def test_a_compaction_handoff_reserved_for_the_old_seats_successor_is_not_moved(pair):
    left, right, source, destination = pair
    handoff = source.new('order', 'captain', 'captain', 'Synthetic handoff reserved for my successor.')
    journal = compaction.new()
    journal['reserved'] = handoff.id
    compaction.save(left.runtime, 'captain', journal)
    assert compaction.withheld(left.runtime, 'captain') == ({handoff.id}, False)

    left = forward(left)
    fleet.run(left, source)
    fleet.run(right, destination)
    assert offered(left, handoff) == [] and not (destination.dir / f'{handoff.id}.json').exists(), \
        "a handoff reserved for the old seat's successor left with the forward"


# -- route authority, returned ancestry included -------------------------------------------------------------------

def test_a_returned_ancestor_whose_route_differs_from_the_origins_own_is_held(pair):
    left, right, source, destination = pair
    left = forward(left)
    order = source.new('order', 'writer', 'captain', 'Synthetic order on its declared route.')
    fleet.run(left, source)
    fleet.run(right, destination)
    received = destination.get(order.id)
    destination.new('report', 'captain', received.sender, 'Synthetic report.', re=received.id)
    fleet.run(right, destination)
    # beta's offer now names a route for alpha's own order that alpha never declared.
    offer, = (right.runtime / 'post' / 'outbound' / 'alpha').glob('*.json')
    value = json.loads(offer.read_text())
    entry = next(e for e in value['files'] if e['id'] == order.id)
    entry['forward'] = dict(entry['forward'], to='writer@beta', via=[entry['forward']['from'], 'writer@beta'])
    offer.chmod(0o600)
    offer.write_text(json.dumps(value, sort_keys=True) + '\n')
    errors = fleet.run(left, source)
    assert any('incoming post held' in e for e in errors), errors
    assert source.is_open(source.get(order.id)), 'a report on a route its origin never declared closes nothing'


def test_a_route_is_the_origin_ships_to_give_not_a_third_ships(pair, tmp_path):
    left, right, source, destination = pair
    gamma = shipmod.load(_copy_declaration(left, 'gamma'))
    order = source.new('order', 'writer', 'captain', 'Synthetic order alpha never forwarded.')
    data = (left.runtime / 'post' / 'store' / f'{order.id}.json').read_bytes()
    # gamma holds alpha's bytes and offers them to beta with a route alpha never declared.
    (gamma.runtime / 'post' / 'store').mkdir(parents=True)
    (gamma.runtime / 'post' / 'store' / f'{order.id}.json').write_bytes(data)
    route = dict(format=1, id=order.id, sha256=fleet.digest(data), via=['captain@alpha', 'captain@beta'],
                 **{'from': 'captain@alpha', 'to': 'captain@beta'})
    value = dict(format=2, source='gamma', target='beta', message=order.id,
                 files=[dict(id=order.id, origin='alpha', size=len(data), sha256=fleet.digest(data), forward=route)])
    fleet.publish(gamma.runtime / 'post' / 'outbound' / 'beta' / f'{order.id}.json', fleet.encoded(value))
    fleet.run(right, destination)
    assert not (right.runtime / 'post' / 'store' / f'{order.id}.json').exists(), \
        "a route alpha never declared, offered by a third ship, was taken"


def _copy_declaration(ship, name):
    folder = ship.folder.parent / name
    folder.mkdir()
    (folder / 'ship.toml').write_text(ship.file.read_text())
    return folder


# -- each ship writes only its own runtime, validation and read paths included -------------------------------------

def test_forwarding_writes_only_each_ships_own_runtime_including_validation_and_reads(pair):
    left, right, source, destination = pair
    left = forward(left)
    order = source.new('order', 'writer', 'captain', 'Synthetic forwarded order.')

    before = everything(right.folder)
    fleet.run(left, source)
    fleet.status(left, source)
    fleet.validate_target(left, 'captain@beta')
    post_threads.render(post_threads.whole(source, order.id, 'writer'))
    thread_context.select(source, 'writer')
    assert everything(right.folder) == before, "alpha's pass, validation and reads wrote nothing of beta"

    before = everything(left.folder)
    fleet.run(right, destination)
    fleet.status(right, destination)
    fleet.validate_target(right, 'captain@alpha')
    post_threads.render(post_threads.whole(destination, order.id, 'captain'))
    assert everything(left.folder) == before, "beta's fetch, validation and reads only read alpha"

    received = destination.get(order.id)
    destination.new('report', 'captain', received.sender, 'Synthetic report.', re=received.id)
    before = everything(left.folder)
    fleet.run(right, destination)
    assert everything(left.folder) == before
    before = everything(right.folder)
    fleet.run(left, source)
    assert fleet.waiting(left, source) == []
    assert everything(right.folder) == before
    assert not source.is_open(source.get(order.id))


def test_a_third_ship_cannot_offer_another_ships_message_as_that_ships_even_without_a_route(pair):
    left, right, source, destination = pair
    gamma = shipmod.load(_copy_declaration(left, 'gamma'))
    order = source.new('order', 'writer', 'captain@beta', 'Synthetic order only alpha may offer.')
    data = (left.runtime / 'post' / 'store' / f'{order.id}.json').read_bytes()
    (gamma.runtime / 'post' / 'store').mkdir(parents=True)
    (gamma.runtime / 'post' / 'store' / f'{order.id}.json').write_bytes(data)
    value = dict(format=1, source='gamma', target='beta', message=order.id,
                 files=[dict(id=order.id, origin='alpha', size=len(data), sha256=fleet.digest(data))])
    fleet.publish(gamma.runtime / 'post' / 'outbound' / 'beta' / f'{order.id}.json', fleet.encoded(value))
    fleet.run(right, destination)
    assert not (right.runtime / 'post' / 'store' / f'{order.id}.json').exists(), \
        "a third ship's offer was imported as alpha's own"
