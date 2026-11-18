"""A crew candidate decision shares reads; every delivery/alarm action leaves that view."""

from datetime import timedelta
import json
import os
from types import SimpleNamespace

import pytest

from quarterdeck import auth_failure, human_wake, post, recovery, service, turn_failure
from quarterdeck.service import Service


def world(tmp_path):
    runtime = tmp_path / 'runtime'
    seat = SimpleNamespace(name='worker', session=SimpleNamespace(human=False), is_up=True,
                           ship=SimpleNamespace(runtime=runtime),
                           launched=lambda: SimpleNamespace(at='2031-01-01T00:00:00+00:00'))
    svc = object.__new__(Service)
    svc.ship = SimpleNamespace(runtime=runtime, forward={})
    svc.store, svc.attempts = post.Store(runtime), post.Attempts(runtime)
    svc.withheld = {}
    svc.resolved_seats = lambda: [seat]
    delivered, alarms = [], []

    def deliver(subject, messages):
        assert post._READING.get() is None
        delivered.extend(m.id for m in messages)
        for message in messages:
            attempt = svc.attempts.open(message, subject.name, '%1', 123, subject.launched().at)
            svc.attempts.close(attempt, 'delivered')

    def alarm(subject):
        assert post._READING.get() is None
        alarms.append(subject.name)

    svc.deliver, svc.needs_human = deliver, alarm
    return SimpleNamespace(svc=svc, seat=seat, delivered=delivered, alarms=alarms)


@pytest.mark.parametrize('result,expected', [(None, True), ('withheld', True), ('blind', False),
                                             ('delivered', False), ('queued', False),
                                             ('launch', False), ('watch', False)])
def test_existing_candidate_decisions_are_preserved(tmp_path, result, expected):
    w = world(tmp_path)
    message = w.svc.store.new('order', 'operator', w.seat.name, 'Synthetic work')
    if result is not None:
        attempt = w.svc.attempts.open(message, w.seat.name, '%1', 123, w.seat.launched().at)
        w.svc.attempts.close(attempt, result)
    w.svc.deliveries()
    assert w.delivered == ([message.id] if expected else [])
    assert w.alarms == [w.seat.name]
    assert post._READING.get() is None


@pytest.mark.parametrize('latest_result,expected', [('delivered', False), ('blind', False), ('withheld', True)])
def test_latest_attempt_from_another_launch_still_decides(tmp_path, latest_result, expected):
    w = world(tmp_path)
    message = w.svc.store.new('order', 'operator', w.seat.name, 'Synthetic work')
    first = w.svc.attempts.open(message, w.seat.name, '%1', 123, w.seat.launched().at)
    w.svc.attempts.close(first, 'withheld')
    other_launch = (post.now() - timedelta(days=1)).isoformat()
    latest = w.svc.attempts.open(message, w.seat.name, '%9', 999, other_launch)
    w.svc.attempts.close(latest, latest_result)
    assert w.svc.attempts.latest()[message.id] == latest_result
    w.svc.deliveries()
    assert w.delivered == ([message.id] if expected else [])
    assert w.alarms == [w.seat.name]


def test_withheld_screen_is_compared_on_each_tick_and_a_change_retries(tmp_path, monkeypatch):
    w = world(tmp_path)
    message = w.svc.store.new('order', 'operator', w.seat.name, 'Synthetic work')
    attempt = w.svc.attempts.open(message, w.seat.name, '%1', 123, w.seat.launched().at)
    w.svc.attempts.close(attempt, 'withheld')
    w.svc.withheld[(message.id, w.seat.name)] = 'same screen'
    screen = ['same screen']
    comparisons = []

    def reading():
        assert post._READING.get() is not None
        comparisons.append(screen[0])
        return SimpleNamespace(text=screen[0])

    monkeypatch.setattr(service, 'Door', lambda seat: SimpleNamespace(screen=reading))
    for _ in range(3):
        w.svc.deliveries()
        assert not w.delivered
    screen[0] = 'changed screen'
    w.svc.deliveries()
    assert comparisons == ['same screen'] * 3 + ['changed screen']
    assert w.delivered == [message.id]
    w.svc.deliveries()
    assert w.delivered == [message.id] and len(comparisons) == 4
    assert len(w.alarms) == 5


def test_actions_and_their_writes_are_outside_and_next_seat_sees_a_new_post(tmp_path, monkeypatch):
    w = world(tmp_path)
    other = SimpleNamespace(name='other', session=SimpleNamespace(human=False), is_up=True,
                            ship=w.seat.ship, launched=w.seat.launched)
    w.svc.resolved_seats = lambda: [w.seat, other]
    first = w.svc.store.new('order', 'operator', w.seat.name, 'First task')
    writer = post.Store(w.svc.ship.runtime)
    # The reader starts with a certified listing; its own cache does not see the writer.
    monkeypatch.setattr(post.time, 'time_ns', lambda: w.svc.store.dir.stat().st_ctime_ns + 10_000_000_000)
    with post.reading_pass(store=w.svc.store, delivery_candidates=True):
        assert w.svc.store.for_seat(w.seat.name) == [first]
    assert w.svc.store._cache_at is not None
    original_write = post.Store._write

    def write(store, message):
        assert post._READING.get() is None
        return original_write(store, message)

    monkeypatch.setattr(post.Store, '_write', write)
    deliver = w.svc.deliver
    posted = []

    def action(seat, messages):
        deliver(seat, messages)
        if seat.name == w.seat.name:
            stamp = writer.dir.stat().st_mtime_ns
            posted.append(writer.new('order', 'operator', other.name, 'Next task'))
            os.utime(writer.dir, ns=(stamp, stamp + 1))

    w.svc.deliver = action
    w.svc.deliveries()
    assert w.delivered == [first.id, posted[0].id]
    assert w.alarms == [w.seat.name, other.name]
    w.svc.deliveries()
    assert w.delivered == [first.id, posted[0].id]
    assert w.alarms == [w.seat.name, other.name] * 2


def test_quiet_passes_reuse_indexes_and_still_read_each_alarm(tmp_path, monkeypatch):
    w = world(tmp_path)
    other = SimpleNamespace(name='other', session=SimpleNamespace(human=False), is_up=True,
                            ship=w.seat.ship, launched=w.seat.launched)
    w.svc.resolved_seats = lambda: [w.seat, other]
    message = w.svc.store.new('order', 'operator', w.seat.name, 'Synthetic work')
    attempt = w.svc.attempts.open(message, w.seat.name, '%1', 123, w.seat.launched().at)
    w.svc.attempts.close(attempt, 'delivered')
    monkeypatch.setattr(post.time, 'time_ns', lambda: w.svc.store.dir.stat().st_ctime_ns + 10_000_000_000)
    folds, inventories = [], []
    latest, inventory = w.svc.attempts.latest, w.svc.attempts._inventory_metadata

    def count_fold(**kwargs):
        folds.append(True)
        return latest(**kwargs)

    def count_inventory():
        inventories.append(True)
        return inventory()

    monkeypatch.setattr(w.svc.attempts, 'latest', count_fold)
    monkeypatch.setattr(w.svc.attempts, '_inventory_metadata', count_inventory)
    w.svc.deliveries()
    first_index = w.svc.store._delivery_recipients
    assert first_index is not None
    w.svc.deliveries()
    assert w.svc.store._delivery_recipients is first_index
    assert len(folds) == 1 and len(inventories) == 2
    assert not w.delivered and w.alarms == [w.seat.name, other.name] * 2


def test_changed_or_unreadable_attempt_inventory_discards_candidate_results(tmp_path):
    w = world(tmp_path)
    message = w.svc.store.new('order', 'operator', w.seat.name, 'Synthetic work')
    attempt = w.svc.attempts.open(message, w.seat.name, '%1', 123, w.seat.launched().at)
    w.svc.attempts.close(attempt, 'queued')
    first = w.svc.attempts.delivery_results()
    assert first[message.id] == 'queued'
    saved = json.loads(attempt.path.read_bytes())
    directory_stamp = attempt.path.parent.stat().st_mtime_ns
    attempt.path.write_text('{broken')
    with pytest.raises(json.JSONDecodeError):
        w.svc.attempts.delivery_results()
    attempt.path.write_text(json.dumps(saved | {'result': 'launch'}))
    assert attempt.path.parent.stat().st_mtime_ns == directory_stamp
    assert w.svc.attempts.delivery_results()[message.id] == 'launch'


def test_uncertified_store_still_selects_from_the_current_listing(tmp_path, monkeypatch):
    w = world(tmp_path)
    message = w.svc.store.new('order', 'operator', w.seat.name, 'Synthetic work')
    monkeypatch.setattr(post.time, 'time_ns', lambda: w.svc.store.dir.stat().st_ctime_ns)
    w.svc.deliveries()
    assert w.svc.store._delivery_recipients is None
    assert w.delivered == [message.id] and w.alarms == [w.seat.name]


def test_human_wake_stays_outside_the_crew_reading_scope(tmp_path, monkeypatch):
    w = world(tmp_path)
    w.seat.session.human = True
    monkeypatch.setattr(auth_failure, 'status', lambda seat: None)
    monkeypatch.setattr(recovery, 'held', lambda seat: None)
    monkeypatch.setattr(turn_failure, 'obstruction', lambda seat, store: None)
    wakes = []

    def wake(seat):
        assert post._READING.get() is None
        wakes.append(seat.name)

    monkeypatch.setattr(human_wake, 'run', wake)
    w.svc.deliveries()
    assert wakes == [w.seat.name] and not w.delivered
    assert w.alarms == [w.seat.name]
