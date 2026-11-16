"""The drill advances on settled evidence; time is only an outer stuck-run guard."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from quarterdeck import drill, lifecycle, service_wait
from quarterdeck.ship import ShipError


@pytest.fixture
def waiting(tmp_path, monkeypatch):
    clock = [0.0]
    exercise = drill.Drill(SimpleNamespace(runtime=tmp_path), Mock(), patience=30,
                           settlement_clock=lambda: clock[0])
    order = exercise.store.new('order', 'ship', 'worker', 'Ordinary synthetic pending work')
    seat = SimpleNamespace(name='worker', session=SimpleNamespace(human=False),
                           fresh=Mock(return_value='fresh proof'),
                           launched=lambda: SimpleNamespace(at='new launch'))
    attempt = exercise.attempts.open(order, seat.name, '%synthetic', 123, 'old launch')
    exercise.attempts.close(attempt, 'delivered', 'Ordinary settled receipt')
    phases = ['submitted']
    monkeypatch.setattr(drill, 'journals', lambda seat: (
        [SimpleNamespace(data={'phase': phases[0]})], []))
    def unexpected_sleep(seconds):
        pytest.fail('settlement used a clock sleep instead of its event wait')
    monkeypatch.setattr(drill.time, 'sleep', unexpected_sleep)
    class Events:
        def __init__(self):
            self.refreshes, self.waits, self.closed = [], [], 0
            self.on_wait = lambda: None
        def refresh(self, names, *, extra=()):
            self.refreshes.append((tuple(names), tuple(extra)))
        def wait(self, seconds):
            assert 0 <= seconds <= drill.SETTLEMENT_FALLBACK
            self.waits.append(seconds)
            clock[0] += seconds
            self.on_wait()
            return False
        def close(self):
            self.closed += 1
    events = Events()
    monkeypatch.setattr(drill.service_wait, 'Arrivals', lambda runtime: events)
    return SimpleNamespace(exercise=exercise, order=order, seat=seat, phases=phases,
                           events=events, clock=clock, runtime=tmp_path)


def test_late_settlement_event_succeeds_after_the_old_thirty_second_window(waiting):
    e = waiting
    def settle():
        e.seat.fresh.assert_not_called()
        if e.clock[0] >= 35:
            e.phases[0] = 'delivered'
    e.events.on_wait = settle
    assert e.exercise.fresh_after_delivery(e.seat, e.order) == ('fresh proof', 'delivered first')
    assert e.clock[0] == 35 and len(e.events.waits) == 35
    e.seat.fresh.assert_called_once_with()
    assert e.events.closed == 1
    assert e.events.refreshes[0] == (('worker',), (
        e.runtime / 'post/attempts', e.runtime / 'post/pending', e.runtime / 'post/pending/worker'))


@pytest.mark.parametrize('failure', ['submitted', 'unreadable', 'unfinished'])
def test_outer_guard_names_unsettled_evidence_and_never_starts_fresh(waiting, monkeypatch, failure):
    e = waiting
    e.exercise.patience = 1
    if failure == 'unreadable':
        monkeypatch.setattr(drill, 'journals', lambda seat: ([], ['ordinary unreadable evidence']))
    elif failure == 'unfinished':
        e.exercise.attempts.open(e.order, e.seat.name, '%synthetic', 123, 'old launch')
    proof, reason = e.exercise.fresh_after_delivery(e.seat, e.order)
    assert proof is None
    assert 'outer settlement guard expired after 4.0s (bound 4.0s)' in reason
    assert {'submitted': 'still being typed or confirmed', 'unreadable': 'unreadable',
            'unfinished': 'unfinished'}[failure] in reason
    e.seat.fresh.assert_not_called()
    assert e.events.closed == 1 and len(e.events.waits) == 4


def test_lock_release_without_a_directory_event_gets_a_bounded_recheck(waiting):
    e = waiting
    e.phases[0] = 'delivered'
    calls = []
    def fresh():
        if e.clock[0] < 2:
            raise lifecycle.Busy('ordinary entry claim is held')
        calls.append('transition body')
        return 'fresh proof'
    e.seat.fresh = fresh
    assert e.exercise.fresh_after_delivery(e.seat, e.order) == ('fresh proof', 'delivered first')
    assert calls == ['transition body'] and len(e.events.waits) == 2
    assert e.events.closed == 1


def test_a_started_fresh_failure_is_not_retried_and_closes_the_watcher(waiting):
    e = waiting
    e.phases[0] = 'delivered'
    e.seat.fresh.side_effect = ShipError('ordinary launch failure after entry')
    with pytest.raises(ShipError, match='launch failure'):
        e.exercise.fresh_after_delivery(e.seat, e.order)
    e.seat.fresh.assert_called_once_with()
    assert e.events.closed == 1 and e.events.waits == []


def test_settlement_clock_is_local_and_a_frozen_guard_waits_for_the_event(waiting, monkeypatch):
    e = waiting
    e.exercise.patience = .2
    e.exercise.settlement_clock = lambda: 0.0
    def unexpected_clock():
        pytest.fail('an injected settlement guard read the real clock')
    monkeypatch.setattr(drill, 'time', SimpleNamespace(monotonic=unexpected_clock))
    e.events.on_wait = lambda: e.phases.__setitem__(0, 'delivered')
    assert e.exercise.fresh_after_delivery(e.seat, e.order) == ('fresh proof', 'delivered first')
    assert e.events.waits == [.8] and e.events.closed == 1
    e.seat.fresh.assert_called_once_with()


def test_default_settlement_guard_still_uses_monotonic_and_the_same_bound(waiting, monkeypatch):
    e = waiting
    e.exercise.patience = 1
    e.exercise.settlement_clock = None
    monkeypatch.setattr(drill.time, 'monotonic', lambda: e.clock[0])
    proof, reason = e.exercise.fresh_after_delivery(e.seat, e.order)
    assert proof is None
    assert 'outer settlement guard expired after 4.0s (bound 4.0s)' in reason
    assert e.clock[0] == 4 and e.events.closed == 1
    e.seat.fresh.assert_not_called()


def test_ordinary_drill_waits_retain_their_original_cadence(monkeypatch):
    clock, sleeps = [0.0], []
    results = iter([None, None, 'observed'])
    monkeypatch.setattr(drill.time, 'monotonic', lambda: clock[0])
    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds
    monkeypatch.setattr(drill.time, 'sleep', sleep)
    assert drill.wait_for(lambda: next(results), 1) == 'observed'
    assert sleeps == [.1, .1]


def test_actual_journal_directory_event_wakes_the_existing_notification_helper(tmp_path):
    folder = tmp_path / 'post/pending/worker'
    folder.mkdir(parents=True)
    notifications = service_wait.Arrivals(tmp_path)
    try:
        notifications.refresh((), extra=(folder,))
        (folder / 'ordinary-journal').write_text('Ordinary changed evidence')
        woke = notifications.wait(.01 if notifications.fd is None else .25)
        assert woke is (notifications.fd is not None)
    finally:
        notifications.close()
