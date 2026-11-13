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
