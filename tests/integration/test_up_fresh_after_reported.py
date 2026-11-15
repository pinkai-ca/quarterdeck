"""The actual fresh/resume launch and carriage, on a private fake-only ship."""

from dataclasses import replace

import pytest

from quarterdeck import post, seat as seatmod, ship as shipmod
from .waiting import wait_for


@pytest.mark.parametrize('policy,reply,open_order,fresh', [
    ('after-order', 'report', False, True),
    ('after-order', 'report', True, False),
    ('after-order', 'ack', False, False),
    (None, 'report', False, False),
])
def test_up_uses_normal_fresh_carriage_or_resumes_the_record(drydock, tmux, policy, reply, open_order, fresh):
    ship = shipmod.load(drydock)
    spec = replace(ship.seat('captain'), fresh=policy,
                   settings={**ship.seat('captain').settings, 'step_seconds': '0.01'})
    ship = replace(ship, seats={**ship.seats, spec.name: spec})
    seat = seatmod.Seat(ship, spec, tmux)
    store = post.Store(ship.runtime)
    order = store.new('order', 'desk', seat.name, 'Ordinary synthetic task')
    seat.up()
    old = wait_for(seat.record)
    wait_for(lambda: seat.harness.state(old).turn_open is False)
    assert post.Attempts(ship.runtime).latest(seat.name)[order.id] == 'launch'
    store.new(reply, seat.name, 'desk', 'Ordinary completion or acknowledgment', re=order.id)
    if open_order:
        store.new('order', 'desk', seat.name, 'Ordinary next task')
    seat.down()
    # Non-duty incoming information still travels through the normal carriage.
    incoming = store.new('answer', 'desk', seat.name, 'Ordinary incoming information')
    proof = seat.up()
    record = wait_for(seat.record)
    wait_for(lambda: any(incoming.id in t.input for t in seat.harness.turns(record)))
    assert old.exists(), 'completed native records stay available to accounting'
    assert (record != old) == fresh
    assert (seat.launched().resume is None) == fresh
    assert ('fresh: its last order is reported' in proof) == fresh
    assert (ship.runtime / 'brief' / seat.name).with_suffix('.md').read_text().strip()
