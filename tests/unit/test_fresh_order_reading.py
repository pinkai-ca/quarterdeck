"""Closing-order decisions share one post view, then leave it before restarting."""

from quarterdeck import post, post_dir
from .test_fresh_after_order import worker


def test_one_absent_marker_inventory_per_seat_decision(worker, monkeypatch):
    w = worker
    calls = []
    original = post_dir.list_children

    def listed(folder, *args, **kwargs):
        if folder.name in ('imports', 'forwarded'):
            calls.append(folder.name)
        return original(folder, *args, **kwargs)

    monkeypatch.setattr(post_dir, 'list_children', listed)
    # Enough incoming orders to expose repeated context/provenance reads.
    for _ in range(12):
        order = w.svc.store.new('order', 'operator', 'worker', 'Synthetic task')
        w.svc.store.new('report', 'worker', 'operator', 'Done', re=order.id)
    w.record.begin()
    w.record.finish()
    w.svc.fresh_after_order()
    assert w.fresh == [True]
    assert calls.count('imports') == calls.count('forwarded') == 1
    assert post._READING.get() is None


def test_fresh_action_reads_new_posts_outside_the_decision_view(worker, monkeypatch):
    w = worker
    w.record.begin()
    w.record.finish()
    original = w.seat.fresh
    seen = []

    def restart():
        assert post._READING.get() is None
        store = post.Store(w.seat.ship.runtime)
        order = store.new('order', 'operator', 'worker', 'Next task')
        assert order.id in {m.id for m in w.svc.store.open_for('worker')}
        seen.append(order.id)
        return original()

    monkeypatch.setattr(w.seat, 'fresh', restart)
    w.svc.fresh_after_order()
    assert len(seen) == 1 and w.fresh == [True]
    w.svc.fresh_after_order()
    assert w.fresh == [True]


def test_changed_post_and_record_are_read_on_the_next_pass(worker):
    w = worker
    w.record.begin()
    w.svc.fresh_after_order()
    assert not w.fresh
    # The report existed already; only the recorded turn changes here.
    w.record.finish()
    next_order = w.svc.store.new('order', 'operator', 'worker', 'More work')
    w.svc.fresh_after_order()
    assert not w.fresh
    w.svc.store.new('report', 'worker', 'operator', 'More done', re=next_order.id)
    w.svc.fresh_after_order()
    assert w.fresh == [True]
