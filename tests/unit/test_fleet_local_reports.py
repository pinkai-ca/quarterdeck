"""Only original-byte local proofs leave the export walk; transport remains live."""

from dataclasses import replace

import pytest

from quarterdeck import fleet_post as fleet, forward_post, post
from .test_fleet_post import pair  # noqa: F401
from .test_fleet_export_worklist import clock  # noqa: F401


def report(pair, *, reply=True):
    ship, _, store, _ = pair
    order = store.new('order', 'writer', 'captain', 'Synthetic local work')
    message = store.new('report', 'captain', 'ship', 'Synthetic result', **({'re': order.id} if reply else {}))
    return ship, store, order, message


@pytest.mark.parametrize('reply', [False, True])
def test_local_report_leaves_and_certified_pass_never_reads_it_again(pair, clock, monkeypatch, reply):
    ship, store, order, message = report(pair, reply=reply)
    worklist = fleet.ExportWorklist()
    assert fleet.run(ship, store, worklist=worklist) == []
    assert worklist.certificate is not None and worklist.messages == ()
    def unexpected(*args, **kwargs):
        pytest.fail('certified local report was walked again')
    monkeypatch.setattr(store, 'all', unexpected)
    monkeypatch.setattr(fleet, '_source', unexpected)
    monkeypatch.setattr(fleet, 'provenance', unexpected)
    monkeypatch.setattr(fleet, 'destinations', unexpected)
    assert fleet.run(ship, store, worklist=worklist) == []


def test_report_on_an_answer_stays_even_when_its_chain_is_local(pair, clock):
    ship, _, store, _ = pair
    answer = store.new('answer', 'writer', 'captain', 'Synthetic information')
    message = store.new('report', 'captain', 'ship', 'Synthetic followup', re=answer.id)
    worklist = fleet.ExportWorklist()
    assert fleet.run(ship, store, worklist=worklist) == []
    assert message in worklist.messages


def test_report_from_someone_other_than_the_order_addressee_stays(pair, clock):
    ship, store, order, _ = report(pair)
    message = store.new('report', 'writer', 'ship', 'Not the addressee', re=order.id)
    worklist = fleet.ExportWorklist()
    assert fleet.run(ship, store, worklist=worklist) == []
    assert message in worklist.messages


@pytest.mark.parametrize('where', ['report', 'ancestor'])
@pytest.mark.parametrize('evidence', ['import', 'pin', 'qualified', 'addressed', 'declaration'])
def test_chain_touched_by_nonlocal_or_unknown_evidence_stays(pair, monkeypatch, where, evidence):
    ship, _, store, _ = pair
    ancestor = store.new('order', 'writer', 'captain@beta' if evidence == 'qualified' and where == 'ancestor' else 'captain', 'Synthetic ancestor')
    order = store.new('order', 'writer', 'captain', 'Synthetic local work', re=ancestor.id)
    message = store.new('report', 'captain', 'ship@beta' if evidence == 'qualified' and where == 'report' else 'ship', 'Synthetic result', re=order.id)
    touched = message if where == 'report' else ancestor
    if evidence == 'import':
        original = fleet.provenance
        monkeypatch.setattr(fleet, 'provenance', lambda runtime, identifier: {'origin': 'beta'} if identifier == touched.id else original(runtime, identifier))
    elif evidence == 'pin':
        original = forward_post.recorded
        monkeypatch.setattr(forward_post, 'recorded', lambda runtime, identifier: {'recorded': True} if identifier == touched.id else original(runtime, identifier))
    elif evidence == 'addressed':
        path = ship.runtime / 'post/addressed' / (touched.id + '.json')
        path.parent.mkdir(parents=True)
        path.write_text('{}')
    elif evidence == 'declaration':
        ship.forward[touched.to] = 'writer@beta'
    assert fleet.ExportWorklist().retained(ship, message, None)


@pytest.mark.parametrize('shape', ['missing', 'cycle'])
def test_missing_or_cyclic_ancestry_proves_nothing(pair, shape):
    ship, _, store, _ = pair
    first = store.prepare('order', 'writer', 'captain', 'Synthetic ancestor')
    if shape == 'cycle':
        second = store.prepare('order', 'writer', 'captain', 'Synthetic ancestor two')
        store.write(replace(second, re=first.id))
        first = replace(first, re=second.id)
    else:
        first = replace(first, re='missing-synthetic-ancestor')
    store.write(first)
    order = store.new('order', 'writer', 'captain', 'Synthetic local work', re=first.id)
    message = store.new('report', 'captain', 'ship', 'Synthetic result', re=order.id)
    assert fleet.ExportWorklist().retained(ship, message, None)


def test_a_new_import_marker_invalidates_the_local_report_certificate(pair, clock, monkeypatch):
    ship, store, order, message = report(pair)
    worklist = fleet.ExportWorklist()
    assert fleet.run(ship, store, worklist=worklist) == []
    path = ship.runtime / 'post/imports' / (order.id + '.json')
    path.parent.mkdir(parents=True)
    # Isolate the classification signal, without making transport process synthetic provenance.
    path.write_text('{}')
    assert worklist.stamp(ship) != worklist.certificate[0]
    assert worklist.retained(ship, message, None)


@pytest.mark.parametrize('reply', [False, True])
def test_a_report_read_inside_the_tick_never_ages_into_trust(pair, clock, monkeypatch, reply):
    ship, store, order, message = report(pair, reply=reply)
    worklist = fleet.ExportWorklist()
    stamp = worklist.stamp(ship)
    clock.now = max(max(item[-2:]) for item in stamp[1] if item is not None) + post.POST_TIMESTAMP_TICK_NS
    assert fleet.run(ship, store, worklist=worklist) == []
    assert worklist.certificate is None
    reads = []
    original = fleet._source
    def read(*args, **kwargs):
        reads.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(fleet, '_source', read)
    clock.now += 1
    assert fleet.run(ship, store, worklist=worklist) == []
    assert reads and worklist.certificate is not None


def test_transport_actions_run_outside_the_classification_view(pair, clock, monkeypatch):
    ship, _, store, _ = pair
    store.new('report', 'captain', 'captain@beta', 'Synthetic cross-ship report')
    offer = fleet.offer
    seen = []
    def publish(*args):
        assert post._READING.get() is None
        seen.append(True)
        return offer(*args)
    monkeypatch.setattr(fleet, 'offer', publish)
    worklist = fleet.ExportWorklist()
    for _ in range(3):
        assert fleet.run(ship, store, worklist=worklist) == []
    assert len(seen) == 3


def test_store_replaced_during_original_byte_read_cannot_certify_the_old_directory(pair, clock, monkeypatch):
    import shutil
    ship, store, order, message = report(pair)
    worklist = fleet.ExportWorklist()
    original = fleet._source
    replaced = []
    def read(*args, **kwargs):
        value = original(*args, **kwargs)
        if kwargs.get('directory') is not None and not replaced:
            replaced.append(True)
            previous = store.dir.with_name('previous-store')
            store.dir.rename(previous)
            shutil.copytree(previous, store.dir)
        return value
    monkeypatch.setattr(fleet, '_source', read)
    assert fleet.run(ship, store, worklist=worklist) == []
    assert replaced and worklist.certificate is None
    assert fleet.run(ship, store, worklist=worklist) == []
    assert worklist.certificate is not None and worklist.messages == ()
