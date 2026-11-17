"""The status line's cross-ship reading walks only the messages export would offer, never every local one.

fleet_post.waiting runs on every status collection once post/outbound exists, which it does for good after one
transport hold. It walked the whole store, asking each local message's destinations. Now it walks what export's
worklist keeps: an imported message, a report not proved local, '@' in the sender or the addressee, a declared or a
recorded forward. Two synthetic ships under tmp_path.
"""
from quarterdeck import fleet_post as fleet
from tests.unit.test_fleet_post import pair  # noqa: F401  (two synthetic ships, alpha and beta)

LOCAL = 60


def test_waiting_walks_only_the_cross_ship_messages(pair, monkeypatch):
    left, right, source, destination = pair
    fleet.prepare_outbound(left.runtime)  # as one transport hold leaves it, for good
    for index in range(LOCAL):
        order = source.new('order', 'captain', 'writer', f'Synthetic local order {index}.')
        source.new('report', 'writer', 'captain', f'Synthetic local report {index}.', re=order.id)
    crossing = source.new('order', 'writer', 'captain@beta', 'Synthetic order across ships.')
    asked = []
    real = fleet.destinations
    monkeypatch.setattr(fleet, 'destinations', lambda ship, store, message: asked.append(message.id) or real(
        ship, store, message))
    lines = fleet.waiting(left, source)
    assert asked == [crossing.id], f'{len(asked)} messages walked'
    assert lines and lines[0].startswith('post to beta: 1 waiting to be fetched')


def test_what_it_reports_is_unchanged_once_fetched(pair):
    left, right, source, destination = pair
    source.new('order', 'captain', 'writer', 'Synthetic local order.')
    source.new('order', 'writer', 'captain@beta', 'Synthetic order across ships.')
    fleet.run(left, source)
    assert fleet.waiting(left, source) == ['post to beta: 1 waiting to be fetched, oldest 0m']
    fleet.run(right, destination)
    assert fleet.waiting(left, source) == []
