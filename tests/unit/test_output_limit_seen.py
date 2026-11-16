"""A seat that stops at its output limit with an order open is seen.

Each reader names a turn that ended at its program's output limit in the forms measured in real crew records on 15
November: pi's stopReason 'length' (123 such messages) and OpenCode's finish 'length' (14). Claude's records showed no
'max_tokens', Codex's rollouts and Grok's updates no limit form at all: for them it is unknown, never guessed. A seat
whose last turn stopped so, with no turn running and an order of its own open, is named on the status line, and its
alarm goes once to the systems seats; both clear when a new turn starts or the order closes.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
from types import SimpleNamespace

import pytest

from quarterdeck import alarms, output_limit, post, record_follow
from quarterdeck.harness import get
from quarterdeck.harness.synthetic_record import ClaudeShape, PiShape
from quarterdeck.ship import SeatSpec, Session, Ship


def pi_record(tmp_path, end_reason):
    shape = PiShape(tmp_path / 'home', tmp_path, 'synthetic', 'synthetic-model')
    shape.end_reason = end_reason
    shape.begin('Synthetic task.', 'synthetic-model', 'low')
    shape.end('Synthetic answer.')
    return shape


@pytest.mark.parametrize('reason, stopped', [('length', True), ('stop', False)])
def test_pi_names_a_turn_that_stopped_at_its_output_limit(tmp_path, reason, stopped):
    shape = pi_record(tmp_path, reason)
    assert get('pi').state(shape.path).ended_at_limit is stopped
    shape.begin('Synthetic next task.', 'synthetic-model', 'low')
    assert get('pi').state(shape.path).ended_at_limit is None, 'a turn running: nothing ended to judge'


def opencode_record(tmp_path, finish):
    path = tmp_path / 'home' / 'data-001' / 'opencode' / 'opencode.db'
    path.parent.mkdir(parents=True)
    ms = int(datetime(2026, 11, 15, 12, tzinfo=timezone.utc).timestamp() * 1000)
    with sqlite3.connect(path) as db:
        db.executescript('''CREATE TABLE session (id TEXT, time_created INTEGER, time_updated INTEGER, parent_id TEXT);
            CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
            CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER,
                               data TEXT);''')
        db.execute('INSERT INTO session VALUES (?,?,?,NULL)', ('synthetic', ms, ms))
        db.execute('INSERT INTO message VALUES (?,?,?,?,?)', ('u', 'synthetic', ms, ms, json.dumps(dict(
            role='user', time=dict(created=ms), model=dict(modelID='synthetic-model')))))
        db.execute('INSERT INTO part VALUES (?,?,?,?,?,?)', ('text', 'u', 'synthetic', ms, ms,
                                                             json.dumps(dict(type='text', text='Synthetic task.'))))
        db.execute('INSERT INTO message VALUES (?,?,?,?,?)', ('a', 'synthetic', ms + 1000, ms + 1000, json.dumps(dict(
            role='assistant', parentID='u', modelID='synthetic-model', finish=finish,
            time=dict(created=ms + 1000, completed=ms + 1001), tokens=dict(input=3, output=2), cost=.25))))
    db.close()
    return path


@pytest.mark.parametrize('finish, stopped', [('length', True), ('stop', False)])
def test_opencode_names_a_turn_that_stopped_at_its_output_limit(tmp_path, finish, stopped):
    assert get('opencode').state(opencode_record(tmp_path, finish)).ended_at_limit is stopped


def test_a_form_not_measured_is_unknown_never_guessed(tmp_path):
    """Claude's records on this ship showed no max_tokens stop: a turn that ends so reads unknown, not stopped."""
    shape = ClaudeShape(tmp_path / 'home', tmp_path, 'synthetic', 'synthetic-model')
    shape.begin('Synthetic task.', 'synthetic-model', 'low')
    shape.assistant([{'type': 'thinking', 'thinking': 'Synthetic.'}], 'max_tokens')
    shape.write({'type': 'system', 'subtype': 'turn_duration', 'durationMs': 1, 'sessionId': shape.session,
                 'timestamp': datetime.now(timezone.utc).isoformat()})
    assert get('claude').state(shape.path).ended_at_limit is None


@pytest.fixture
def ship(tmp_path):
    seats = {'worker-example': SeatSpec('worker-example', 'crew', 'worker', 'pi', ('local',)),
             'systems-example': SeatSpec('systems-example', 'crew', 'systems', 'pi', ('local',))}
    return Ship(tmp_path, 'synthetic', 'operator', tmp_path, {}, {'crew': Session('crew', 'crew')}, seats)


def seat_on(ship, shape, human=False):
    record_follow.forget('worker-example')
    return SimpleNamespace(name='worker-example', ship=ship, session=SimpleNamespace(human=human),
                           record=lambda: shape.path, harness=get('pi'), launched=lambda: None)


def test_the_status_line_names_the_seat_only_with_an_order_open_and_no_turn_running(ship, tmp_path):
    store = post.Store(ship.runtime)
    shape = pi_record(tmp_path, 'length')
    seat = seat_on(ship, shape)
    assert output_limit.line(seat, store) is None, 'no order open: nothing waits on it'
    order = store.new('order', 'captain', 'worker-example', 'Synthetic order.')
    assert output_limit.line(seat, store) == 'worker-example: its last turn stopped at its output limit with an order open'
    assert output_limit.line(seat_on(ship, shape, human=True), store) is None, "a human's seat is never read here"
    shape.begin('Synthetic next task.', 'synthetic-model', 'low')
    assert output_limit.line(seat_on(ship, shape), store) is None, 'a new turn running clears it'
    shape.end('Synthetic answer.')
    assert output_limit.line(seat_on(ship, shape), store) is not None, 'stopped there again'
    store.new('report', 'worker-example', 'captain', 'Synthetic report.', re=order.id)
    assert output_limit.line(seat_on(ship, shape), store) is None, 'the order closed clears it'


def test_the_alarm_goes_once_to_the_systems_seats_and_clears(ship, tmp_path):
    store = post.Store(ship.runtime)
    shape = pi_record(tmp_path, 'length')
    order = store.new('order', 'captain', 'worker-example', 'Synthetic order.')
    service = SimpleNamespace(ship=ship, store=store, log=lambda *a, **k: None,
                              systems_recipients=lambda seat: ['systems-example'])

    def told():
        return [m for m in post.Store(ship.runtime).all() if m.to == 'systems-example' and m.kind == 'report']
    output_limit.step(service, seat_on(ship, shape))
    [first] = told()
    assert first.body.startswith('worker-example: its last turn stopped at its output limit with an order open')
    assert 'nothing was typed' in first.body
    output_limit.step(service, seat_on(ship, shape))
    assert len(told()) == 1, 'once, not at every pass'
    store.new('report', 'worker-example', 'captain', 'Synthetic report.', re=order.id)
    output_limit.step(service, seat_on(ship, shape))
    assert alarms.load(ship.runtime, 'worker-example', output_limit.CHANNEL)['key'] is None, 'the order closed: cleared'
    store.new('order', 'captain', 'worker-example', 'Synthetic second order.')
    output_limit.step(service, seat_on(ship, shape))
    assert len(told()) == 2, 'a stop standing again is told again'
