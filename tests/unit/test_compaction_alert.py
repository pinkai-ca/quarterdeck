"""Ordinary synthetic notification cases; no provider, native program or lifecycle runs."""

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from quarterdeck import compaction, compaction_alert, post
from quarterdeck.harness.compactions import Compactions
from quarterdeck.harness.types import State
from quarterdeck.service import Service
from quarterdeck.watch import Facts


MANUAL = 'ready for manual fresh: execution unknown; choose an idle break'
RELEASED = 'new incoming work; prepare a new handoff and renew readiness'
SUBJECT = 'engineer-orchid'
SYSTEMS = 'systems-maple'
SECOND = 'systems-willow'


def service_for(env):
    service = Service.__new__(Service)
    service.ship, service.runtime, service.store = env.ship, env.ship.runtime, env.store
    service.seat = lambda name: env.seats[name]
    service.resolved_seats = lambda: [env.subject]
    service.log = lambda *args, **kwargs: pytest.fail('unexpected service error')
    return service


@pytest.fixture
def env(tmp_path, monkeypatch):
    ship = SimpleNamespace(runtime=tmp_path / 'runtime', seats={})
    seats = {}
    for name, role, up in ((SUBJECT, 'engineer', True), (SYSTEMS, 'systems', True),
                           (SECOND, 'systems', False), ('desk-fern', 'desk', True)):
        spec = SimpleNamespace(name=name, role=role, up=True, fresh_after_compactions=2, harness='codex')
        ship.seats[name] = spec
        seats[name] = SimpleNamespace(name=name, spec=spec, ship=ship, is_up=up,
                                      launched=lambda: None)
    store = post.Store(ship.runtime)
    request = post.Message('ord-synthetic-request-orchid', 'order', post.SHIP, SUBJECT,
                           datetime(2025, 1, 1, tzinfo=timezone.utc), 'Synthetic threshold request')
    store.write(request)
    record = tmp_path / 'record-orchid.jsonl'
    reading = Compactions(conversation='conversation-orchid', count=2, reason=None,
                          extent=18, digest='digest-orchid',
                          state=State(turn_open=False, pending_tools=None, queued=(), queue_error=None))
    data = compaction.new()
    data.update(generation='generation-orchid', count=2,
                source=dict(record=str(record), harness='codex', conversation='conversation-orchid'),
                source_launch=None, extent=18, digest='digest-orchid', manual=MANUAL,
                reason='threshold reached; waiting for open handoff/readiness',
                request=request.to_json(), protocol_requests=[request.id])
    compaction.save(ship.runtime, SUBJECT, data)
    env = SimpleNamespace(ship=ship, seats=seats, subject=seats[SUBJECT], store=store,
                          record=record, reading=reading, request=request)
    env.service = service_for(env)
    monkeypatch.setattr(compaction, 'current', lambda seat: (env.record, env.reading, None))
    monkeypatch.setattr(compaction, 'live_identity', lambda seat: True)
    return env


def change(env, *, state=None, count=None, **fields):
    data = compaction.load(env.ship.runtime, SUBJECT)
    if count is not None:
        env.reading = replace(env.reading, count=count)
        data['count'] = count
    if state is not None:
        env.reading = replace(env.reading, state=state)
    data.update(fields)
    compaction.save(env.ship.runtime, SUBJECT, data)


def handoff(env, suffix='lilac'):
    message = post.Message(f'ord-synthetic-handoff-{suffix}', 'order', SUBJECT, SUBJECT,
                           datetime(2025, 1, 2, tzinfo=timezone.utc),
                           'Synthetic private work text stays out of notification', re=env.request.id)
    env.store.write(message)
    return message


def reports(env):
    return [message for message in env.store.all() if message.kind == 'report']


def step(env):
    compaction_alert.step(env.service, env.subject)


def test_idle_threshold_handoff_reports_once_through_existing_pass(env, monkeypatch):
    message = handoff(env)
    ticks = []
    monkeypatch.setattr(compaction, 'tick', lambda seat: ticks.append(seat.name))
    for _ in range(3):
        env.service.compactions()
    assert ticks == [SUBJECT] * 3
    [report] = reports(env)
    assert (report.sender, report.to) == (post.SHIP, SYSTEMS)
    assert f'{SUBJECT}: idle in its record; compactions 2/2: {MANUAL}' in report.body
    assert message.id in report.body
    assert message.body not in report.body
    assert 'execution may remain unknown' in report.body
    assert 'outside controller must choose the idle break' in report.body


@pytest.mark.parametrize('reason', sorted(compaction_alert.RELEASED))
def test_released_readiness_idle_reports_once(env, reason):
    change(env, manual=None, reason=reason, ready=None, attestation=None,
           state=State(turn_open=False, pending_tools=(), queued=(), queue_error=None))
    step(env)
    step(env)
    [report] = reports(env)
    assert f'compactions 2/2: {reason}' in report.body
    assert 'none open' in report.body


def test_past_threshold_has_exact_safe_count(env):
    change(env, count=3)
    step(env)
    assert f'compactions 3/2: {MANUAL}' in reports(env)[0].body


def test_changing_handoff_inventory_does_not_rearm(env):
    step(env)
    original = reports(env)[0].to_json()
    message = handoff(env)
    step(env)
    assert [report.to_json() for report in reports(env)] == [original]
    assert message.id not in original
    assert 'none open' in original


def test_released_open_handoff_is_named_by_metadata(env):
    message = handoff(env)
    change(env, manual=None, reason=RELEASED, reserved=None, handoff_digest=None)
    step(env)
    assert message.id in reports(env)[0].body


def test_changed_reading_rearms(env):
    step(env)
    change(env, count=3)
    step(env)
    step(env)
    assert len(reports(env)) == 2


def test_changed_generation_rearms_same_displayed_reading(env):
    step(env)
    change(env, generation='generation-willow')
    step(env)
    assert len(reports(env)) == 2


def test_record_growth_alone_does_not_rearm(env):
    step(env)
    env.reading = replace(env.reading, extent=36, digest='digest-willow')
    change(env, extent=36, digest='digest-willow')
    step(env)
    assert len(reports(env)) == 1


@pytest.mark.parametrize('state', [
    State(turn_open=True, pending_tools=None, queued=()),
    State(turn_open=False, pending_tools=None, queued=('input-orchid',)),
    State(turn_open=False, open_tool='tool-orchid', pending_tools=None, queued=()),
    State(turn_open=False, pending_tools=('tool-orchid',), queued=()),
])
def test_busy_activity_preserves_standing_reading_without_rearming(env, state):
    step(env)
    original = compaction_alert.load(env.ship.runtime, SUBJECT)['key']
    change(env, state=state)
    step(env)
    assert compaction_alert.load(env.ship.runtime, SUBJECT)['key'] == original
    change(env, state=State(turn_open=False, pending_tools=None, queued=()))
    step(env)
    assert len(reports(env)) == 1
    change(env, state=state, manual=None, reason='handoff published; waiting for explicit readiness')
    step(env)
    assert compaction_alert.load(env.ship.runtime, SUBJECT)['key'] is None
    change(env, manual=MANUAL, state=State(turn_open=False, pending_tools=None, queued=()))
    step(env)
    assert len(reports(env)) == 2


def test_restart_and_unobserved_clear_do_not_rearm(env):
    step(env)
    original = reports(env)[0].to_json()
    # No service observation of the intervening turn: restart cannot infer it.
    change(env, state=State(turn_open=True, pending_tools=None, queued=()))
    change(env, state=State(turn_open=False, pending_tools=None, queued=()))
    env.service = service_for(env)
    step(env)
    assert [report.to_json() for report in reports(env)] == [original]


def test_newly_up_recipient_gets_one_and_returning_recipient_gets_none(env):
    step(env)
    first_body = reports(env)[0].body
    handoff(env)
    env.seats[SYSTEMS].is_up = False
    env.seats[SECOND].is_up = True
    step(env)
    env.seats[SYSTEMS].is_up = True
    step(env)
    step(env)
    assert sorted(report.to for report in reports(env)) == [SYSTEMS, SECOND]
    assert all(report.body == first_body for report in reports(env))


def test_all_up_systems_receive_report(env):
    env.seats[SECOND].is_up = True
    step(env)
    assert sorted(report.to for report in reports(env)) == [SYSTEMS, SECOND]


def test_multiple_saved_intents_have_distinct_names(env, monkeypatch):
    env.seats[SECOND].is_up = True
    prepare = env.store.prepare
    prepared = []

    def repeated_name(*args, **kwargs):
        message = prepare(*args, **kwargs)
        prepared.append(message)
        return replace(message, id=prepared[0].id) if len(prepared) == 2 else message

    monkeypatch.setattr(env.store, 'prepare', repeated_name)
    step(env)
    assert len(reports(env)) == 2
    assert len({report.id for report in reports(env)}) == 2


def test_no_systems_has_named_status_even_on_overflow(env):
    env.seats[SYSTEMS].is_up = False
    step(env)
    assert reports(env) == []
    [fallback] = compaction_alert.unrouted(env.ship, list(env.seats.values()))
    assert f'{SUBJECT}: compactions 2/2: {MANUAL}' in fallback
    assert 'no systems seat up' in fallback
    facts = Facts(service='ok', waiting=0, oldest=0, blind=0,
                  compactions=[f'{SUBJECT}: compactions 2/2: {MANUAL}'], manual_fresh=[fallback])
    assert SUBJECT in facts.line() and MANUAL in facts.line()
    crowded = replace(facts, pending=['synthetic pending detail ' * 20])
    from quarterdeck.headlines import details
    assert fallback in crowded.line() and crowded.line().endswith(' · qd status')
    assert fallback in details(crowded)
    env.seats[SYSTEMS].is_up = True
    step(env)
    assert len(reports(env)) == 1
    assert compaction_alert.unrouted(env.ship, list(env.seats.values())) == []


def test_subject_never_receives_own_report_or_desk_fallback(env):
    env.subject.spec.role = 'systems'
    env.seats[SYSTEMS].is_up = False
    step(env)
    assert reports(env) == []
    [fallback] = compaction_alert.unrouted(env.ship, list(env.seats.values()))
    assert SUBJECT in fallback


def test_systems_subject_notifies_other_systems_only(env):
    env.subject.spec.role = 'systems'
    step(env)
    assert [report.to for report in reports(env)] == [SYSTEMS]


def test_manual_human_owned_reading_keeps_controller_instruction(env):
    change(env, manual='ready for manual fresh: human-owned session; choose an idle break',
           state=State(turn_open=False, pending_tools=(), queued=()))
    step(env)
    [report] = reports(env)
    assert 'human-owned session' in report.body
    assert 'outside controller must choose the idle break' in report.body


@pytest.mark.parametrize('state', [
    State(turn_open=True, pending_tools=None, queued=()),
    State(turn_open=None, pending_tools=None, queued=()),
    State(turn_open=False, pending_tools=('tool-orchid',), queued=()),
    State(turn_open=False, open_tool='tool-orchid', pending_tools=None, queued=()),
    State(turn_open=False, pending_tools=None, queued=('input-orchid',)),
    State(turn_open=False, pending_tools=None, queued=(), queue_error='queue unknown'),
    State(turn_open=False, pending_tools=None, queued=None),
])
def test_nonidle_or_unknown_record_activity_does_not_notify(env, state):
    change(env, state=state)
    step(env)
    assert reports(env) == []


def test_unknown_turn_holds_standing_key_without_rearming(env):
    step(env)
    change(env, state=State(turn_open=None, pending_tools=None, queued=()))
    step(env)
    change(env, state=State(turn_open=False, pending_tools=None, queued=()))
    step(env)
    assert len(reports(env)) == 1


def test_record_unknown_holds_standing_key_without_rearming(env, monkeypatch):
    step(env)
    current = compaction.current

    def unavailable(seat):
        raise compaction.ShipError('synthetic record temporarily unavailable')

    monkeypatch.setattr(compaction, 'current', unavailable)
    with pytest.raises(compaction.ShipError, match='temporarily unavailable'):
        step(env)
    monkeypatch.setattr(compaction, 'current', current)
    step(env)
    assert len(reports(env)) == 1


@pytest.mark.parametrize('fields', [
    {'count': 1}, {'reading_unknown': True}, {'transition': {'handoff': 'ord-synthetic-handoff-lilac'}},
    {'manual': None, 'reason': 'handoff published; waiting for explicit readiness'},
    {'manual': None, 'reason': RELEASED, 'ready': {'revision': 'revision-orchid'}},
])
def test_nonactionable_compaction_reading_does_not_notify(env, fields):
    change(env, **fields)
    step(env)
    assert reports(env) == []


def test_disabled_threshold_clears_standing_alert(env):
    step(env)
    env.subject.spec.fresh_after_compactions = 0
    step(env)
    assert compaction_alert.load(env.ship.runtime, SUBJECT)['key'] is None
    assert compaction_alert.unrouted(env.ship, list(env.seats.values())) == []


def test_prepublication_restart_recovers_exact_saved_intent(env, monkeypatch):
    publish = compaction_alert.publish
    monkeypatch.setattr(compaction_alert, 'publish', lambda *args: (_ for _ in ()).throw(OSError('synthetic stop')))
    with pytest.raises(OSError, match='synthetic stop'):
        step(env)
    [intent] = compaction_alert.load(env.ship.runtime, SUBJECT)['reports']
    assert reports(env) == []
    env.service = service_for(env)
    monkeypatch.setattr(compaction_alert, 'publish', publish)
    step(env)
    step(env)
    assert [report.to_json() for report in reports(env)] == [intent]


def test_postpublication_restart_recovers_exact_saved_intent(env, monkeypatch):
    publish = compaction_alert.publish

    def interrupted(runtime, intent):
        publish(runtime, intent)
        raise OSError('synthetic stop after publication')

    monkeypatch.setattr(compaction_alert, 'publish', interrupted)
    with pytest.raises(OSError, match='after publication'):
        step(env)
    [intent] = compaction_alert.load(env.ship.runtime, SUBJECT)['reports']
    env.service = service_for(env)
    monkeypatch.setattr(compaction_alert, 'publish', publish)
    step(env)
    assert [report.to_json() for report in reports(env)] == [intent]


def test_a_pending_intent_is_not_published_from_the_file_once_its_reading_cleared(env, monkeypatch):
    """A generation change clears the reading. Its unpublished words then exist only in the seat's journal, which
    the seat's own jail can write, so they are never published from it."""
    publish = compaction_alert.publish
    monkeypatch.setattr(compaction_alert, 'publish', lambda *args: (_ for _ in ()).throw(OSError('synthetic stop')))
    with pytest.raises(OSError):
        step(env)
    [intent] = compaction_alert.load(env.ship.runtime, SUBJECT)['reports']
    change(env, generation='generation-willow', count=1)
    monkeypatch.setattr(compaction_alert, 'publish', publish)
    step(env)
    assert intent and reports(env) == []
    assert compaction_alert.load(env.ship.runtime, SUBJECT)['key'] is None


@pytest.mark.parametrize('journal, words', [
    ('compaction', 'compaction journal could not be read (JSONDecodeError)'),
    ('alert', 'manual fresh alert journal could not be read (JSONDecodeError)'),
])
def test_an_unreadable_journal_says_so_by_its_seat_and_kind_never_its_content(env, journal, words):
    env.seats[SYSTEMS].is_up = False
    target = (compaction.path(env.ship.runtime, SUBJECT) if journal == 'compaction'
              else compaction_alert.path(env.ship.runtime, SUBJECT))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"synthetic": "secret-looking words never shown"')  # not JSON: cut short
    lines = compaction_alert.unrouted(env.ship, list(env.seats.values()))
    assert lines == [f'{SUBJECT}: {words}']
    assert 'no systems seat up' not in lines[0], 'the cause is the journal, not the routing'
    assert 'secret-looking' not in lines[0]


def test_a_journal_not_in_its_form_says_so(env):
    env.seats[SYSTEMS].is_up = False
    compaction.path(env.ship.runtime, SUBJECT).write_text('{"version": 99, "generation": "x"}')
    assert compaction_alert.unrouted(env.ship, list(env.seats.values())) == [
        f'{SUBJECT}: compaction journal could not be read (not in its form)']


def test_a_readable_alert_with_nobody_to_route_it_keeps_its_line(env):
    env.seats[SYSTEMS].is_up = False
    step(env)
    [line] = compaction_alert.unrouted(env.ship, list(env.seats.values()))
    assert line == f'{SUBJECT}: compactions 2/2: {MANUAL}; no systems seat up'


def test_a_readable_journal_with_no_alert_gives_no_line(env):
    env.seats[SYSTEMS].is_up = False
    # The subject's journal is readable and no alert was raised for it; the other seats have none at all.
    assert compaction_alert.unrouted(env.ship, list(env.seats.values())) == []
