"""A certified local classification never substitutes for transport evidence."""
import json
import time
from types import SimpleNamespace

import pytest

from quarterdeck import fleet_post as fleet, post, ship as shipmod
from quarterdeck.service import Service
from tests.unit.test_fleet_post import pair, wire
from tests.unit.test_post_forwarding import forward


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=time.time_ns() + 10 * post.POST_TIMESTAMP_TICK_NS)
    monkeypatch.setattr(fleet, 'time', SimpleNamespace(time_ns=lambda: clock.now))
    return clock


def local(pair):
    left, _, store, _ = pair
    message = store.new('order', 'writer', 'captain', 'Only local work.')
    return left, store, message


def service(ship, store):
    return SimpleNamespace(ship=ship, store=store, runtime=ship.runtime,
                           fleet_exports=fleet.ExportWorklist())


def test_quiet_second_service_pass_skips_local_walk_and_all_mutations(pair, clock, monkeypatch):
    ship, store, _ = local(pair)
    duty = service(ship, store)
    Service.fleet_post(duty)
    assert duty.fleet_exports.certificate is not None
    assert duty.fleet_exports.messages == ()
    def unexpected(*args, **kwargs):
        pytest.fail('quiet pass attempted a local export walk or mutation')
    monkeypatch.setattr(store, 'all', unexpected)
    monkeypatch.setattr(fleet, 'provenance', unexpected)
    monkeypatch.setattr(fleet, 'destinations', unexpected)
    monkeypatch.setattr(fleet, 'publish', unexpected)
    monkeypatch.setattr(fleet, 'replace_status', unexpected)
    from pathlib import Path
    monkeypatch.setattr(Path, 'unlink', unexpected)
    Service.fleet_post(duty)


def test_classifying_pass_walks_only_retained_messages_outside_reading(pair, clock, monkeypatch):
    ship, store, order = local(pair)
    store.new('report', 'captain', 'ship', 'Local completion.', re=order.id)
    store.new('report', 'captain', 'ship', 'Local note without a reply.')
    remote = store.new('question', 'writer', 'captain@beta', 'Cross-ship question.')
    answer = store.new('answer', 'writer', 'captain', 'Synthetic information.')
    uncertain = store.new('report', 'captain', 'ship', 'Non-order ancestry stays.', re=answer.id)
    walked, offered = [], []
    original = fleet.destinations
    def destinations(*args):
        assert post._READING.get() is None
        walked.append(args[-1].id)
        return original(*args)
    def offer(ship, message, target):
        assert post._READING.get() is None
        offered.append((message.id, target))
    monkeypatch.setattr(fleet, 'destinations', destinations)
    monkeypatch.setattr(fleet, 'offer', offer)
    worklist = fleet.ExportWorklist()
    assert worklist.certificate is None
    assert fleet.run(ship, store, worklist=worklist) == []
    assert walked == [remote.id, uncertain.id]
    assert offered == [(remote.id, 'beta')]
    assert {message.id for message in worklist.messages} == set(walked)
    assert worklist.certificate is not None


@pytest.mark.parametrize('changed', ['store', 'imports', 'forwarded', 'addressed',
                                     'declaration', 'groups', 'compaction'])
def test_each_local_classification_input_rebuilds_whole_worklist(pair, clock, monkeypatch, changed):
    ship, store, message = local(pair)
    # A declaration also makes the compaction reservation an explicit input.
    ship.forward['retired'] = 'writer@beta'
    worklist = fleet.ExportWorklist()
    assert fleet.run(ship, store, worklist=worklist) == []
    assert worklist.certificate is not None
    calls = []
    original = store.all
    monkeypatch.setattr(store, 'all', lambda: (calls.append(True), original())[1])
    if changed in ('store', 'imports', 'forwarded', 'addressed'):
        folder = ship.runtime / 'post' / changed
        folder.mkdir(exist_ok=True)
        # Ignored names still change the inventory's input without changing semantics.
        (folder / 'inventory-change.txt').write_text('changed')
    elif changed == 'declaration':
        ship.file.write_text(ship.file.read_text() + '\n# changed alone\n')
    elif changed == 'groups':
        ship.groups['new'] = SimpleNamespace(members=('captain',))
    else:
        path = ship.runtime / 'seats' / 'retired' / 'compaction.json'
        path.parent.mkdir(parents=True)
        path.write_text('{}')
    assert fleet.run(ship, store, worklist=worklist) == []
    assert calls


def test_recent_listing_is_reread_after_tick_instead_of_aged_into_certificate(pair, clock, monkeypatch):
    ship, store, _ = local(pair)
    worklist = fleet.ExportWorklist()
    stamp = worklist.stamp(ship)
    newest = max(max(s[-2:]) for s in stamp[1] if s is not None)
    clock.now = newest + post.POST_TIMESTAMP_TICK_NS
    assert fleet.run(ship, store, worklist=worklist) == []
    assert worklist.certificate is None
    calls = []
    original = store.all
    monkeypatch.setattr(store, 'all', lambda: (calls.append(True), original())[1])
    clock.now += 1
    assert fleet.run(ship, store, worklist=worklist) == []
    assert calls and worklist.certificate is not None


def test_input_recent_at_entry_is_not_certified_after_a_slow_rebuild(pair, clock, monkeypatch):
    ship, store, _ = local(pair)
    worklist = fleet.ExportWorklist()
    stamp = worklist.stamp(ship)
    newest = max(max(item[-2:]) for item in stamp[1] if item is not None)
    clock.now = newest + post.POST_TIMESTAMP_TICK_NS // 10
    retained = worklist.retained
    def slow(*args):
        clock.now += 3 * post.POST_TIMESTAMP_TICK_NS
        return retained(*args)
    with monkeypatch.context() as patch:
        patch.setattr(worklist, 'retained', slow)
        assert fleet.run(ship, store, worklist=worklist) == []
    assert clock.now - newest > post.POST_TIMESTAMP_TICK_NS
    assert worklist.stamp(ship) == stamp and worklist.certificate is None
    calls = []
    original = store.all
    monkeypatch.setattr(store, 'all', lambda: (calls.append(True), original())[1])
    assert fleet.run(ship, store, worklist=worklist) == []
    assert calls == [True] and worklist.certificate is not None
    calls.clear()
    assert fleet.run(ship, store, worklist=worklist) == []
    assert calls == [], 'the fresh post-tick reading certifies the following quiet pass'


def test_change_during_build_and_backward_clock_certify_nothing(pair, clock, monkeypatch):
    ship, store, _ = local(pair)
    worklist = fleet.ExportWorklist()
    original = worklist.retained
    def change(*args):
        ship.file.write_text(ship.file.read_text() + '\n# during export\n')
        return original(*args)
    monkeypatch.setattr(worklist, 'retained', change)
    assert fleet.run(ship, store, worklist=worklist) == []
    assert worklist.certificate is None
    monkeypatch.setattr(worklist, 'retained', original)
    assert fleet.run(ship, store, worklist=worklist) == []
    assert worklist.certificate is not None
    clock.now -= 1
    calls = []
    monkeypatch.setattr(store, 'all', lambda: (calls.append(True), [])[1])
    assert fleet.run(ship, store, worklist=worklist) == []
    assert calls


def test_fresh_service_rebuilds_even_with_unchanged_store(pair, clock, monkeypatch):
    ship, store, _ = local(pair)
    Service.fleet_post(service(ship, store))
    calls = []
    original = store.all
    monkeypatch.setattr(store, 'all', lambda: (calls.append(True), original())[1])
    Service.fleet_post(service(ship, store))
    assert calls


def test_failed_export_and_failed_peer_pass_invalidate_certificate(pair, clock, monkeypatch):
    ship, store, message = local(pair)
    worklist = fleet.ExportWorklist()
    assert fleet.run(ship, store, worklist=worklist) == []
    # A peer discovery exception after successful export invalidates the whole pass.
    with monkeypatch.context() as patch:
        patch.setattr(fleet, 'peers', lambda *_: (_ for _ in ()).throw(RuntimeError('discovery failed')))
        with pytest.raises(RuntimeError, match='discovery failed'):
            fleet.run(ship, store, worklist=worklist)
    assert worklist.certificate is None
    # The failure must arise from retained work; proven local work is never walked.
    store.new('question', 'writer', 'captain@beta', 'Synthetic outbound failure.')
    assert fleet.run(ship, store, worklist=worklist) == []  # establish outbound folders before the error
    with monkeypatch.context() as patch:
        patch.setattr(fleet, 'destinations', lambda *_: (_ for _ in ()).throw(OSError('unreadable')))
        assert fleet.run(ship, store, worklist=worklist)
    assert worklist.certificate is None
    assert fleet.run(ship, store, worklist=worklist) == []
    assert worklist.certificate is not None


def test_qualified_posts_and_saved_routes_remain_but_local_reports_leave(pair, clock):
    ship, other, store, destination = pair
    plain = store.new('order', 'writer', 'captain', 'Local.')
    remote = store.new('question', 'writer', 'captain@beta', 'Remote.')
    report = store.new('report', 'captain', 'ship', 'Local report.', re=plain.id)
    worklist = fleet.ExportWorklist()
    assert fleet.run(ship, store, worklist=worklist) == []
    # First offer creates folders, so rebuild once to certify the final inputs.
    assert fleet.run(ship, store, worklist=worklist) == []
    assert {m.id for m in worklist.messages} == {remote.id}
    assert report.id not in {m.id for m in worklist.messages}
    assert fleet.run(other, destination) == []
    received = destination.get(remote.id)
    imported = fleet.provenance(other.runtime, remote.id)
    assert worklist.retained(other, received, imported)
    route = ship.runtime / 'post' / 'forwarded' / (plain.id + '.json')
    fleet.publish(route, fleet.encoded({'format': 1}))
    assert worklist.retained(ship, plain, None)


def test_peer_offer_arriving_after_certification_is_fetched_same_pass(pair, clock):
    left, right, source, destination = pair
    destination.new('order', 'writer', 'captain', 'Local warmup.')
    worklist = fleet.ExportWorklist()
    assert fleet.run(right, destination, worklist=worklist) == []
    assert worklist.certificate is not None
    message = source.new('question', 'writer', 'captain@beta', 'A new arrival.')
    assert fleet.run(left, source) == []
    assert fleet.run(right, destination, worklist=worklist) == []
    assert wire(left, message) == wire(right, message)
    assert destination.open_for('captain')[-1].id == message.id


def test_cached_outbound_revalidates_peer_declaration_and_repairs_missing_offer(pair, clock):
    left, right, source, _ = pair
    message = source.new('question', 'writer', 'captain@beta', 'Keep validating.')
    worklist = fleet.ExportWorklist()
    for _ in range(2):
        assert fleet.run(left, source, worklist=worklist) == []
    assert worklist.certificate is not None
    path = left.runtime / 'post' / 'outbound' / 'beta' / (message.id + '.json')
    path.unlink()
    assert fleet.run(left, source, worklist=worklist) == []
    assert path.exists()
    # This is peer configuration only; the local worklist key does not change.
    right.file.write_text(right.file.read_text().replace('[seats.captain]', '[seats.changed]'))
    assert fleet.run(left, source, worklist=worklist)
    assert worklist.certificate is None


def test_new_forwarding_declaration_reclassifies_previously_local_order(pair, clock):
    left, right, source, destination = pair
    message = source.new('order', 'writer', 'captain', 'Local until the declaration changes.')
    worklist = fleet.ExportWorklist()
    assert fleet.run(left, source, worklist=worklist) == []
    assert worklist.messages == () and worklist.certificate is not None
    original = wire(left, message)
    left = forward(left)
    assert fleet.run(left, source, worklist=worklist) == []
    assert fleet.run(right, destination, worklist=fleet.ExportWorklist()) == []
    assert destination.open_for('writer')[0].id == message.id
    assert wire(right, message) == original


def test_report_rechain_and_imported_ancestry_survive_cached_round_trip(pair, clock):
    left, right, source, destination = pair
    lists = fleet.ExportWorklist(), fleet.ExportWorklist()
    source.new('order', 'writer', 'captain', 'Local warmup.')
    assert fleet.run(left, source, worklist=lists[0]) == []
    order = source.new('order', 'writer', 'captain@beta', 'Report silently.')
    assert fleet.run(left, source, worklist=lists[0]) == []
    assert fleet.run(right, destination, worklist=lists[1]) == []
    report = destination.new('report', 'captain', 'ship', 'Finished.', re=order.id)
    assert fleet.run(right, destination, worklist=lists[1]) == []
    assert fleet.run(left, source, worklist=lists[0]) == []
    assert source.closing_report(source.get(order.id)).id == report.id
    for ship, store, worklist in ((left, source, lists[0]), (right, destination, lists[1])):
        for _ in range(2):
            assert fleet.run(ship, store, worklist=worklist) == []
    assert wire(left, order) == wire(right, order)
    assert wire(left, report) == wire(right, report)
    assert len([m for m in source.all() if m.id == report.id]) == 1
