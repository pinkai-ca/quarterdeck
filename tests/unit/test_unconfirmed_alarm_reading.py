"""Ordinary synthetic notification transitions; no delivery or native record actions."""

from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

import pytest

from quarterdeck import alarms, auth_failure, compaction_alert, post, service, unconfirmed_alert, watch
from quarterdeck.door import Reading


OLD = unconfirmed_alert.PREFIX + '; synthetic old submission (worker)'
OTHER = unconfirmed_alert.PREFIX + '; synthetic other submission (worker)'
UNKNOWN = 'pending delivery journal unreadable; never resent (worker)'
MENDABLE = 'synthetic mendable obstruction'


@pytest.fixture
def alarm(tmp_path, monkeypatch):
    ship = SimpleNamespace(runtime=tmp_path / 'runtime', workspace=tmp_path,
                           seats={name: SimpleNamespace(name=name, role=role)
                                  for name, role in [('worker', 'worker'), ('desk', 'desk'),
                                                     ('systems', 'systems')]})
    seat = SimpleNamespace(name='worker', ship=ship, session=SimpleNamespace(human=False),
                           spec=SimpleNamespace(pools=('pool',)),
                           harness=SimpleNamespace(reads_queue_error=False), launched=lambda: None)
    state = SimpleNamespace(detached=[OLD], door=None, up={'systems'}, counter=0,
                            when=datetime(2030, 1, 1, tzinfo=timezone.utc))
    monkeypatch.setattr(alarms, 'now', lambda: state.when)
    monkeypatch.setattr(auth_failure, 'status', lambda seat: None)

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
    ship.session_of = lambda spec: seat.session
    state.actor = restart()
    return state


def notice(alarm):
    alarm.actor.needs_human(alarm.seat)
    marker = MENDABLE if getattr(alarm, 'mendable', False) else unconfirmed_alert.PREFIX
    return [message for message in alarm.actor.store.all() if marker in message.body and message.sender == post.SHIP]


def mendable(alarm):
    alarm.mendable = True
    alarm.door = MENDABLE
    alarm.detached = []


def reading_state(alarm):
    channel = 'human' if getattr(alarm, 'mendable', False) else 'detached'
    return alarms.load(alarm.ship.runtime, 'worker', channel)


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


def test_committed_initial_notifications_survive_clear_before_recovery(alarm, monkeypatch):
    publish = compaction_alert.publish
    monkeypatch.setattr(compaction_alert, 'publish', lambda *args: (_ for _ in ()).throw(
        OSError('synthetic publication interruption')))
    with pytest.raises(OSError):
        notice(alarm)
    monkeypatch.setattr(compaction_alert, 'publish', publish)
    alarm.actor = alarm.restart()
    alarm.detached = []
    assert {message.to for message in notice(alarm)} == {'systems', 'desk'}
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

def reply(alarm, sender='systems', kind='ack', parent=None):
    root = next(message for message in notice(alarm) if message.to == 'systems')
    return alarm.actor.store.new(kind, sender, post.SHIP, 'synthetic alarm reply', re=parent or root.id)


def test_untaken_alarm_escalates_at_ten_minutes_once_across_restart(alarm):
    mendable(alarm)
    assert [message.to for message in notice(alarm)] == ['systems']
    first_at = reading_state(alarm)['first_at']
    alarm.when += timedelta(seconds=599)
    assert len(notice(alarm)) == 1
    alarm.actor = alarm.restart()
    alarm.when += timedelta(seconds=1)
    assert {message.to for message in notice(alarm)} == {'systems', 'desk'}
    alarm.when += timedelta(minutes=30)
    assert len(notice(alarm)) == 2
    assert reading_state(alarm)['first_at'] == first_at


def test_systems_ack_takes_alarm_without_closing_a_human_order(alarm):
    mendable(alarm)
    order = alarm.actor.store.new('order', 'captain', 'worker', 'synthetic open work')
    notice(alarm)
    acknowledgment = reply(alarm)
    alarm.when += timedelta(minutes=10)
    assert [message.to for message in notice(alarm)] == ['systems']
    assert reading_state(alarm)['taken'] == acknowledgment.id
    alarm.actor = alarm.restart()
    alarm.when += timedelta(minutes=20)
    assert len(notice(alarm)) == 1
    assert alarm.actor.store.is_open(order)


def test_any_systems_descendant_in_alarm_thread_takes_it(alarm):
    mendable(alarm)
    middle = reply(alarm, sender='desk', kind='question')
    response = reply(alarm, kind='answer', parent=middle.id)
    alarm.when += timedelta(minutes=10)
    assert len(notice(alarm)) == 1
    assert reading_state(alarm)['taken'] == response.id


@pytest.mark.parametrize('unrelated', [False, True])
def test_other_posts_do_not_take_alarm(alarm, unrelated):
    mendable(alarm)
    notice(alarm)
    if unrelated:
        parent = alarm.actor.store.new('report', post.SHIP, 'systems', 'synthetic other alarm')
        reply(alarm, parent=parent.id)
    else:
        reply(alarm, sender='worker')
    alarm.when += timedelta(minutes=10)
    assert {message.to for message in notice(alarm)} == {'systems', 'desk'}
    assert reading_state(alarm)['taken'] is None


def test_clear_before_deadline_never_escalates_and_next_reading_gets_new_clock(alarm):
    mendable(alarm)
    notice(alarm)
    alarm.when += timedelta(seconds=599)
    alarm.door = None
    assert len(notice(alarm)) == 1
    alarm.when += timedelta(minutes=10)
    assert len(notice(alarm)) == 1
    alarm.actor = alarm.restart()
    alarm.door = MENDABLE
    assert len(notice(alarm)) == 2
    alarm.when += timedelta(seconds=599)
    assert len(notice(alarm)) == 2
    alarm.when += timedelta(seconds=1)
    assert len(notice(alarm)) == 3


def test_no_systems_routes_desk_immediately_even_after_taking(alarm):
    mendable(alarm)
    notice(alarm)
    reply(alarm)
    notice(alarm)
    alarm.up.clear()
    assert {message.to for message in notice(alarm)} == {'systems', 'desk'}
    alarm.actor = alarm.restart()
    assert len(notice(alarm)) == 2


def test_human_owned_alarm_goes_to_desk_immediately(alarm):
    alarm.seat.session.human = True
    assert {message.to for message in notice(alarm)} == {'systems', 'desk'}
    alarm.when += timedelta(minutes=30)
    alarm.actor = alarm.restart()
    assert len(notice(alarm)) == 2


def test_desk_can_receive_its_own_alarm_through_post(alarm):
    alarms.notify(alarm.actor, 'desk', True, 'synthetic desk hold', 'desk needs attention')
    reports = alarm.actor.store.all()
    assert {message.to for message in reports} == {'systems', 'desk'}


@pytest.mark.parametrize('cleared', [False, True])
def test_pending_escalation_is_not_published_after_taking_or_clear(alarm, monkeypatch, cleared):
    mendable(alarm)
    notice(alarm)
    alarm.when += timedelta(minutes=10)
    publish = compaction_alert.publish

    def interrupted(runtime, intent):
        if post.Message.from_json(intent).to == 'desk':
            raise OSError('synthetic escalation interruption')
        return publish(runtime, intent)

    monkeypatch.setattr(compaction_alert, 'publish', interrupted)
    with pytest.raises(OSError, match='synthetic escalation interruption'):
        notice(alarm)
    saved = reading_state(alarm)
    assert len(saved['reports']) == 2
    if cleared:
        alarm.door = None
    else:
        root = next(message for message in alarm.actor.store.all() if message.to == 'systems')
        alarm.actor.store.new('ack', 'systems', post.SHIP, 'synthetic taking', re=root.id)
    monkeypatch.setattr(compaction_alert, 'publish', publish)
    alarm.actor = alarm.restart()
    assert [message.to for message in notice(alarm)] == ['systems']


def test_unknown_reading_cannot_escalate_or_restart_its_clock(alarm):
    notice(alarm)
    first_at = reading_state(alarm)['first_at']
    alarm.detached = [UNKNOWN]
    alarm.when += timedelta(minutes=10)
    assert len(notice(alarm)) == 2
    alarm.actor = alarm.restart()
    alarm.detached = [OLD]
    assert {message.to for message in notice(alarm)} == {'systems', 'desk'}
    assert reading_state(alarm)['first_at'] == first_at


def test_manual_fresh_alarm_uses_same_taking_and_escalation(alarm, monkeypatch):
    key = dict(generation='generationg', source={}, reading='ready for manual fresh')
    monkeypatch.setattr(compaction_alert, 'observation', lambda seat: (key, dict(protocol_requests=[])))
    compaction_alert.step(alarm.actor, alarm.seat)
    assert [message.to for message in alarm.actor.store.all()] == ['systems']
    alarm.when += timedelta(minutes=10)
    alarm.actor = alarm.restart()
    compaction_alert.step(alarm.actor, alarm.seat)
    assert {message.to for message in alarm.actor.store.all()} == {'systems', 'desk'}
    assert compaction_alert.load(alarm.ship.runtime, 'worker')['key'] == key


def test_older_manual_alert_starts_escalation_clock_at_next_observation(alarm, monkeypatch):
    import json
    key = dict(generation='generationg', source={}, reading='ready for manual fresh')
    monkeypatch.setattr(compaction_alert, 'observation', lambda seat: (key, dict(protocol_requests=[])))
    compaction_alert.step(alarm.actor, alarm.seat)
    path = compaction_alert.path(alarm.ship.runtime, 'worker')
    old = json.loads(path.read_text())
    del old['first_at']
    del old['taken']
    path.write_text(json.dumps(old))
    alarm.when += timedelta(days=1)
    compaction_alert.step(alarm.actor, alarm.seat)
    assert [message.to for message in alarm.actor.store.all()] == ['systems']
    assert compaction_alert.load(alarm.ship.runtime, 'worker')['first_at'] == alarm.when.isoformat()
    alarm.when += timedelta(minutes=10)
    compaction_alert.step(alarm.actor, alarm.seat)
    assert {message.to for message in alarm.actor.store.all()} == {'systems', 'desk'}


def test_selection_alarm_keeps_same_clock_and_clears_on_resolution(alarm):
    from quarterdeck.seat import SelectionError
    alarm.ship.seat = lambda name: alarm.ship.seats[name]
    alarm.ship.session_of = lambda spec: alarm.seat.session
    alarm.actor.log = lambda *args, **kwargs: None
    error = SelectionError('worker', 'synthetic selection unavailable')
    alarm.actor.hold_selection(error)
    assert [message.to for message in alarm.actor.store.all()] == ['systems']
    alarm.when += timedelta(minutes=10)
    alarm.actor.hold_selection(error)
    assert {message.to for message in alarm.actor.store.all()} == {'systems', 'desk'}
    alarms.notify(alarm.actor, 'worker', False, None, None, channel='selection')
    assert alarms.load(alarm.ship.runtime, 'worker', 'selection')['key'] is None


def test_manual_alert_keeps_existing_injected_recipient_interface(alarm, monkeypatch):
    key = dict(generation='generationg', source={}, reading='ready for manual fresh')
    monkeypatch.setattr(compaction_alert, 'observation', lambda seat: (key, dict(protocol_requests=[])))
    owner = SimpleNamespace(store=alarm.actor.store, systems_recipients=lambda seat: ['systems'])
    compaction_alert.step(owner, alarm.seat)
    assert [message.to for message in owner.store.all()] == ['systems']
    alarm.when += timedelta(minutes=10)
    compaction_alert.step(owner, alarm.seat)
    assert {message.to for message in owner.store.all()} == {'systems', 'desk'}


def test_four_field_notification_sink_keeps_once_per_reading_on_restart(alarm):
    sent = []
    owner = SimpleNamespace(store=SimpleNamespace(new=lambda *args: sent.append(args)), ship=alarm.ship)
    for _ in range(2):
        alarms.notify(owner, 'worker', False, 'synthetic mendable reading', MENDABLE, up=['systems'])
    assert sent == [('report', post.SHIP, 'systems', MENDABLE)]
    owner = SimpleNamespace(store=owner.store, ship=alarm.ship)
    alarm.when += timedelta(minutes=10)
    for _ in range(2):
        alarms.notify(owner, 'worker', False, 'synthetic mendable reading', MENDABLE, up=['systems'])
    assert sent == [('report', post.SHIP, 'systems', MENDABLE), ('report', post.SHIP, 'desk', MENDABLE)]


def test_needs_human_uses_only_existing_alarm_recipient_callback(alarm):
    mendable(alarm)
    asked = []

    def recipients(seat, reason):
        asked.append((seat.name, reason))
        return ['systems']

    owner = SimpleNamespace(ship=alarm.ship, store=alarm.actor.store, told={}, alarm_recipients=recipients)
    service.Service.needs_human(owner, alarm.seat)
    assert [message.to for message in owner.store.all()] == ['systems']
    alarm.when += timedelta(minutes=10)
    service.Service.needs_human(owner, alarm.seat)
    assert {message.to for message in owner.store.all()} == {'systems', 'desk'}
    assert asked == [('worker', MENDABLE)] * 2
