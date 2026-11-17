"""A status collection reads a hand's order in hand, and the store by id, once per seat, not once per waiting message.

hand_door.held asked order_in_hand (every message ever sent to the hand, each asked is_open) and built a dict of the
whole store for every undelivered message: hands x waiting x (history + store). Within one reading pass both are now
read once per seat. Outside a pass (the deliveries' re-check under admission) each call still reads afresh.
"""
from __future__ import annotations

from quarterdeck import post, watch
from .test_hand_door_one_order import as_role, hand, pending_lines  # noqa: F401
from .test_pending_delivery import setup  # noqa: F401

WAITING = 40


def counting(monkeypatch, store):
    counts = {'is_open': 0, 'all': 0}
    is_open, all_ = store.is_open, store.all
    def opened(message):
        counts['is_open'] += 1
        return is_open(message)
    def listed(*args, **kwargs):
        counts['all'] += 1
        return all_(*args, **kwargs)
    monkeypatch.setattr(store, 'is_open', opened)
    monkeypatch.setattr(store, 'all', listed)
    return counts


def orders(svc, seat):
    first = svc.store.new('order', 'captain', seat.name, 'The order in hand.')
    rest = [svc.store.new('order', 'captain', seat.name, f'Waiting order {index}.') for index in range(WAITING)]
    return first, rest


def test_one_collection_reads_the_order_in_hand_once_per_hand(hand, monkeypatch):
    svc, seat, live, record = hand
    first, rest = orders(svc, seat)
    counts = counting(monkeypatch, svc.store)
    with post.reading_pass(store=svc.store, attempts=svc.attempts):
        lines = pending_lines(svc, seat)
    assert len([line for line in lines if 'waits: its order in hand' in line]) == WAITING
    # Each of the hand's 41 obligations is asked is_open once for the order in hand, not 40 times over.
    assert counts['is_open'] <= 3 * (WAITING + 1), counts
    assert counts['all'] <= 3, counts


def test_outside_a_pass_each_call_still_reads_afresh(hand):
    from quarterdeck import hand_door
    svc, seat, live, record = hand
    first, rest = orders(svc, seat)
    assert hand_door.held(seat, svc.store, svc.attempts, rest[0]) == f'waits: its order in hand {first.short} is open'
    svc.store.new('report', seat.name, first.sender, 'Done.', re=first.id)  # the order in hand closes
    assert hand_door.order_in_hand(svc.store, seat.name).id == rest[0].id, 'no reading kept between calls'
