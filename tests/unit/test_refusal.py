"""A crew seat whose provider refused its conversation starts fresh once, without the refused message.

Synthetic records and invented messages only. The relaunch itself is the ordinary lifecycle, replaced
here by a stand-in that takes the launch's first message exactly as ``Seat.first_message`` builds it.
"""
from __future__ import annotations

import pytest

from quarterdeck import post, refusal, turn_failure
from quarterdeck.seat import Seat
from quarterdeck.ship import ShipError
from tests.unit.refusal_ship import OTHER, REFUSED, World
from tests.unit.thread_reading import index_rows, thread_text


@pytest.fixture
def world(tmp_path, monkeypatch):
    built = World(tmp_path)
    built.fresh = []

    def fresh(seat):
        first, carried = seat.first_message()
        built.fresh.append((seat.name, first, [m.id for m in carried]))
        built.launch(seat.name)
        return f'{seat.name} fresh (stand-in)'
    monkeypatch.setattr(Seat, 'fresh', fresh)
    yield built
    built.svc.ledger.close()


def notices(world, to=None):
    return [m for m in world.svc.store.all() if m.sender == post.SHIP and (to is None or m.to == to)]


def test_a_refused_seat_starts_fresh_once_without_the_refused_message_and_its_sender_is_told(world):  # law: context-is-cache
    world.launch('worker')
    other = world.order('worker', 'An earlier invented order that stays open.', sender='spock')
    refused = world.order('worker', 'The invented order the provider refused.')
    world.turn('worker', refused, error=REFUSED)
    world.svc.refusals()
    assert len(world.fresh) == 1
    name, first, carried = world.fresh[0]
    assert carried == [other.id], 'every other open message is carried'
    assert other.id in first and f'qd thread {other.id}' in first, 'indexed, its body one command away'
    assert other.body in thread_text(world.svc.store, other.id, 'worker'), 'in full'
    assert refused.body not in first
    [row] = [row for row in index_rows(first) if refused.id in row['withheld']]
    assert row['notice'] == refusal.WITHHELD and row['subject'] is None, 'named, never retyped'
    assert refused.body not in thread_text(world.svc.store, refused.id, 'worker')
    told = notices(world, 'tester')
    assert len(told) == 1 and told[0].re == refused.id and told[0].kind == 'report'
    assert refused.body not in told[0].body and 'refused' in told[0].body
    assert world.svc.store.is_open(refused), 'the order stays open; nothing is closed or relabelled delivered'
    events = refusal.load(world.svc.runtime, 'worker')
    assert [e['stage'] for e in events] == ['launched']
    assert events[0]['quarantined'] == [{'message': refused.id, 'sender': 'tester', 'kind': 'order'}]
    world.svc.refusals()
    world.svc.refusals()
    assert len(world.fresh) == 1 and len(notices(world, 'tester')) == 1, 'once'


def test_the_refused_message_is_never_typed_again_by_a_tick_a_launch_or_a_resend(world):  # law: one-door
    world.launch('worker')
    refused = world.order('worker', 'The invented order the provider refused.')
    world.turn('worker', refused, error=REFUSED)
    world.svc.refusals()
    assert refused.id in refusal.excluded(world.svc.runtime, 'worker')
    assert refused.id not in {m.id for m in world.svc.attempts.undelivered(world.svc.store, 'worker')}
    first, carried = world.seat('worker').first_message()
    assert refused.id not in {m.id for m in carried} and refused.body not in first
    answer = world.svc.answer({'kind': 'resend', 'message': refused.id})
    assert 'not typed again' in answer


@pytest.mark.parametrize('case', ['two-messages', 'unmatched-input', 'no-input'])
def test_an_ambiguous_refused_input_is_held_and_shown_never_guessed(world, case):
    seat = world.launch('worker')
    world.event('worker', {'type': 'task_started'})
    if case == 'two-messages':
        world.typed('worker', world.order('worker', 'First invented.'), world.order('worker', 'Second invented.'))
    elif case == 'unmatched-input':
        world.human_input('worker', 'Something no door typed.')
    world.event('worker', {'type': 'task_complete', 'error': REFUSED, 'last_agent_message': None})
    world.svc.refusals()
    assert world.fresh == [] and notices(world) == []
    events = refusal.load(world.svc.runtime, 'worker')
    assert [e['stage'] for e in events] == ['held'] and events[0]['quarantined'] == [], 'nothing quarantined on a guess'
    assert refusal.excluded(world.svc.runtime, 'worker') == frozenset()
    reason = turn_failure.obstruction(seat)
    assert 'held:' in reason and 'not identified and must be kept out by hand before any qd fresh worker' in reason
    assert 'inspect, then qd fresh' not in reason, 'no unqualified cue to start it fresh by hand'
    world.svc.refusals()
    assert world.fresh == [], 'a held refusal is not retried by itself'


def test_a_fresh_conversation_refused_again_before_a_normal_turn_is_held_not_started_fresh_twice(world):
    world.launch('worker')
    world.turn('worker', world.order('worker', 'First invented refused order.'), error=REFUSED)
    world.svc.refusals()
    assert len(world.fresh) == 1
    second = world.order('worker', 'Second invented refused order.')
    world.turn('worker', second, error=REFUSED)
    world.svc.refusals()
    assert len(world.fresh) == 1
    held = refusal.load(world.svc.runtime, 'worker')[-1]
    assert held['stage'] == 'held' and [q['message'] for q in held['quarantined']] == [second.id]
    assert second.id in refusal.excluded(world.svc.runtime, 'worker') and len(notices(world, 'tester')) == 2
    assert 'not started fresh twice' in turn_failure.obstruction(world.seat('worker'))


def test_after_a_normal_turn_a_new_refusal_is_its_own_event(world):
    world.launch('worker')
    world.turn('worker', world.order('worker', 'First invented refused order.'), error=REFUSED)
    world.svc.refusals()
    world.turn('worker', world.order('worker', kind='answer'), last='done')
    world.turn('worker', world.order('worker', 'Later invented refused order.'), error=REFUSED)
    world.svc.refusals()
    assert len(world.fresh) == 2


@pytest.mark.parametrize('seat_name,error', [('worker', OTHER), ('aide', REFUSED)], ids=['other-error', 'human-session'])
def test_no_automatic_fresh_for_another_error_or_a_human_session(world, seat_name, error):
    world.launch(seat_name)
    world.turn(seat_name, world.order(seat_name), error=error)
    world.svc.refusals()
    assert world.fresh == [] and refusal.load(world.svc.runtime, seat_name) == []


def test_an_open_turn_or_queued_input_waits(world):
    world.launch('worker')
    world.turn('worker', world.order('worker'), error=REFUSED)
    world.event('worker', {'type': 'task_started'})
    world.svc.refusals()
    assert world.fresh == [] and refusal.load(world.svc.runtime, 'worker') == []


def test_a_claim_survives_a_crash_and_is_adopted_or_held_never_repeated(world, monkeypatch):
    world.launch('worker')
    refused = world.order('worker', 'The invented order the provider refused.')
    world.turn('worker', refused, error=REFUSED)
    monkeypatch.setattr(Seat, 'fresh', lambda seat: (_ for _ in ()).throw(RuntimeError('synthetic service death')))
    with pytest.raises(RuntimeError):
        world.svc.refusals()  # the service dies mid-relaunch, after its durable claim
    events = refusal.load(world.svc.runtime, 'worker')
    assert [e['stage'] for e in events] == ['claimed'] and len(notices(world, 'tester')) == 1
    world.launch('worker')  # someone else's explicit fresh happened meanwhile
    world.svc.refusals()
    assert [e['stage'] for e in refusal.load(world.svc.runtime, 'worker')] == ['launched']
    assert len(notices(world, 'tester')) == 1, 'the fixed notice is published once'


def test_a_failed_relaunch_is_held_once_shown_and_never_retried(world, monkeypatch):
    world.launch('worker')
    refused = world.order('worker', 'The invented order the provider refused.')
    world.turn('worker', refused, error=REFUSED)
    tried = []

    def failing(seat):
        tried.append(seat.name)
        raise ShipError('synthetic lifecycle refusal')
    monkeypatch.setattr(Seat, 'fresh', failing)

    def failures():
        lines = world.svc.log_path.read_text().splitlines() if world.svc.log_path.exists() else []
        return [line for line in lines if '"step": "refusals"' in line]
    world.svc.refusals()
    events = refusal.load(world.svc.runtime, 'worker')
    assert tried == ['worker'] and len(failures()) == 1
    assert [e['stage'] for e in events] == ['held'] and 'synthetic lifecycle refusal' in events[0]['reason']
    assert refusal.excluded(world.svc.runtime, 'worker') == {refused.id}, 'the quarantine stands'
    assert len(notices(world, 'tester')) == 1, 'the sender was told once'
    reason = turn_failure.obstruction(world.seat('worker'))
    assert 'held:' in reason and 'inspect, then qd fresh worker' in reason
    world.svc.refusals()
    world.svc.refusals()
    assert tried == ['worker'] and len(failures()) == 1, 'no retry and no second log on later ticks'
    assert refusal.load(world.svc.runtime, 'worker') == events and len(notices(world, 'tester')) == 1


def test_an_unreadable_journal_holds_every_launch(world):
    seat = world.launch('worker')
    refusal.path(world.svc.runtime, 'worker').write_text('{not json')
    with pytest.raises(ShipError, match='refusal journal unreadable'):
        seat.first_message()
    world.turn('worker', world.order('worker'), error=REFUSED)
    world.svc.refusals()
    assert world.fresh == []
    assert 'no automatic fresh' in turn_failure.obstruction(seat)


def test_the_inbox_shows_the_refused_message_as_a_notice_and_the_store_keeps_its_text(world):  # law: context-is-cache
    world.launch('worker')
    refused = world.order('worker', 'The invented order the provider refused.')
    kept = world.order('worker', 'Another invented order, shown whole.')
    world.turn('worker', refused, error=REFUSED)
    world.svc.refusals()
    text = post.inbox_text(world.svc.store, 'worker')
    assert refused.body not in text and refusal.WITHHELD in text and refused.short in text
    assert kept.body in text
    reading = post.inbox_reading(world.svc.store, 'worker')
    assert [m.body for m in reading['open'] if m.id == refused.id] == [refusal.WITHHELD], 'the JSON reading too'
    assert world.svc.store.get(refused.id).body == refused.body, 'the post itself is unchanged'


def test_an_unknown_tool_set_holds_only_the_fresh_start_and_still_quarantines_and_tells(world, monkeypatch):  # law: context-is-cache
    world.launch('worker')
    other = world.order('worker', 'An earlier invented order that stays open.', sender='spock')
    refused = world.order('worker', 'The invented order the provider refused.')
    world.event('worker', {'type': 'task_started'})
    world.typed('worker', refused)
    world.item('worker', {'type': 'future_execution_item'})
    world.event('worker', {'type': 'task_complete', 'error': REFUSED, 'last_agent_message': None})
    publish = refusal._publish

    def dies(store, message):
        raise RuntimeError('synthetic service death after the durable hold')
    monkeypatch.setattr(refusal, '_publish', dies)
    with pytest.raises(RuntimeError):
        world.svc.refusals()
    events = refusal.load(world.svc.runtime, 'worker')
    assert [e['stage'] for e in events] == ['held'] and 'tool set is unknown' in events[0]['reason']
    assert events[0]['quarantined'] == [{'message': refused.id, 'sender': 'tester', 'kind': 'order'}]
    assert refusal.excluded(world.svc.runtime, 'worker') == {refused.id} and notices(world) == []
    monkeypatch.setattr(refusal, '_publish', publish)
    for _ in range(3):
        world.svc.refusals()
    assert world.fresh == [] and refusal.load(world.svc.runtime, 'worker') == events
    told = notices(world, 'tester')
    assert len(told) == 1 and told[0].re == refused.id and refused.body not in told[0].body, 'one notice, completed after the crash'
    reason = turn_failure.obstruction(world.seat('worker'))
    assert 'withheld; inspect, then qd fresh worker' in reason
    first, carried = world.seat('worker').first_message()  # a person's qd fresh
    assert [m.id for m in carried] == [other.id] and other.id in first and f'qd thread {other.id}' in first
    assert other.body in thread_text(world.svc.store, other.id, 'worker')
    assert refused.body not in first
    [row] = [row for row in index_rows(first) if refused.id in row['withheld']]
    assert row['notice'] == refusal.WITHHELD and row['subject'] is None, 'named, never retyped'
    assert refused.body not in thread_text(world.svc.store, refused.id, 'worker')
    assert world.svc.store.is_open(refused) and world.svc.store.get(refused.id).body == refused.body


def test_a_held_refusal_survives_a_later_change_of_its_notices_words(world, monkeypatch):
    """A notice already published is recognized by its id; republication never rebuilds its text."""
    world.launch('worker')
    refused = world.order('worker', 'The invented order the provider refused.')
    world.turn('worker', refused, error=REFUSED)
    monkeypatch.setattr(Seat, 'fresh', lambda seat: (_ for _ in ()).throw(ShipError('synthetic relaunch failure')))
    world.svc.refusals()
    events = refusal.load(world.svc.runtime, 'worker')
    assert [e['stage'] for e in events] == ['held'] and len(notices(world, 'tester')) == 1
    published = notices(world, 'tester')[0]
    original = refusal.notice
    monkeypatch.setattr(refusal, 'notice', lambda event, entry, seat: post.Message(
        *[getattr(original(event, entry, seat), f) for f in ('id', 'kind', 'sender', 'to', 'at')],
        'Synthetic reworded notice text, as a later version might write it.', re=entry['message']))
    logged = []
    monkeypatch.setattr(world.svc, 'log', lambda *args, **kwargs: logged.append((args, kwargs)))
    for _ in range(3):
        world.svc.refusals()
    assert not any(kwargs.get('step') == 'refusals' for _, kwargs in logged), 'no conflict, on any tick'
    assert notices(world, 'tester') == [published], 'the published notice stands, unrebuilt, and is not doubled'


def test_one_seats_refusal_failure_never_stops_the_next_seat_and_is_logged_once(world, monkeypatch):
    seen, logged = [], []
    original = refusal.step

    def step(service, seat):
        seen.append(seat.name)
        if seat.name == 'worker':
            raise ShipError('synthetic notice conflict')
        return original(service, seat)
    monkeypatch.setattr(refusal, 'step', step)
    monkeypatch.setattr(world.svc, 'log', lambda *args, **kwargs: logged.append(kwargs))
    for _ in range(3):
        world.svc.refusals()
    assert seen.count('tester') == 3, 'the seats after it still ran, every tick'
    errors = [entry for entry in logged if entry.get('step') == 'refusals' and entry.get('seat') == 'worker']
    assert len(errors) == 1 and 'synthetic notice conflict' in errors[0]['error'], 'logged once while it stands'


OTHER_SHIP = '''[ship]
name = "other"
operator = "operator"
[pools.test]
provider = "codex"
[sessions.crew]
title = "Crew"
[seats.lead]
session = "crew"
role = "worker"
harness = "codex"
pool = "test"
'''


@pytest.fixture
def fleet_world(tmp_path, monkeypatch):
    """The refusal world as one ship of a fleet: ships/synthetic, beside ships/other with its seat 'lead'."""
    here = tmp_path / 'ships' / 'synthetic'
    here.mkdir(parents=True)
    (tmp_path / 'ships' / 'other').mkdir()
    (tmp_path / 'ships' / 'other' / 'ship.toml').write_text(OTHER_SHIP)
    built = World(here)
    built.fresh = []

    def fresh(seat):
        first, carried = seat.first_message()
        built.fresh.append((seat.name, first, [m.id for m in carried]))
        built.launch(seat.name)
        return f'{seat.name} fresh (stand-in)'
    monkeypatch.setattr(Seat, 'fresh', fresh)
    yield built
    built.svc.ledger.close()


def test_a_refused_message_from_another_ships_seat_is_told_through_the_fleet_post(fleet_world):  # law: post-is-files
    from quarterdeck import fleet_post as fleet, ship as shipmod
    world = fleet_world
    world.launch('worker')
    other = shipmod.load(world.svc.ship.folder.parent / 'other')
    sent = post.Store(other.runtime).new('order', 'lead', 'worker@synthetic',
                                         'An invented order from another ship that the provider refused.')
    fleet.run(other, post.Store(other.runtime))  # filed on other, carried by the fleet post
    fleet.run(world.svc.ship, world.svc.store)
    [refused] = [m for m in world.svc.store.all() if m.body == sent.body]  # an imported id names its ship
    assert refused.sender == 'lead@other' and refused.to == 'worker'
    world.turn('worker', refused, error=REFUSED)
    world.svc.refusals()
    [told] = notices(world, 'lead@other')
    assert told.re == refused.id and told.kind == 'report' and refused.body not in told.body
    events = refusal.load(world.svc.runtime, 'worker')
    assert [n['message'] for n in events[0]['notices']] == [refused.id], 'the event holds the notice it made'
    assert refusal.told(world.svc.runtime, 'worker') == {refused.id}
    [row] = [row for row in index_rows(world.fresh[0][1]) if refused.id in row['withheld']]
    assert row['notice'] == refusal.WITHHELD, 'its sender was told, and the stub says so'
    fleet.run(world.svc.ship, world.svc.store)  # the fleet post carries the notice as the same file
    fleet.run(other, post.Store(other.runtime))
    arrived = [m for m in post.Store(other.runtime).all() if m.kind == 'report' and m.sender != 'lead'
               and m.body == told.body]
    assert arrived and arrived[0].re == sent.id and arrived[0].to == 'lead', [(m.re, m.to) for m in arrived]


def test_a_refused_message_from_no_seat_says_its_sender_was_not_told(world):
    world.launch('worker')
    refused = world.order('worker', 'An invented order the ship itself sent, which the provider refused.',
                          sender=post.SHIP)
    world.turn('worker', refused, error=REFUSED)
    world.svc.refusals()
    assert notices(world, post.SHIP) == [] and refusal.told(world.svc.runtime, 'worker') == frozenset()
    events = refusal.load(world.svc.runtime, 'worker')
    assert events[0]['notices'] == [] and [q['message'] for q in events[0]['quarantined']] == [refused.id]
    [row] = [row for row in index_rows(world.fresh[0][1]) if refused.id in row['withheld']]
    assert row['notice'] == refusal.NOT_TOLD, 'the stub never claims a notice the event does not hold'
    text = post.inbox_text(world.svc.store, 'worker')
    assert refusal.NOT_TOLD in text and refusal.WITHHELD not in text and refused.body not in text


def test_a_seats_refused_message_with_no_notice_held_says_plainly_it_was_not_told(world):
    """A seat of this ship whose refused message has no notice in its event (a journal written so by hand): the stub
    says only that its sender was not told, never that it is not a seat."""
    world.launch('worker')
    refused = world.order('worker', 'An invented order from a seat, refused, with no notice held.')
    refusal.save(world.svc.runtime, 'worker', [{'stage': 'held', 'launch': {}, 'turn': None, 'notices': [],
                                                 'quarantined': [{'message': refused.id}]}])
    text = post.inbox_text(world.svc.store, 'worker')
    assert refusal.UNTOLD in text and refusal.NOT_TOLD not in text and refusal.WITHHELD not in text
    words = refusal.withheld_words
    assert words(False, True) == refusal.UNTOLD and words(False, None) == refusal.UNTOLD
    assert words(False, False) == refusal.NOT_TOLD and words(True, False) == refusal.WITHHELD


def test_the_unsettled_count_counts_every_kind_until_it_reaches_its_addressee(world):
    """As the waiting count reads it: an unsettled message counts until it has reached its addressee, whatever its
    kind; an order since reported on drops out; one that cannot be read stays counted."""
    from quarterdeck import post_reads, watch
    unsettled = sorted(post.UNSETTLED)[0]
    world.launch('worker')
    report = world.svc.store.new('report', 'tester', 'worker', 'An invented report whose delivery was unsettled.')
    attempt = world.svc.attempts.open(report, 'worker', '%1', 1, None)
    world.svc.attempts.close(attempt, unsettled, 'synthetic unsettled')
    assert watch._unsettled(world.svc.store, {report.id: unsettled}) == 1, 'a report not yet delivered counts'
    post_reads.record(world.svc.runtime, 'worker', [report.id])  # the seat read it whole: it has reached the seat
    assert watch._unsettled(post.Store(world.svc.runtime), {report.id: unsettled}) == 0
    assert watch._unsettled(world.svc.store, {report.id: 'delivered'}) == 0, 'a later delivery settles it'
    order = world.order('worker', 'An invented order whose delivery was left unsettled.')
    latest = {order.id: unsettled}
    assert watch._unsettled(post.Store(world.svc.runtime), latest) == 1
    world.svc.store.new('report', 'worker', 'tester', 'Invented report.', re=order.id)
    assert watch._unsettled(post.Store(world.svc.runtime), latest) == 0, 'reported on: no longer counted'
    assert watch._unsettled(world.svc.store, {'ord-missing-zzzz': unsettled}) == 1, 'unreadable stays'


def test_an_inbox_render_pass_reads_each_journal_once_and_the_ship_at_most_once(world, monkeypatch):
    from quarterdeck import service_inbox, ship as shipmod
    world.launch('worker')
    refused = world.order('worker', 'An invented order the provider refused, with no notice held.')
    refusal.save(world.svc.runtime, 'worker', [{'stage': 'held', 'launch': {}, 'turn': None, 'notices': [],
                                                 'quarantined': [{'message': refused.id}]}])
    for name in world.svc.ship.seats:
        world.svc.store.new('report', 'ship', name, f'An invented report to {name}, so its inbox changes.')
    journals, ships = [], []
    load, load_ship = refusal.load, shipmod.load
    monkeypatch.setattr(refusal, 'load', lambda runtime, seat: journals.append(seat) or load(runtime, seat))
    monkeypatch.setattr(shipmod, 'load', lambda folder: ships.append(folder) or load_ship(folder))
    store = post.Store(world.svc.runtime)
    succeeded, _ = service_inbox.render_pass(world.svc, store)
    assert succeeded
    assert sorted(journals) == sorted(world.svc.ship.seats), 'each seat journal read once in the pass'
    assert len(ships) <= 1, 'the ship read at most once in the pass'
    text = (world.svc.runtime / 'post' / 'inbox' / 'worker.md').read_text()
    assert refusal.UNTOLD in text and refused.body not in text, 'the stub is the same as without the pass context'
