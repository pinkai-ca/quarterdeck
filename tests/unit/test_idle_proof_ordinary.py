"""Ordinary synthetic idle evidence only; native/protective consumers have separate gates."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from quarterdeck import idle, move_hold, quota, recovery
from quarterdeck.harness.json_record import JsonLinesReader
from quarterdeck.harness.types import ActiveTool, State, Tokens, Turn
from quarterdeck.service import Service


AT = datetime(2032, 4, 5, 6, 7, tzinfo=timezone.utc)


def completed():
    turn = Turn(0, AT, AT + timedelta(seconds=5), 'synthetic input', 'synthetic reply',
                None, Tokens())
    return State(turn_open=False, pending_tools=(), last_input_at=AT, completed_turns=(turn,))


@pytest.mark.parametrize(('changes', 'dimension'), [
    ({}, None),
    ({'turn_open': None}, 'turn-unknown'),
    ({'turn_open': True}, 'turn-open'),
    ({'pending_tools': None}, 'tools-unknown'),
    ({'pending_tools': ('Bash',)}, 'tools-pending'),
    ({'queued': ('synthetic queued input',)}, 'queue-pending'),
    ({'queued': None}, 'queue-unknown'),
    ({'queue_error': 'synthetic queue ambiguity'}, 'queue-unknown'),
    ({'last_input_at': None}, 'completion'),
    ({'last_input_at': AT + timedelta(seconds=6)}, 'completion'),
    ({'last_input_at': AT - timedelta(seconds=1)}, 'completion'),
    ({'completed_turns': ()}, 'completion'),
])
def test_idle_dimensions(changes, dimension):
    hold = idle.check(replace(completed(), **changes))
    assert (hold.dimension if hold else None) == dimension


@pytest.mark.parametrize('changes', [
    {'timed': False}, {'started': None}, {'ended': None},
    {'started': AT + timedelta(seconds=8)},
])
def test_only_timed_ordered_completion_covers_latest_input(changes):
    state = completed()
    turn = replace(state.completed_turns[0], **changes)
    assert idle.check(replace(state, completed_turns=(turn,))).dimension == 'completion'


def service_case(tmp_path, monkeypatch, state):
    moved = []
    launch = SimpleNamespace(pane='%pane-fern', pid=101001, at=AT.isoformat(), harness='fake')
    seat = SimpleNamespace(
        name='seat-fern', folder=tmp_path / 'seats' / 'seat-fern', trusted=tmp_path / 'seats' / 'seat-fern',
        ship=SimpleNamespace(runtime=tmp_path), session=SimpleNamespace(human=False),
        spec=SimpleNamespace(pools=('pool-willow', 'pool-oak'), pool_choices=True),
        pool=SimpleNamespace(name='pool-willow'), is_up=True,
        launched=lambda: launch, record=lambda: tmp_path / 'record-fern.jsonl',
        pane=lambda: SimpleNamespace(dead=False, id=launch.pane, pid=launch.pid),
        harness=SimpleNamespace(state=lambda record: state, waiting_for_human=lambda reading: False),
        failover_to=lambda pool: moved.append(pool) or 'synthetic move proof',
        require_restart_target=lambda: None,
        restart=lambda: moved.append('restart') or 'synthetic restart proof',
        fresh=lambda: moved.append('fresh') or 'synthetic fresh proof',
    )
    service = Service.__new__(Service)
    service.ledger = object()
    service.store = SimpleNamespace(new=lambda *args: None)
    service.log = lambda *args, **kwargs: None
    service.resolved_seats = lambda: [seat]
    service.known_open = lambda target, pool: True
    monkeypatch.setattr(recovery, 'held', lambda target: None)
    monkeypatch.setattr(quota, 'current_walled', lambda *args: True)
    monkeypatch.setattr(quota, 'candidate_pools', lambda target: ['pool-oak'])
    return service, seat, moved


@pytest.mark.parametrize('path', ['failover', 'select', 'relaunch'])
@pytest.mark.parametrize('tools', [(), ('Bash',), None])
def test_all_live_paths_require_known_empty_tools(tmp_path, monkeypatch, path, tools):
    service, seat, moved = service_case(tmp_path, monkeypatch, replace(completed(), pending_tools=tools))
    if path == 'failover':
        service.failover()
    elif path == 'select':
        answer = service.select(seat, 'pool-oak')
        if tools != ():
            assert ('tool state unknown' if tools is None else 'tools pending: Bash') in answer
    else:
        answer = service.relaunch(seat, True)
        assert (answer is not None) == (tools == ())
    assert bool(moved) == (tools == ())
    reason = move_hold.reading(seat)
    assert (reason is None) == (tools == ())


@pytest.mark.parametrize('changes', [{'queued': ('synthetic queued input',)},
                                     {'queued': None}, {'queue_error': 'synthetic ambiguity'}])
def test_explicit_relaunch_requires_empty_known_queue_but_allows_error_alone(tmp_path, monkeypatch, changes):
    service, seat, moved = service_case(tmp_path, monkeypatch, replace(completed(), **changes))
    answer = service.relaunch(seat, False)
    if changes.get('queue_error'):
        assert answer == 'synthetic fresh proof' and moved == ['fresh']
        assert move_hold.reading(seat) is None
    else:
        assert answer is None and moved == []
        assert 'queue' in move_hold.reading(seat)


def test_first_proved_reading_releases_wall_move(tmp_path, monkeypatch):
    service, seat, moved = service_case(tmp_path, monkeypatch, replace(completed(), pending_tools=None))
    service.failover()
    assert moved == []
    seat.harness.state = lambda record: completed()
    service.failover()
    assert moved == ['pool-oak']
    assert move_hold.reading(seat) is None


def test_record_absence_and_unreadable_are_distinct(tmp_path, monkeypatch):
    service, seat, moved = service_case(tmp_path, monkeypatch, completed())
    seat.record = lambda: None
    assert service.not_idle(seat) == 'it has no joined record'

    def unreadable(record):
        raise OSError('synthetic unreadable record')

    seat.record = lambda: tmp_path / 'record-fern.jsonl'
    seat.harness.state = unreadable
    assert service.not_idle(seat) == 'its record is unreadable'
    assert moved == []


def test_changed_launch_during_read_cannot_supply_idle(tmp_path, monkeypatch):
    service, seat, moved = service_case(tmp_path, monkeypatch, completed())
    launch = seat.launched()

    def changed(record):
        launch.at = (AT + timedelta(seconds=1)).isoformat()
        return completed()

    seat.harness.state = changed
    assert service.relaunch(seat, True) is None
    assert moved == []


def test_call_detail_survives_turn_accounting_without_new_certainty(tmp_path):
    class OrdinaryReader(JsonLinesReader):
        kinds = ('synthetic',)

        def take(self, entry):
            pass

    record = tmp_path / 'record-fern.jsonl'
    record.write_text('')
    reader = OrdinaryReader(record)
    reader.start_turn({'tools': []})
    reader.open_call('call-willow', 'Bash', AT)
    reader.close_turn()
    state = reader.state()
    assert state.pending_tools == ('Bash',)
    assert state.active_tools == (ActiveTool('Bash', AT),)
    assert 'Bash since 2032-04-05T06:07:00+00:00' in idle.tool_reason(state)
    reader.tools_known = False
    reader.start_turn({'tools': []})
    reader.close_tool('call-willow', AT + timedelta(seconds=9), False)
    assert reader.state().active_tools == ()
    assert reader.state().pending_tools is None


@pytest.mark.parametrize(('reason', 'owned', 'allowed'), [
    ('no record yet', True, True),
    ('synthetic join unknown', True, False),
    ('no record yet', False, False),
])
def test_explicit_no_record_join_needs_owned_live_pane(tmp_path, monkeypatch, reason, owned, allowed):
    service, seat, moved = service_case(tmp_path, monkeypatch, completed())
    seat.record = lambda: None
    seat.join = lambda: SimpleNamespace(record=None, reason=reason)
    if not owned:
        seat.pane = lambda: SimpleNamespace(dead=False, id='%pane-other', pid=101004)
    proof = idle.reading(seat)
    assert proof.state is None
    assert (proof.hold is None) == allowed
    if allowed:
        assert proof.reason == 'no record yet: nothing has run'
        assert service.relaunch(seat, True) == 'synthetic restart proof'
    else:
        assert service.relaunch(seat, True) is None
    assert bool(moved) == allowed


QUEUED = {'queued': ('synthetic background notice the walled program never takes',)}


@pytest.mark.parametrize('path', ['failover', 'select'])
def test_a_walled_seat_whose_only_hold_is_its_queue_moves(tmp_path, monkeypatch, path):
    """At a wall the program never takes its queue: a seat held there by queued input alone moves."""
    service, seat, moved = service_case(tmp_path, monkeypatch, replace(completed(), **QUEUED))
    if path == 'failover':
        service.failover()
    else:
        assert 'moved to pool-oak' in service.select(seat, 'pool-oak')
    assert moved == ['pool-oak']


@pytest.mark.parametrize('changes', [{'pending_tools': ('Bash',)}, {'turn_open': True}, {'last_input_at': None}])
def test_a_walled_seat_with_queued_input_and_other_work_stays(tmp_path, monkeypatch, changes):
    service, seat, moved = service_case(tmp_path, monkeypatch, replace(completed(), **QUEUED, **changes))
    service.failover()
    assert 'not moved' in service.select(seat, 'pool-oak')
    assert moved == []


def test_queued_input_without_a_wall_still_holds_a_selection(tmp_path, monkeypatch):
    service, seat, moved = service_case(tmp_path, monkeypatch, replace(completed(), **QUEUED))
    monkeypatch.setattr(quota, 'current_walled', lambda *args: False)
    assert 'input is queued' in service.select(seat, 'pool-oak')
    assert moved == []


def test_a_person_s_walled_seat_with_queued_input_is_never_moved(tmp_path, monkeypatch):
    """In a person's session a queued input may be the person's own typing: no allowance there."""
    service, seat, moved = service_case(tmp_path, monkeypatch, replace(completed(), **QUEUED))
    seat.session = SimpleNamespace(human=True)
    service.failover()
    assert 'input is queued' in service.select(seat, 'pool-oak')
    assert moved == []
