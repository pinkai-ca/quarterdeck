"""Two synthetic ships exchange the original bytes, with receiver-owned fetches."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import cli, fleet_post as fleet, post, post_threads, ship as shipmod
from quarterdeck.service import Service


@pytest.fixture
def pair(tmp_path):
    def create(name):
        folder = tmp_path / 'ships' / name
        folder.mkdir(parents=True)
        (folder / 'ship.toml').write_text('''[ship]
name="Display name"
operator="person"
code="checkout"
[pools.fake]
provider="fake"
[sessions.crew]
title="Synthetic crew"
[seats.captain]
session="crew"
role="captain"
harness="fake"
pool="fake"
[seats.writer]
session="crew"
role="worker"
harness="fake"
pool="fake"
''')
        return shipmod.load(folder)
    left, right = create('alpha'), create('beta')
    return left, right, post.Store(left.runtime), post.Store(right.runtime)


def offer_path(ship, target, message):
    return ship.runtime / 'post' / 'outbound' / target / (message.id + '.json')


def wire(ship, message):
    return (ship.runtime / 'post' / 'store' / (message.id + '.json')).read_bytes()


def test_round_trip_preserves_bytes_identity_claim_and_closure(pair):
    left, right, source, destination = pair
    order = source.new('order', 'writer', 'captain@beta', 'Build one thing.\nKeep this exact body.  ')
    assert fleet.waiting(left, source) and destination.all() == []
    before = wire(left, order)
    assert source.get(order.id) == order
    assert fleet.run(left, source) == []
    assert destination.all() == [], 'the sender never fetches into the destination'
    assert fleet.run(right, destination) == []
    assert wire(right, order) == before and wire(left, order) == before  # law: post-is-files
    received = destination.get(order.id)
    assert received.to == 'captain' and received.sender == 'writer@alpha'
    assert 'post/inbox/captain.md' in post.typed([received])
    assert destination.open_for('captain') == [received]
    assert fleet.waiting(left, source) == []
    ack = destination.new('ack', 'captain', received.sender, 'claimed', re=received.id)
    assert fleet.run(right, destination) == [] and fleet.run(left, source) == []
    assert source.is_open(order)
    assert destination.claimed('captain', post.now()) == order.id
    assert ack.id not in {m.id for m in post.Attempts(left.runtime).undelivered(source, 'writer')}
    report = destination.new('report', 'captain', received.sender, 'Done.', re=received.id)
    fleet.run(right, destination)
    assert source.is_open(order), 'a report is not locally committed until its ship fetches it'
    fleet.run(left, source)
    assert not source.is_open(order)
    assert source.closing_report(order).id == report.id
    assert wire(left, report) == wire(right, report)
    assert fleet.waiting(right, destination) == []
    thread = post_threads.whole(source, report.id, 'writer')
    assert {m.id for m in thread.messages} == {order.id, ack.id, report.id}
    assert len(source.all()) == 3 == len(destination.all())


def test_repeated_fetch_and_fresh_store_do_not_create_another_delivery(pair):
    left, right, source, destination = pair
    order = source.new('question', 'writer', 'captain@beta', 'Which choice?')
    fleet.run(left, source)
    path = offer_path(left, 'beta', order)
    for _ in range(3):
        fleet.fetch(right, left, path)
    attempts = post.Attempts(right.runtime)
    received = destination.get(order.id)
    # A local consumer owns its receipt; transport neither invents nor erases it.
    opened = attempts.open(received, 'captain', None, None, None)
    attempts.close(opened, 'delivered')
    fleet.run(right, post.Store(right.runtime))
    assert post.Attempts(right.runtime).undelivered(post.Store(right.runtime), 'captain') == []
    assert len(attempts.for_message(order.id)) == 1
    assert len(destination.all()) == 1


@pytest.mark.parametrize('cut', ['intent', 'post', 'addressed', 'receipt'])
def test_crash_at_each_publication_recovers_the_same_file(pair, monkeypatch, cut):
    left, right, source, destination = pair
    order = source.new('order', 'writer', 'captain@beta', 'One original.')
    fleet.run(left, source)
    original = fleet.publish
    failed = []
    def crash(path, data):
        original(path, data)
        folder = path.parent.name
        matches = {'intent': folder == 'imports', 'post': folder == 'store',
                   'addressed': folder == 'addressed', 'receipt': folder == 'alpha' and path.parent.parent.name == 'fetched'}
        if matches[cut] and not failed:
            failed.append(str(path))
            raise RuntimeError('synthetic crash after publication')
    monkeypatch.setattr(fleet, 'publish', crash)
    with pytest.raises(RuntimeError, match='synthetic crash'):
        fleet.fetch(right, left, offer_path(left, 'beta', order))
    monkeypatch.setattr(fleet, 'publish', original)
    fleet.fetch(right, left, offer_path(left, 'beta', order))
    assert failed and wire(right, order) == wire(left, order)
    assert destination.open_for('captain')[0].id == order.id
    assert fleet.waiting(left, source) == []
    assert len(list(destination.dir.glob('*.json'))) == 1


def test_competing_fetches_publish_one_name_and_receipt(pair):
    left, right, source, destination = pair
    order = source.new('order', 'writer', 'captain@beta', 'Same id under concurrency.')
    fleet.run(left, source)
    path = offer_path(left, 'beta', order)
    with ThreadPoolExecutor(max_workers=2) as workers:
        assert list(workers.map(lambda _: fleet.fetch(right, left, path), range(4))) == [order.id] * 4
    assert len(list(destination.dir.glob('*.json'))) == 1
    assert len(list((right.runtime / 'post' / 'fetched' / 'alpha').glob('*.json'))) == 1


def test_context_ancestors_keep_their_origin_and_do_not_wake(pair):
    left, right, source, destination = pair
    root = source.new('order', 'writer', 'captain', 'Original local brief.')
    child = source.new('question', 'captain', 'writer@beta', 'Consult the other ship.', re=root.id)
    original = wire(left, root)
    fleet.run(left, source)
    fleet.run(right, destination)
    assert wire(right, root) == original
    assert destination.get(root.id).to == 'captain@alpha'
    assert destination.get(root.id).sender == 'writer@alpha'
    assert destination.for_seat('captain') == []
    thread = post_threads.whole(destination, child.id, 'writer')
    assert [m.id for m in thread.messages] == [child.id]
    assert root.id in thread.others[0], 'a same-named local seat is not a party to foreign history'
    assert post.Attempts(right.runtime).undelivered(destination, 'writer')[0].id == child.id


def test_closing_report_to_silent_ship_returns_as_context(pair):
    left, right, source, destination = pair
    order = source.new('order', 'writer', 'captain@beta', 'Report silently if no wake is needed.')
    fleet.run(left, source); fleet.run(right, destination)
    report = destination.new('report', 'captain', 'ship', 'Done.', re=order.id)
    fleet.run(right, destination)
    assert fleet.waiting(right, destination), 'a context-only closing report must also show its transport wait'
    fleet.run(left, source)
    assert source.closing_report(order).id == report.id
    assert source.for_seat('ship') == []
    assert source.get(report.id).to == 'ship@beta'
    assert fleet.waiting(right, destination) == []


def test_a_down_ship_keeps_outbound_waiting_until_it_fetches(pair):
    left, right, source, destination = pair
    order = source.new('order', 'writer', 'captain@beta', 'Wait until the ship is running.')
    fleet.run(left, source)
    assert 'beta: 1 waiting to be fetched' in fleet.status(left, source)[0]
    assert not right.runtime.exists()
    fleet.run(right, destination)
    assert fleet.status(left, source) == [] and destination.get(order.id)


def test_only_identical_existing_bytes_count_as_the_same_fetch(pair):
    left, right, source, destination = pair
    order = source.new('order', 'writer', 'captain@beta', 'Original bytes.')
    fleet.run(left, source)
    fleet.fetch(right, left, offer_path(left, 'beta', order))
    path = destination.dir / (order.id + '.json')
    conflicting = wire(right, order).replace(b'Original bytes.', b'Conflicting text')
    path.write_bytes(conflicting)
    assert fleet.waiting(left, source)
    with pytest.raises(shipmod.ShipError, match='conflict'):
        fleet.fetch(right, left, offer_path(left, 'beta', order))
    assert path.read_bytes() == conflicting


def test_receiver_never_admits_a_partial_source_or_mismatched_offer(pair):
    left, right, source, destination = pair
    order = source.new('order', 'writer', 'captain@beta', 'Original bytes.')
    fleet.run(left, source)
    (source.dir / (order.id + '.json')).write_bytes(b'{')
    assert fleet.run(right, destination)
    assert destination.all() == []
    assert 'unreadable' in fleet.status(left, source)[0]


def test_unchanged_export_and_fetch_do_not_fsync_every_post_again(pair, monkeypatch):
    left, right, source, destination = pair
    source.new('order', 'writer', 'captain@beta', 'One publication.')
    fleet.run(left, source); fleet.run(right, destination)
    def unexpected(*args):
        pytest.fail('an unchanged transport tick must perform no durable write')
    monkeypatch.setattr(fleet.os, 'fsync', unexpected)
    fleet.run(left, source); fleet.run(right, destination)


def test_cli_uses_qualified_address_and_default_reply_target(pair, monkeypatch, capsys):
    left, right, source, destination = pair
    monkeypatch.delenv('QD_SEAT', raising=False)
    assert cli.main(['--ship', str(left.folder), 'post', 'question', '--from', 'writer',
                     '--to', 'captain@beta', 'Which plan?']) == 0
    assert 'waiting for the addressee ship to fetch' in capsys.readouterr().out
    fleet.run(left, source); fleet.run(right, destination)
    question = destination.for_seat('captain')[0]
    assert cli.main(['--ship', str(right.folder), 'post', 'answer', '--from', 'captain',
                     '--re', question.id, 'Plan one.']) == 0
    fleet.run(right, destination); fleet.run(left, source)
    assert not source.is_open(source.get(question.id))
    answer = source.replies(source.get(question.id))[0]
    assert answer.sender == 'captain@beta' and answer.to == 'writer'


def test_service_fetch_runs_before_inbox_rendering(pair):
    left, right, source, destination = pair
    message = source.new('question', 'writer', 'captain@beta', 'Read the fetched inbox.')
    fleet.run(left, source)
    service = Service(right, SimpleNamespace())
    try:
        service.fleet_post()
        service.inboxes()
        text = (right.runtime / 'post' / 'inbox' / 'captain.md').read_text()
        assert message.short in text and 'writer@alpha' in text
    finally:
        service.ledger.close()


def test_discovery_uses_folder_addresses_and_standalone_is_local_only(pair, tmp_path):
    from quarterdeck.fleet import peers
    left, right, _, _ = pair
    assert set(peers(left)) == {'alpha', 'beta'}
    assert peers(left)['beta'].name == 'Display name', 'display names do not change an address'
    standalone = tmp_path / 'standalone'
    standalone.mkdir()
    (standalone / 'ship.toml').write_text('[ship]\nname="Display name"\n')
    assert set(peers(shipmod.load(standalone))) == {'standalone'}


def test_discovery_includes_fixed_office_in_both_directions(pair, monkeypatch, tmp_path):
    from quarterdeck.fleet import peers
    left, right, _, _ = pair
    folder = tmp_path / 'office'
    folder.mkdir()
    (folder / 'ship.toml').write_text('[ship]\nname="Office display"\n')
    office = shipmod.load(folder)
    # The separate office predecessor supplies kind; this pins the shared API
    # without duplicating its parser/schema change in the transport branch.
    office.kind = 'office'
    load = shipmod.load
    monkeypatch.setattr(shipmod, 'load', lambda path: office if path == folder else load(path))
    assert set(peers(left)) == {'office', 'alpha', 'beta'}
    assert set(peers(office)) == {'office', 'alpha', 'beta'}
    assert peers(office)['office'] is office


def test_a_marker_without_destination_bytes_never_clears_waiting(pair):
    left, right, source, destination = pair
    order = source.new('order', 'writer', 'captain@beta', 'Keep the source waiting on incomplete evidence.')
    fleet.run(left, source); fleet.run(right, destination)
    saved = fleet.receipt_path(right, 'alpha', order.id).read_bytes()
    assert saved and fleet.waiting(left, source) == []
    (destination.dir / (order.id + '.json')).unlink()
    assert fleet.waiting(left, source)
    fleet.run(right, destination)
    assert wire(right, order) == wire(left, order)
    assert fleet.waiting(left, source) == []
