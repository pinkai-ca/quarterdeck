"""Nothing is typed into a hand while its pull-request screen's hold stands (pr_screen): its conversation holds an
outsider's text. A message waits, unread and with no attempt, and goes once the hold is released; only the screen's
own instruction, named in the hold, is typed meanwhile. An unreadable hold types nothing. Status names the hand as
screening, so no seat picks it for work. Synthetic messages only."""
from __future__ import annotations

from quarterdeck import pr_screen, watch
from quarterdeck.door import Door
from .test_pending_delivery import setup  # noqa: F401


def typed(svc, message) -> list:
    return [attempt for attempt in svc.attempts.all() if attempt.message == message.id]


def test_an_order_to_a_held_hand_is_not_typed_and_goes_after_the_release(setup):
    svc, seat, live, _ = setup
    pr_screen.hold(svc.runtime, seat.name, '1-synthetic')
    order = svc.store.new('order', 'desk', seat.name, 'An invented order sent while the hand screens.')
    line = svc.deliver(seat, [order])
    assert 'waits: screening a pull request' in line
    assert live['pasted'] == live['submitted'] == 0 and not typed(svc, order), 'never typed, no attempt opened'
    assert [m.id for m in svc.attempts.undelivered(svc.store, seat.name)] == [order.id], 'it waits, still unread'
    svc.deliveries()  # the service's own pass holds it the same
    assert live['pasted'] == 0 and not typed(svc, order)
    pr_screen.release(svc.runtime, seat.name)
    svc.deliveries()
    assert live['pasted'] == 1 and typed(svc, order), 'it goes once the hold is released'


def test_only_the_instruction_named_in_the_hold_is_typed_while_it_stands(setup):
    svc, seat, live, _ = setup
    pr_screen.hold(svc.runtime, seat.name, '1-synthetic')
    instruction = svc.store.new('answer', 'ship', seat.name, 'An invented instruction from the screen.')
    other = svc.store.new('order', 'desk', seat.name, 'An invented order beside it.')
    pr_screen.admit(svc.runtime, seat.name, instruction.id)
    assert svc.deliver(seat, [instruction]).startswith(f'{instruction.short} to {seat.name} · ')
    assert typed(svc, instruction) and live['pasted'] == 1
    assert 'waits: screening' in svc.deliver(seat, [other]) and not typed(svc, other) and live['pasted'] == 1
    # The door itself types nothing else, by any path: no message, a control, another message's proof.
    assert Door(seat).deliver('/model synthetic', confirm=False).result == 'withheld'
    assert live['pasted'] == 1


def test_an_unreadable_hold_counts_as_held_and_types_nothing(setup):
    svc, seat, live, _ = setup
    order = svc.store.new('order', 'desk', seat.name, 'An invented order.')
    marker = pr_screen._screening_dir(svc.runtime) / f'{seat.name}.json'
    marker.parent.mkdir(parents=True)
    for unreadable in ('not json', '[]', '{"instruction": "%s"' % order.id, 'x' * (pr_screen.HOLD_BYTES + 1)):
        marker.write_text(unreadable)
        assert not pr_screen.admits(svc.runtime, seat.name, order.id), unreadable
        assert 'waits: screening' in svc.deliver(seat, [order])
    marker.unlink()
    marker.mkdir()  # any entry at the marker's name holds, whatever its kind
    assert not pr_screen.admits(svc.runtime, seat.name, order.id)
    marker.rmdir()
    marker.symlink_to(svc.runtime / 'elsewhere.json')  # never read through a link
    (svc.runtime / 'elsewhere.json').write_text('{"instruction": "%s"}' % order.id)
    assert not pr_screen.admits(svc.runtime, seat.name, order.id)
    assert 'waits: screening' in svc.deliver(seat, [order])
    assert live['pasted'] == 0 and not typed(svc, order)
    assert watch.reading(seat, svc.store).activity.state == 'screening'


def test_status_names_a_held_hand_screening_and_its_door_closed(setup):
    svc, seat, _, _ = setup
    before = watch.reading(seat, svc.store)
    assert before.activity.state != 'screening'
    pr_screen.hold(svc.runtime, seat.name, '1-synthetic')
    row = watch.reading(seat, svc.store)
    assert row.activity.state == 'screening' and str(before.activity) in row.activity.detail
    assert not row.door.open and row.door.reason == pr_screen.SCREENING
    assert any(f'{seat.name}: screening a pull request' in line and 'nothing is typed to it' in line
               for line in pr_screen.status(svc.runtime))
    pr_screen.release(svc.runtime, seat.name)
    assert watch.reading(seat, svc.store).activity.state != 'screening'
