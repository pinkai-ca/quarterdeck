"""Ordinary synthetic notification transitions; no delivery or native record actions."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from quarterdeck import compaction_alert, post, service, unconfirmed_alert, watch
from quarterdeck.door import Reading


OLD = unconfirmed_alert.PREFIX + '; synthetic old submission (worker)'
OTHER = unconfirmed_alert.PREFIX + '; synthetic other submission (worker)'
UNKNOWN = 'pending delivery journal unreadable; never resent (worker)'


@pytest.fixture
def alarm(tmp_path, monkeypatch):
    ship = SimpleNamespace(runtime=tmp_path / 'runtime', workspace=tmp_path,
                           seats={name: SimpleNamespace(name=name, role=role)
                                  for name, role in [('worker', 'worker'), ('desk', 'desk'),
                                                     ('systems', 'systems')]})
    seat = SimpleNamespace(name='worker', ship=ship, session=SimpleNamespace(human=False),
                           spec=SimpleNamespace(pools=('pool',)),
                           harness=SimpleNamespace(reads_queue_error=False), launched=lambda: None)
    state = SimpleNamespace(detached=[OLD], door=None, up={'systems'}, counter=0)

    def prepare(store, kind, sender, to, body, **fields):
        state.counter += 1
        return post.Message(f'rep-synthetic-noticeg{state.counter}', kind, sender, to,
                            datetime(2030, 1, 1, tzinfo=timezone.utc), body, **fields)

    monkeypatch.setattr(post.Store, 'prepare', prepare)
    monkeypatch.setattr(service.pending_delivery, 'detached', lambda seat: state.detached)
    monkeypatch.setattr(service, 'Door', lambda seat: SimpleNamespace(
        human_obstruction=lambda: (Reading(True), state.door)))

    def restart():
        actor = service.Service.__new__(service.Service)
        actor.runtime, actor.ship = ship.runtime, ship
        actor.store = post.Store(ship.runtime)
        actor.told, actor.alarmed = {}, {}
        actor._up = lambda name: name in state.up
        return actor

    state.seat, state.ship, state.restart = seat, ship, restart
    state.actor = restart()
    return state


def notice(alarm):
    alarm.actor.needs_human(alarm.seat)
    return [message for message in alarm.actor.store.all() if unconfirmed_alert.PREFIX in message.body]


def test_repeated_passes_restart_and_door_changes_keep_one_reading(alarm):
    first = notice(alarm)
    assert {message.to for message in first} == {'systems', 'desk'}
    path = unconfirmed_alert.path(alarm.ship.runtime, 'worker')
    before = path.stat()
    original = path.read_bytes()
    for obstruction in ['text in the box', None, 'login', None]:
        alarm.door = obstruction
        assert notice(alarm) == first
    alarm.actor = alarm.restart()
    assert notice(alarm) == first
    assert path.read_bytes() == original
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    assert all(message.re is None and message.sender == post.SHIP for message in first)


def test_new_recipient_gets_one_without_resending_to_returning_recipient(alarm):
    alarm.up.clear()
    assert [message.to for message in notice(alarm)] == ['desk']
    alarm.up.add('systems')
    first = notice(alarm)
    assert {message.to for message in first} == {'desk', 'systems'}
    alarm.up.clear()
    notice(alarm)
    alarm.actor = alarm.restart()
    alarm.up.add('systems')
    assert notice(alarm) == first


def test_changed_then_cleared_reading_rearms_even_after_restart(alarm):
    assert len(notice(alarm)) == 2
    alarm.detached = [OTHER]
    assert len(notice(alarm)) == 4
    alarm.actor = alarm.restart()
    assert len(notice(alarm)) == 4
    alarm.detached = []
    notice(alarm)
    alarm.actor = alarm.restart()
    alarm.detached = [OTHER]
    assert len(notice(alarm)) == 6


def test_unknown_evidence_does_not_clear_or_rearm(alarm):
    first = notice(alarm)
    alarm.detached = [UNKNOWN]
    notice(alarm)
    alarm.actor = alarm.restart()
    alarm.detached = [OLD]
    assert notice(alarm) == first


def test_masked_detached_reading_is_not_observed_or_cleared(alarm, monkeypatch):
    first = notice(alarm)
    alarm.door = 'login'
    monkeypatch.setattr(service.pending_delivery, 'detached', lambda seat: pytest.fail(
        'a door obstruction must retain the existing short-circuit'))
    assert notice(alarm) == first
    alarm.actor = alarm.restart()
    assert notice(alarm) == first
    monkeypatch.setattr(service.pending_delivery, 'detached', lambda seat: [OLD])
    alarm.door = None
    assert notice(alarm) == first


def test_initial_door_obstruction_keeps_existing_alarm_ownership(alarm, monkeypatch):
    alarm.door = 'login'
    monkeypatch.setattr(service.pending_delivery, 'detached', lambda seat: pytest.fail(
        'the existing door alarm must not read detached journals'))
    assert notice(alarm) == []
    assert {message.to for message in alarm.actor.store.all()} == {'systems', 'desk'}
    assert not unconfirmed_alert.path(alarm.ship.runtime, 'worker').exists()


def test_reordering_and_duplicates_are_the_same_operational_reading(alarm):
    alarm.detached = [OLD, OTHER]
    first = notice(alarm)
    assert all(OLD in message.body and OTHER in message.body for message in first)
    alarm.actor = alarm.restart()
    alarm.detached = [OTHER, OLD, OTHER]
    assert notice(alarm) == first


@pytest.mark.parametrize('published', [False, True])
def test_fixed_intents_recover_publication_interruption(alarm, monkeypatch, published):
    publish = compaction_alert.publish

    def interrupted(runtime, intent):
        if published:
            publish(runtime, intent)
        raise OSError('synthetic publication interruption')

    monkeypatch.setattr(compaction_alert, 'publish', interrupted)
    with pytest.raises(OSError, match='synthetic publication interruption'):
        notice(alarm)
    saved = unconfirmed_alert.load(alarm.ship.runtime, 'worker')
    assert len(saved['reports']) == 2
    fixed = sorted(post.Message.from_json(intent).id for intent in saved['reports'])
    monkeypatch.setattr(compaction_alert, 'publish', publish)
    alarm.actor = alarm.restart()
    recovered = notice(alarm)
    assert sorted(message.id for message in recovered) == fixed
    assert notice(alarm) == recovered


def test_committed_notifications_survive_clear_before_recovery(alarm, monkeypatch):
    publish = compaction_alert.publish
    monkeypatch.setattr(compaction_alert, 'publish', lambda *args: (_ for _ in ()).throw(
        OSError('synthetic publication interruption')))
    with pytest.raises(OSError):
        notice(alarm)
    monkeypatch.setattr(compaction_alert, 'publish', publish)
    alarm.actor = alarm.restart()
    alarm.detached = []
    assert len(notice(alarm)) == 2
    assert unconfirmed_alert.load(alarm.ship.runtime, 'worker')['key'] is None


def test_watch_keeps_standing_hold_after_notification_and_restart(alarm, monkeypatch):
    first = notice(alarm)
    alarm.actor = alarm.restart()
    assert notice(alarm) == first
    from quarterdeck import compaction, structure_outcome, turn_failure

    monkeypatch.setattr(watch, 'seats', lambda *args: [alarm.seat])
    monkeypatch.setattr(watch, 'limit_state', lambda *args: ([], [], []))
    monkeypatch.setattr(watch, 'Door', lambda seat: SimpleNamespace(
        human_obstruction=lambda: (Reading(True), None)))
    monkeypatch.setattr(turn_failure, 'obstruction', lambda *args: None)
    monkeypatch.setattr(compaction, 'status', lambda *args, **kwargs: None)
    monkeypatch.setattr(compaction_alert, 'unrouted', lambda *args: [])
    monkeypatch.setattr(structure_outcome, 'waits', lambda *args: [])
    monkeypatch.setattr(watch, 'ledger_walls', lambda *args: [])
    monkeypatch.setattr(watch, 'headline_readings', lambda *args: [])
    monkeypatch.setattr(watch, 'disks', lambda *args: [])
    facts = watch.facts(alarm.ship, None, login=[])
    assert 'worker' in facts.needs_human
    assert any(OLD in reason for reason in facts.pending)
    assert 'worker' in facts.line()
    assert unconfirmed_alert.PREFIX in facts.line()
    assert notice(alarm) == first
