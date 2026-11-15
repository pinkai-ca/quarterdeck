"""An empty recognized database is not a missing turn; bad or orphaned records remain uncertain."""

from datetime import timedelta
import json
import sqlite3
from types import SimpleNamespace

import pytest

from quarterdeck import service, watch
from quarterdeck.harness import opencode, opencode_record
from quarterdeck.harness.base import Join
from quarterdeck.ledger import table
from quarterdeck.seat import Launched, Seat
from quarterdeck.service import Service
from quarterdeck.ship import Pool, SeatSpec, Session, Ship
from .test_opencode_record import AT, database, session, user, assistant


@pytest.fixture
def setup(tmp_path, monkeypatch):
    pool = Pool('local', 'local', endpoint='http://synthetic.invalid/v1')
    spec = SeatSpec('worker', 'crew', 'worker', 'opencode', ('local',), up=False)
    ship = Ship(tmp_path/'ship', 'synthetic', 'operator', tmp_path, {'local': pool},
                {'crew': Session('crew', 'Synthetic')}, {'worker': spec})
    tmux = SimpleNamespace(pane=lambda name: None)
    seat = Seat(ship, spec, tmux)
    history = seat.home/'data-001'/'opencode'/'opencode.db'
    con = database(history)
    session(con)
    user(con, 'historical-input', 'root', 10, 'Synthetic measured input')
    assistant(con, 'historical-answer', 'historical-input', 'root', 20)
    con.commit()
    con.close()
    launched = Launched('%1', 1, (AT-timedelta(seconds=1)).isoformat(), 'local', None,
                        ['opencode'], 'synthetic', harness='opencode')
    launched.write(seat.trusted/'launch.json')
    (seat.trusted/'record').write_text(str(history))
    monkeypatch.setattr(service.machine, 'processes', lambda: {})
    svc = Service(ship, tmux)
    svc.account()
    fresh = seat.home/'data-002'/'opencode'/'opencode.db'
    con = database(fresh)
    con.commit()
    con.close()
    (seat.trusted/'record').write_text(str(fresh))
    yield svc, seat, history, fresh
    svc.ledger.close()


def test_empty_database_is_idle_without_inventing_input_time_or_session(setup):
    _, seat, _, fresh = setup
    state, turns = opencode.read(fresh)
    assert state.turn_open is False and state.turns == 0 and turns == []
    assert state.last_input is state.last_input_at is state.last_write is state.model is None
    assert state.open_tool is None and state.queued == ()
    assert opencode.session_id(fresh) is None and opencode.first_timestamp(fresh) is None
    assert opencode.inputs(fresh) == []
    observed = SimpleNamespace(pane=lambda: SimpleNamespace(dead=False), harness=seat.harness, join=lambda: Join(fresh))
    assert watch.activity(observed).state == 'idle'


def test_historical_totals_stay_complete_then_first_prompt_and_turn_are_read(setup):
    svc, seat, history, fresh = setup
    baseline = {by: svc.ledger.rollup(by) for by in ('seat', 'pool')}
    assert all(rows[0].turns == 1 and rows[0].unmeasured == 0 for rows in baseline.values())
    # A marker left by the old reader is cleared on the next service read.
    svc.ledger.mark_record(fresh, seat.name, 'local', 'incomplete turn state in record')
    svc.account()
    assert svc.ledger.incomplete_records() == []
    assert {by: svc.ledger.rollup(by) for by in baseline} == baseline
    assert all('+' not in '\n'.join(table(svc.ledger, by)) for by in baseline)
    stamp = svc.read_marks[fresh]
    svc.account()
    assert svc.read_marks[fresh] == stamp and svc.ledger.ingested(history) == 1
    con = sqlite3.connect(fresh)
    session(con, at=100)
    user(con, 'fresh-input', 'root', 110, 'First synthetic prompt')
    con.commit()
    svc.account()
    assert svc.read_marks[fresh] != stamp
    state = opencode.state(fresh)
    assert state.turn_open and state.last_input == 'First synthetic prompt' and state.turns == 0
    assert svc.ledger.incomplete_records() == [] and svc.ledger.ingested(fresh) == 0
    assistant(con, 'fresh-answer', 'fresh-input', 'root', 120)
    con.commit()
    con.close()
    svc.account()
    svc.account()
    assert svc.ledger.ingested(fresh) == svc.ledger.ingested(history) == 1
    for by in ('seat', 'pool'):
        row = svc.ledger.rollup(by)[0]
        assert (row.turns, row.read_uncached, row.written, row.unmeasured) == (2, 6, 4, 0)
        assert '+' not in '\n'.join(table(svc.ledger, by))


@pytest.mark.parametrize('bad', ['missing', 'unreadable', 'wrong-schema', 'wrong-columns', 'corrupt', 'orphan-message', 'orphan-part', 'child-only'])
def test_bad_and_incomplete_records_keep_reader_and_accounting_uncertainty(setup, monkeypatch, bad):
    svc, seat, _, fresh = setup
    svc.account()  # remember the empty record's proven reader/launch and fingerprint
    if bad == 'missing':
        fresh.unlink()
    elif bad == 'unreadable':
        original = opencode_record.snapshot
        def unreadable(path):
            if path == fresh:
                raise OSError('synthetic unreadable snapshot')
            return original(path)
        monkeypatch.setattr(opencode_record, 'snapshot', unreadable)
        svc.read_marks.pop(fresh)  # model a service restart/retry without touching real permissions
    elif bad == 'corrupt':
        fresh.write_bytes(b'synthetic invalid SQLite')
    else:
        con = sqlite3.connect(fresh)
        if bad == 'wrong-schema':
            con.execute('DROP TABLE part')
        elif bad == 'wrong-columns':
            con.execute('DROP TABLE session')
            con.execute('CREATE TABLE session (unrecognized TEXT)')
        elif bad == 'orphan-message':
            user(con, 'orphan', 'missing-root', 10, 'Synthetic orphan prompt')
        elif bad == 'orphan-part':
            con.execute('INSERT INTO part VALUES (?,?,?,?,?,?)', ('part','missing-message','missing-root',1,1,'{}'))
        else:
            session(con, 'child', 10, 'missing-root')
        con.commit()
        con.close()
    if bad in ('orphan-message', 'orphan-part', 'child-only'):
        assert opencode.state(fresh).turn_open is None
    else:
        with pytest.raises(OSError):
            opencode.read(fresh)
    observed = SimpleNamespace(pane=lambda: SimpleNamespace(dead=False), harness=seat.harness, join=lambda: Join(fresh))
    assert watch.activity(observed).state == 'unknown'
    svc.account()
    assert len(svc.ledger.incomplete_records()) == 1
    assert all(svc.ledger.rollup(by)[0].unmeasured == 1 for by in ('seat', 'pool'))
    assert all('+' in '\n'.join(table(svc.ledger, by)) for by in ('seat', 'pool'))
