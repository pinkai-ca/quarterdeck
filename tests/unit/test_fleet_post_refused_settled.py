"""A message its addressee's provider refused, once it has crossed and its refusal notice has crossed back, is settled
on both ships: neither waiting nor held. Its descendants stay held, and no refused words cross. Two synthetic ships in
a throwaway project; a synthetic refusal journal entry."""
from __future__ import annotations

from quarterdeck import fleet_post as fleet, refusal
from tests.unit.test_fleet_post import pair  # noqa: F401  (two synthetic ships, alpha and beta)
from tests.unit.test_fleet_post_claude_gates import quarantine

NOTICE = 'rep-20400101-0000-ship-settled'


def refused_and_told(pair, body):
    """alpha's writer orders beta's captain; beta's provider refuses it; beta tells alpha. Each ship runs its pass."""
    left, right, source, destination = pair
    order = source.new('order', 'writer', 'captain@beta', body)
    assert fleet.run(left, source) == [] and fleet.run(right, destination) == []
    received = destination.get(order.id)
    quarantine(right.runtime, 'captain', received, NOTICE)
    event = refusal.load(right.runtime, 'captain')[0]
    entry = dict(message=received.id, sender=received.sender, kind=received.kind)
    refusal._publish(destination, refusal.notice(event, entry, 'captain'))  # as the refusal step publishes it
    return order


def names(lines, message):
    return [line for line in lines if message.short in line or 'waiting' in line]


def test_a_refused_message_whose_notice_has_crossed_is_neither_waiting_nor_held_on_either_ship(pair):
    left, right, source, destination = pair
    order = refused_and_told(pair, 'Synthetic body the provider refused.')
    fleet.run(right, destination), fleet.run(left, source)  # beta offers the notice; alpha fetches it (after its export)
    assert source.get(NOTICE).re == order.id, 'the refusal notice crossed back to the sender'
    lines = []
    for _ in range(2):  # every later pass on each ship, and their status lines
        lines += fleet.run(left, source) + fleet.run(right, destination)
    lines += fleet.status(left, source) + fleet.status(right, destination)
    assert names(lines, order) == []
    assert fleet.waiting(left, source) == [] and fleet.waiting(right, destination) == []
    told = [m for m in source.all() if m.re == order.id and m.kind == 'report']
    assert [m.id for m in told] == [NOTICE], 'the notice the laws ask for remains, once'


def test_before_its_notice_crosses_the_refused_message_is_still_held(pair):
    left, right, source, destination = pair
    order = refused_and_told(pair, 'Synthetic body refused, notice not yet crossed.')
    held = fleet.run(left, source)  # alpha's pass before beta offers the notice
    assert any(order.short in line and 'outbound held' in line for line in held)
    assert fleet.waiting(left, source) != []


def test_a_descendant_of_the_refused_message_is_still_held_and_carries_no_refused_words(pair):
    left, right, source, destination = pair
    body = 'Synthetic body refused, then replied to.'
    order = refused_and_told(pair, body)
    for _ in range(2):
        fleet.run(right, destination), fleet.run(left, source)
    follow = source.new('question', 'writer', 'captain@beta', 'Did my refused order reach you?', re=order.id)
    held = fleet.run(left, source)
    assert any(follow.short in line and 'outbound held' in line for line in held), held
    assert not any(order.short in line for line in held), 'the settled root is not itself held again'
    fleet.run(right, destination)
    assert not (right.runtime / 'post' / 'store' / f'{follow.id}.json').exists(), 'the descendant did not cross'
    assert not [path for path in right.runtime.rglob('*') if path.is_file() and body.encode() in path.read_bytes()
                and path.name != f'{order.id}.json'], "the refused words reached beta beyond the order's own import"
    assert any('waiting' in line for line in fleet.waiting(left, source)), 'the held descendant still waits'
