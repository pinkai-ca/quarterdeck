"""Restart through the real CLI and service with made-up records and simulated panes."""

from dataclasses import replace
from datetime import timedelta
import json
from uuid import uuid4

import pytest

from quarterdeck import cli, fresh_handoff, post, quota, relaunch_intent, shutdown
from quarterdeck.harness import get, record_file, resume_target
from quarterdeck.harness.limits import Usage, Window
from quarterdeck.harness.synthetic_record import CodexShape
from quarterdeck.harness.types import Screen

from .test_cross_program_failover import running, usage  # noqa: F401


def cross_read_bound(path, reader, monkeypatch):
    """Set the reader limit at the current fixture size, then cross it by one byte."""
    bound_name = 'CLAUDE_STREAM_BOUND' if reader == 'claude' else 'LINES_BOUND'
    bound = path.stat().st_size
    monkeypatch.setattr(record_file, bound_name, bound)
    with path.open('r+b') as stream:
        stream.truncate(bound + 1)


@pytest.mark.parametrize('running', [False, True], indirect=True, ids=['crew', 'human'])
@pytest.mark.parametrize('case', ['unreadable', 'missing', 'no-id', 'oversized-no-id'])
@pytest.mark.parametrize('entry', ['cli', 'service'])
def test_unproved_restart_keeps_the_old_process_until_the_correct_next_step(running, monkeypatch, case, entry, capsys):
    r = running
    seat = r.seat
    usage(r.svc, 'first', 0.2)
    before = seat.launched()
    launches = list(r.launches)
    record = r.source.path
    if case == 'missing':
        record.unlink()
        sibling = CodexShape(seat.home, seat.ship.folder, 'synthetic-other', 'synthetic-source')
        sibling.begin('Made-up text from another record', 'synthetic-source', 'high')
        sibling.end('Made-up output')
    elif case == 'unreadable':
        record.write_bytes(b'\xff\xfe\n')
    else:
        record.write_text(json.dumps({'type': 'session_meta', 'payload': {}}) + '\n')
        if case == 'oversized-no-id':
            cross_read_bound(record, seat.harness.name, monkeypatch)

    if entry == 'cli':
        monkeypatch.setattr(cli, 'seat_of', lambda *_: seat)
        monkeypatch.setattr(seat.harness, 'screen', lambda _: Screen('', '', working=False))
        code = cli.main(['--ship', str(seat.ship.folder), 'restart', seat.name])
        output = capsys.readouterr()
        assert code == (2 if seat.session.human else 0)
        message = output.err if seat.session.human else output.out
    elif seat.session.human:
        with pytest.raises(resume_target.Unproved) as refusal:
            r.svc.relaunch_request(dict(seat=seat.name, resume=True))
        message = str(refusal.value)
    else:
        message = r.svc.relaunch_request(dict(seat=seat.name, resume=True))
    if seat.session.human:
        assert f'Use qd fresh {seat.name} to start a new conversation carrying its handoff' in message
    else:
        assert 'handoff order' in message
        assert fresh_handoff.active(seat.ship.runtime, seat.name)['phase'] == 'waiting'
    assert seat.is_up and seat.launched() == before and r.launches == launches
    assert (seat.trusted / 'record').read_text().strip() == str(record)


@pytest.mark.parametrize('running', [False, True], indirect=True, ids=['crew', 'human'])
@pytest.mark.parametrize('program', ['codex', 'claude'])
def test_oversized_owned_record_supplies_resume_to_the_native_launch(running, monkeypatch, program, capsys):
    r = running
    seat = r.seat
    if program == 'claude':
        r.svc.failover()
        active = r.created[-1]
    else:
        usage(r.svc, 'first', 0.2)
        active = r.source
    owned = seat.record()
    expected = seat.harness.session_id(owned)
    assert owned == active.path and expected
    cross_read_bound(owned, program, monkeypatch)
    with pytest.raises(record_file.PastBound):
        seat.harness.session_id(owned)

    # The normal launch must carry its proved ID. A second identity read after retirement would fail.
    identify = resume_target.identify
    calls = []
    def before_retirement(harness, record):
        assert seat.is_up
        calls.append(record)
        return identify(harness, record)

    monkeypatch.setattr(resume_target, 'identify', before_retirement)
    monkeypatch.setattr(cli, 'seat_of', lambda *_: seat)
    monkeypatch.setattr(seat.harness, 'screen', lambda _: Screen('', '', working=False))
    assert cli.main(['--ship', str(seat.ship.folder), 'restart', seat.name]) == 0, capsys.readouterr()
    assert calls == [owned]
    arg = 'resume' if program == 'codex' else '--resume'
    assert r.launches[-1][r.launches[-1].index(arg) + 1] == expected
    assert seat.launched().resume == expected and seat.record() == owned
    with pytest.raises(record_file.PastBound):
        seat.harness.state(owned)


def test_same_program_service_relaunch_and_selection_carry_the_same_owned_session(running):
    r = running
    r.svc.failover()
    seat = r.seat
    record = seat.record()
    expected = seat.harness.session_id(record)
    assert 'resumed' in r.svc.relaunch(seat, True)
    assert 'resumed' in r.svc.select(seat, 'third')
    assert seat.launched().resume == expected and seat.record() == record
    assert all(argv[argv.index('--resume') + 1] == expected for argv in r.launches[-2:])
    assert any(m.body == 'moved to pool third from second by qd set'
               for m in r.svc.store.for_seat(seat.name))
    assert (seat.trusted / 'record').read_text().strip() == str(record)


def on_the_open_program(r):
    """Move onto the second pool, then wall it so the next move stays on the same program."""
    r.svc.failover()
    assert r.seat.pool.name == 'second' and r.seat.harness.name == 'claude'
    usage(r.svc, 'second', 1.0)


def damage_record(seat, case):
    record = seat.record()
    if case == 'missing':
        record.unlink()
    elif case == 'unreadable':
        record.write_bytes(b'\xff\xfe\n')
    else:
        record.write_text(json.dumps({'type': 'user'}) + '\n')
        with record.open('r+b') as stream:
            stream.truncate(record_file.bound_of(record, seat.harness.name) + 1)
    return record


@pytest.mark.parametrize('case', ['unreadable', 'oversized', 'missing'])
def test_walled_crew_unproved_record_moves_fresh_to_the_open_pool_once(running, case, monkeypatch):
    from dataclasses import replace
    r = running
    on_the_open_program(r)
    before = len(r.launches)
    message = r.svc.store.new('order', 'issuer', 'worker', 'synthetic open order')
    r.seat.ship.seats['sentry'] = replace(r.seat.ship.seat('worker'), name='sentry', role='systems', persona=None)
    if case == 'oversized':
        # Pin the joined record first. The streamed reader then takes this constant through bound_of.
        joined = r.seat.record()
        monkeypatch.setattr(record_file, 'CLAUDE_STREAM_BOUND', 64)
        assert joined == r.seat.record()
    record = damage_record(r.seat, case)
    if case == 'oversized':
        assert record.stat().st_size > record_file.bound_of(record, 'claude') == 64
        with pytest.raises(record_file.PastBound):
            r.seat.harness.session_id(record)
    r.svc.failover()
    assert r.seat.pool.name == 'third' and r.seat.launched().resume is None
    assert len(r.launches) == before + 1 and '--resume' not in r.launches[-1]
    assert message.id in r.launches[-1][-1]
    assert any('started a new conversation' in body for body in (m.body for m in r.svc.store.for_seat('worker')))
    told = [m.body for m in r.svc.store.for_seat('sentry')]
    assert any('worker started a new conversation' in body for body in told)
    launched = r.seat.launched()
    r.svc.failover()
    assert r.seat.launched() == launched and len(r.launches) == before + 1
    assert fresh_handoff.active(r.seat.ship.runtime, r.seat.name) is None


def test_crew_select_of_an_unproved_record_refuses_a_walled_pool(running):
    r = running
    on_the_open_program(r)
    damage_record(r.seat, 'missing')
    usage(r.svc, 'third', 1.0)
    before = r.seat.launched()
    answer = r.svc.select(r.seat, 'third')
    assert answer == 'worker not moved: third is walled'
    assert r.seat.launched() == before and r.seat.pool.name == 'second'
    assert fresh_handoff.active(r.seat.ship.runtime, r.seat.name) is None


def test_crew_select_of_an_unproved_record_starts_fresh_on_the_chosen_pool_once(running):
    r = running
    on_the_open_program(r)
    damage_record(r.seat, 'missing')
    before = len(r.launches)
    answer = r.svc.select(r.seat, 'third')
    assert 'moved to third from second' in answer and 'started a new conversation' in answer
    assert r.seat.pool.name == 'third' and r.seat.launched().resume is None
    assert len(r.launches) == before + 1 and '--resume' not in r.launches[-1]
    again = r.svc.select(r.seat, 'third')
    assert 'already on third' in again and len(r.launches) == before + 1


@pytest.mark.parametrize('running', [True], indirect=True, ids=['human'])
def test_human_unproved_record_stays_held_and_named(running):
    from quarterdeck import watch
    r = running
    on_the_open_program(r)
    before = r.seat.launched()
    damage_record(r.seat, 'missing')
    r.svc.failover()
    assert r.seat.is_up and r.seat.launched() == before and r.seat.pool.name == 'second' and len(r.launches) == 1
    assert fresh_handoff.active(r.seat.ship.runtime, r.seat.name) is None
    named = watch.facts(r.seat.ship, r.seat.tmux, login=[]).move_holds
    assert any(r.seat.name in line for line in named), named
    with pytest.raises(resume_target.Unproved, match='worker'):
        r.svc.select(r.seat, 'third')
    assert r.seat.launched() == before and not any('moved to pool third' in m.body for m in r.svc.store.for_seat('worker'))


def qd(seat, action):
    return cli.main(['--ship', str(seat.ship.folder), action, seat.name])


@pytest.mark.parametrize('running', [True], indirect=True, ids=['human'])
@pytest.mark.parametrize('up', [True, False], ids=['live', 'down'])
def test_human_refusal_requires_explicit_fresh_and_carries_open_handoff(running, monkeypatch, capsys, up):
    r = running
    seat = r.seat
    usage(r.svc, 'first', 0.2)
    handoff = r.svc.store.new('order', seat.name, seat.name, 'Made-up complete handoff')
    r.source.path.unlink()
    if not up:
        seat.down()
    before = seat.launched()
    monkeypatch.setattr(cli, 'seat_of', lambda *_: seat)
    monkeypatch.setattr(seat.harness, 'screen', lambda _: Screen('', '', working=False))
    assert qd(seat, 'restart') == 2
    assert 'start a new conversation carrying its handoff' in capsys.readouterr().err
    assert seat.launched() == before and seat.is_up is up and not r.launches
    assert fresh_handoff.active(seat.ship.runtime, seat.name) is None
    assert qd(seat, 'fresh') == 0
    assert 'started a new conversation' in capsys.readouterr().out
    assert handoff.body in r.launches[-1][-1] and seat.launched().resume is None


def report_handoff(r, order):
    s = r.seat
    r.svc.attempts.close(r.svc.attempts.open(order, s.name, s.launched().pane, s.launched().pid, s.launched().at),
                         'delivered')
    r.source.begin('Made-up handoff preparation', s.spec.model, 'high')
    handoff = r.svc.store.new('order', s.name, s.name, 'Made-up notes to carry in full', re=order.id)
    r.svc.store.new('report', s.name, post.SHIP, f'Handoff: {handoff.id}', re=order.id)
    return handoff


def handoff_orders(r):
    return [m for m in r.svc.store.for_seat(r.seat.name) if m.subject == fresh_handoff.SUBJECT]


def test_live_crew_handoff_is_durable_bound_and_waits_for_report_and_turn_end(running, monkeypatch):
    r = running
    s = r.seat
    usage(r.svc, 'first', 0.2)
    monkeypatch.setattr(s.harness, 'screen', lambda _: Screen('', '', working=False))
    before = s.launched()
    text = r.source.path.read_text()
    first, _, rest = text.partition('\n')
    row = json.loads(first)
    row['payload'].pop('id')
    r.source.path.write_text(json.dumps(row) + '\n' + rest)

    assert 'handoff order' in r.svc.relaunch(s, True, caller='operator')
    result = r.svc.request_fresh_handoff(s, reason='the record is nearing its limit', expected_launch=before)
    assert result.disposition == 'already-staged'
    [order] = handoff_orders(r)
    unrelated = r.svc.store.new('order', s.name, s.name, 'Made-up unrelated handoff')
    r.svc.store.new('report', s.name, post.SHIP, f'Handoff: {unrelated.id}', re=order.id)
    r.svc.fresh_handoffs()
    assert s.launched() == before
    handoff = report_handoff(r, order)
    assert fresh_handoff.blocks_delivery(r.svc.runtime, handoff)
    r.svc.fresh_handoffs()
    assert s.launched() == before
    r.source.end('Made-up final response')
    r.svc.attempts.close(r.svc.attempts.open(unrelated, s.name, before.pane, before.pid, before.at), 'delivered')
    r.svc.fresh_handoffs()
    r.svc.fresh_handoffs()
    assert len(r.launches) == 1 and s.launched().resume is None
    assert handoff.body in r.launches[-1][-1] and handoff_orders(r) == [order]
    [report] = r.svc.store.for_seat('operator')
    assert 'started a new conversation because the existing conversation could not be identified' in report.body


def test_forecast_cannot_reopen_inactive_launch_but_actual_restart_can(running, monkeypatch):
    r = running
    s = r.seat
    usage(r.svc, 'first', 0.2)
    before = s.launched()
    r.source.path.unlink()
    r.live['pane'] = None
    monkeypatch.setattr('quarterdeck.server_recovery.pid_dead', lambda pid: True)
    result = r.svc.request_fresh_handoff(s, reason='the record is nearing its limit', expected_launch=before)
    assert result.disposition == 'held' and not r.launches
    proof = r.svc.relaunch(s, True, caller='operator')
    assert 'started a new conversation' in proof and 'old program could no longer run' in proof
    assert s.is_up and len(r.launches) == 1 and not handoff_orders(r)
    assert r.svc.request_fresh_handoff(s, reason='the record is nearing its limit',
                                       expected_launch=before).disposition == 'changed-launch'
    r.svc.fresh_handoffs()
    assert len(r.launches) == 1
    assert 'started a new conversation' in r.svc.store.for_seat('operator')[0].body


def test_later_launch_supersedes_a_staged_handoff(running):
    r = running
    s = r.seat
    result = r.svc.request_fresh_handoff(s, reason='the record is nearing its limit', expected_launch=s.launched())
    assert result.disposition == 'staged'
    [order] = handoff_orders(r)
    report_handoff(r, order)
    s.fresh()
    r.svc.fresh_handoffs()
    assert len(r.launches) == 1
    assert fresh_handoff.load(r.svc.runtime, s.name)['phase'] == 'superseded'


def conceal_session(record):
    """Keep a finished Claude record readable while its session id cannot be resumed."""
    lines = []
    for line in record.path.read_text().splitlines():
        if not line:
            continue
        row = json.loads(line)
        if 'sessionId' in row:
            row['sessionId'] = '-synthetic'
        lines.append(json.dumps(row))
    record.path.write_text('\n'.join(lines) + '\n')
    record.session = '-synthetic'


def report_shape(r, order, record):
    s = r.seat
    launched = s.launched()
    r.svc.attempts.close(r.svc.attempts.open(order, s.name, launched.pane, launched.pid, launched.at), 'delivered')
    record.begin('Made-up handoff preparation', s.spec.model, 'high')
    handoff = r.svc.store.new('order', s.name, s.name, 'Made-up notes to carry in full', re=order.id)
    r.svc.store.new('report', s.name, post.SHIP, f'Handoff: {handoff.id}', re=order.id)
    record.end('Made-up final response')
    return handoff


def _staged_before_wall(r):
    on_the_open_program(r)
    result = r.svc.request_fresh_handoff(r.seat, reason='the record is nearing its limit',
                                        expected_launch=r.seat.launched())
    assert result.disposition == 'staged'
    [order] = handoff_orders(r)
    other = r.svc.store.new('order', 'issuer', 'worker', 'synthetic other open order')
    notes = r.svc.store.new('order', r.seat.name, r.seat.name, 'Made-up notes that stay open', re=order.id)
    damage_record(r.seat, 'missing')
    return order, other, notes


def test_held_fresh_move_names_the_seat_and_keeps_the_staged_handoff(running, monkeypatch):
    from quarterdeck import watch
    r = running
    order, other, notes = _staged_before_wall(r)
    before = r.seat.launched()

    def held(*_args, **_kwargs):
        return fresh_handoff.FreshHandoffResult('held', 'worker: synthetic hold before a start')

    monkeypatch.setattr(fresh_handoff, 'request', held)
    launches = len(r.launches)
    r.svc.failover()
    assert r.seat.pool.name == 'second' and r.seat.launched() == before and len(r.launches) == launches
    assert r.svc.store.is_open(order) and r.svc.store.is_open(other) and r.svc.store.is_open(notes)
    assert fresh_handoff.load(r.svc.runtime, r.seat.name)['phase'] == 'waiting'
    named = watch.facts(r.seat.ship, r.seat.tmux, login=[]).move_holds
    assert any('move held for third: its conversation could not be identified' in line for line in named), named


def test_failed_fresh_move_names_the_outcome_and_keeps_open_orders(running, monkeypatch):
    from quarterdeck import watch
    from quarterdeck.ship import ShipError
    r = running
    order, other, notes = _staged_before_wall(r)
    before = r.seat.launched()

    def refuse(_pool):
        raise ShipError('synthetic start refused before a move')

    monkeypatch.setattr(r.seat, '_fresh_on_pool', refuse)
    launches = len(r.launches)
    r.svc.failover()
    assert r.seat.pool.name == 'second' and r.seat.launched() == before and len(r.launches) == launches
    assert fresh_handoff.load(r.svc.runtime, r.seat.name)['phase'] == 'failed'
    assert r.svc.store.is_open(order) and r.svc.store.is_open(other) and r.svc.store.is_open(notes)
    named = watch.facts(r.seat.ship, r.seat.tmux, login=[]).move_holds
    assert any('third was chosen: move outcome unknown' in line for line in named), named


def test_immediate_fresh_settles_a_carried_handoff_after_the_replacement_starts(running):
    r = running
    order, other, notes = _staged_before_wall(r)
    before = len(r.launches)
    r.svc.failover()
    assert r.seat.pool.name == 'third' and r.seat.launched().resume is None
    assert len(r.launches) == before + 1 and '--resume' not in r.launches[-1]
    assert order.id in r.launches[-1][-1] and other.id in r.launches[-1][-1]
    assert not r.svc.store.is_open(order)
    assert r.svc.store.is_open(other) and r.svc.store.is_open(notes)
    assert fresh_handoff.load(r.svc.runtime, r.seat.name)['phase'] == 'done'
    closed = [m for m in r.svc.store.replies(order) if m.sender == r.seat.name and m.kind == 'report']
    assert closed and all(fresh_handoff.REPORT.fullmatch(m.body.strip()) is None for m in closed)


def test_open_pool_select_of_a_missing_record_stages_the_chosen_pool(running):
    r = running
    r.svc.failover()
    assert r.seat.pool.name == 'second' and not quota.current_walled(r.seat, r.svc.ledger)
    damage_record(r.seat, 'missing')
    before, launches = r.seat.launched(), len(r.launches)
    answer = r.svc.select(r.seat, 'third')
    assert 'handoff order' in answer and 'moved to third' not in answer
    assert r.seat.launched() == before and r.seat.pool.name == 'second' and len(r.launches) == launches
    data = fresh_handoff.load(r.svc.runtime, r.seat.name)
    assert data['phase'] == 'waiting' and data['pool'] == 'third'
    [order] = handoff_orders(r)
    assert r.svc.store.is_open(order)
    again = r.svc.select(r.seat, 'third')
    assert 'handoff order' in again and len(r.launches) == launches and r.seat.launched() == before


@pytest.mark.parametrize('later_wall', [False, True], ids=['open', 'wall-then-open'])
def test_open_pool_select_rechecks_the_chosen_pool_after_report_and_idle(running, monkeypatch, later_wall):
    from quarterdeck import idle, watch
    r = running
    s = r.seat
    r.svc.failover()
    assert s.pool.name == 'second' and not quota.current_walled(s, r.svc.ledger)
    record = r.created[-1]
    conceal_session(record)
    assert resume_target.identify(s.harness, record.path) is None
    assert idle.reading(s).hold is None
    before = len(r.launches)
    launched = s.launched()
    answer = r.svc.select(s, 'third')
    assert 'handoff order' in answer and 'moved to third' not in answer
    assert s.launched() == launched and s.pool.name == 'second' and len(r.launches) == before
    data = fresh_handoff.load(r.svc.runtime, s.name)
    assert data['phase'] == 'waiting' and data['pool'] == 'third'
    [order] = handoff_orders(r)
    assert r.svc.store.is_open(order)
    monkeypatch.setattr(s.harness, 'screen', lambda *_: Screen('', '', working=False))
    handoff = report_shape(r, order, record)
    launched = s.launched()
    for message in r.svc.attempts.undelivered(r.svc.store, s.name):
        if message.id == handoff.id:
            continue
        r.svc.attempts.close(r.svc.attempts.open(message, s.name, launched.pane, launched.pid, launched.at),
                             'delivered')
    if later_wall:
        s.ship.seats['sentry'] = replace(s.spec, name='sentry', role='systems')
        usage(r.svc, 'third', 1.0)
        replies = r.svc.store.replies(order)
        r.svc.fresh_handoffs()
        r.svc.fresh_handoffs()
        assert s.launched() == launched and len(r.launches) == before
        assert fresh_handoff.load(r.svc.runtime, s.name) == data
        assert r.svc.store.replies(order) == replies and r.svc.store.is_open(handoff)
        assert any(line.startswith('worker:') and 'third is walled' in line
                   for line in watch.facts(s.ship, s.tmux).seat_holds)
        [alert] = r.svc.store.for_seat('sentry')
        assert alert.body == 'worker: staged pool move held. third is walled'
        usage(r.svc, 'third', 0.0)
    r.svc.fresh_handoffs()
    assert s.pool.name == 'third' and s.launched().resume is None
    assert len(r.launches) == before + 1 and '--resume' not in r.launches[-1]
    assert handoff.body in r.launches[-1][-1]
    assert not r.svc.store.is_open(order) and r.svc.store.is_open(handoff)
    started = s.launched()
    r.svc.fresh_handoffs()
    assert s.launched() == started and len(r.launches) == before + 1
    moved = 'moved to pool third from second by qd set and started a new conversation'
    assert [message.body for message in r.svc.store.for_seat(s.name) if message.body == moved] == [moved]
    assert all('third is walled' not in line for line in watch.facts(s.ship, s.tmux).seat_holds)


def plain_claude(r, model):
    r.svc.failover()
    s = r.seat
    s.spec = replace(s.spec, pools=('second', 'third'), pool_choices={}, model=model)
    s.ship.seats[s.name] = s.spec
    return s, s.record()


@pytest.mark.parametrize(('group', 'model'), [(None, None), ('sonnet', 'claude-sonnet-4-6')])
def test_plain_list_relaunch_moves_from_proved_wall_with_oversized_identity(running, monkeypatch, group, model):
    r = running
    s, owned = plain_claude(r, model)
    before = s.launched()
    identity = s.harness.session_id(owned)
    cross_read_bound(owned, s.harness.name, monkeypatch)
    # No running model can be read from this synthetic screen or the above-bound record.
    monkeypatch.setattr(s.harness, 'screen', lambda _: Screen('', '', working=False))
    at = post.now()
    r.svc.ledger.record_usage('second', Usage((Window('5h', 1.0, at + timedelta(hours=1), group=group),), at))
    assert quota.current_walled(s, r.svc.ledger) is (group is None)
    proof = s.restart()
    assert 'relaunched on third' in proof and 'resumed ' in proof
    assert s.launched() != before and s.launched().resume == identity and s.record() == owned
    assert s.pool.name == 'third'


@pytest.mark.parametrize('running', [False, True], indirect=True, ids=['crew', 'human'])
def test_plain_list_wall_preserves_the_no_identity_policy(running, monkeypatch, capsys):
    r = running
    s, owned = plain_claude(r, None)
    before = s.launched()
    usage(r.svc, 'second', 1.0)
    owned.unlink()
    monkeypatch.setattr(cli, 'seat_of', lambda *_: s)
    monkeypatch.setattr(s.harness, 'screen', lambda _: Screen('', '', working=False))
    assert qd(s, 'restart') == (2 if s.session.human else 0)
    assert s.launched() == before and s.pool.name == 'second' and s.is_up
    assert len(handoff_orders(r)) == (0 if s.session.human else 1)
    if s.session.human:
        assert 'start a new conversation carrying its handoff' in capsys.readouterr().err


def test_oversized_claude_stable_filename_and_pi_header(tmp_path, monkeypatch):
    native = tmp_path / 'projects' / 'synthetic'
    native.mkdir(parents=True)
    identity = str(uuid4())
    claude = native / (identity + '.jsonl')
    pi = tmp_path / 'synthetic-pi.jsonl'
    claude.write_text('{"type":"file-history-snapshot"}\n')
    pi.write_text('{"type":"session","id":"synthetic-pi"}\n')
    for record, reader in ((claude, 'claude'), (pi, 'pi')):
        cross_read_bound(record, reader, monkeypatch)
    assert resume_target.identify(get('claude'), claude) == identity
    assert resume_target.identify(get('pi'), pi) == 'synthetic-pi'



@pytest.mark.parametrize('mode', ['normal-stop', 'abrupt'])
def test_handoff_stop_preserves_proved_completion_or_holds_unknown(running, monkeypatch, mode):
    r = running
    s = r.seat
    usage(r.svc, 'first', 0.2)
    r.source.path.unlink()
    r.live['pane'] = None
    monkeypatch.setattr('quarterdeck.server_recovery.pid_dead', lambda _: True)
    start = s._fresh

    class AbruptEnd(BaseException):
        pass

    def interrupted():
        if mode == 'abrupt':
            raise AbruptEnd()
        proof = start()
        controller.requested = True  # the actual section exits only after its body finishes
        return proof

    with monkeypatch.context() as cut:
        cut.setattr(s, '_fresh', interrupted)
        if mode == 'abrupt':
            cut.setattr(relaunch_intent, '_remove', lambda *_: None)
        controller = shutdown.Stop(r.svc.runtime)
        with controller.installed():
            owner = controller.owner
            with pytest.raises(SystemExit if mode == 'normal-stop' else AbruptEnd):
                r.svc.relaunch(s, True, caller='operator')
    monkeypatch.setattr(relaunch_intent, '_gone', lambda candidate: candidate == owner)
    r.svc.relaunch_intents()
    r.svc.fresh_handoffs()
    r.svc.fresh_handoffs()
    state = fresh_handoff.load(r.svc.runtime, s.name)
    [report] = r.svc.store.for_seat('operator')
    if mode == 'normal-stop':
        assert len(r.launches) == 1 and state['phase'] == 'done'
        assert 'started a new conversation because' in report.body
    else:
        assert not r.launches and state['phase'] == 'failed'
        assert 'could not confirm the outcome' in report.body
        assert 'could not confirm the outcome' in r.svc.seat_holds[(s.name, 'fresh_handoffs')]
