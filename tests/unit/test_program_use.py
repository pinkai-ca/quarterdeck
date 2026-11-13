"""Ordinary synthetic public readings and launch interval attribution."""

from datetime import datetime, timezone
import json
import sqlite3
from types import SimpleNamespace as NS

import pytest

from quarterdeck import program_use, watch
from quarterdeck.ledger import Ledger
from quarterdeck.output import value
from quarterdeck.seat import SelectionError, UnresolvedSeat
from quarterdeck.ship import SeatSpec

START = '2001-01-01T10:00:00+00:00'
END = '2001-01-01T10:01:00+00:00'


@pytest.mark.parametrize('values,expected', [([], None), ([None], None), ([False], False),
    ([False, False], False), ([False, None], None), ([None, True], True), ([True, False], True)])
def test_truthful_mixed_activity(values, expected):
    assert program_use.aggregate(values) is expected


def synthetic_seat(tmp_path, turn_open):
    folder = tmp_path / 'synthetic-seat'
    folder.mkdir()
    record = folder / 'ordinary-record'
    (folder / 'record').write_text(str(record))
    launch = NS(at=START, pool='local', harness='fake')
    (folder / 'record-launches.jsonl').write_text(json.dumps(
        dict(record=str(record), at=START, pool='local', harness='fake')) + '\n')
    harness = NS(name='fake', state=lambda _: NS(turn_open=turn_open), waiting_for_human=lambda _: False)
    return NS(folder=folder, harness=harness, launched=lambda: launch,
              pane=lambda: NS(dead=False))


@pytest.mark.parametrize('turn_open', [True, False, None])
def test_current_activity_from_saved_join(tmp_path, turn_open):
    assert program_use.working(synthetic_seat(tmp_path, turn_open)) is turn_open


def test_never_launched_seat_is_unknown(tmp_path):
    seat = synthetic_seat(tmp_path, False)
    seat.launched = lambda: None
    assert program_use.working(seat) is None


def test_unresolved_selection_stays_unknown(tmp_path):
    spec = SeatSpec('synthetic-seat', 'studio', 'worker', 'fake', ('local',))
    seat = UnresolvedSeat(NS(runtime=tmp_path), spec, None, SelectionError(spec.name, 'synthetic unknown'))
    assert program_use.working(seat) is None


def test_program_with_no_turns_and_no_current_seats_is_unknown(tmp_path):
    spec = SeatSpec('synthetic-seat', 'studio', 'worker', 'fake', ('local',))
    result, error = program_use.collect(NS(runtime=tmp_path, seats={spec.name: spec}), [])
    assert result == [dict(harness='fake', working_now=None, last_turn_ended=None)]
    assert error is None
    assert not (tmp_path / 'ledger.sqlite').exists()


def add_turn(book, record, seat, start, end, index=1):
    book.db.execute('INSERT INTO turns (record,turn,seat,pool,order_id,started,ended) VALUES (?,?,?,?,?,?,?)',
                    (record, index, seat, 'local', '-', start, end))


def add_launch(book, record, name, start=START, seat='synthetic-seat'):
    book.provenance.remember(record, seat, 'local', datetime.fromisoformat(start), harness=name)


def test_historical_harness_survives_changed_current_preference(tmp_path):
    with Ledger(tmp_path / 'ledger.sqlite') as book:
        add_launch(book, 'synthetic-record', 'codex')
        add_turn(book, 'synthetic-record', 'synthetic-seat', START, '2001-01-01T12:01:00+02:00')
        book.db.commit()
    spec = SeatSpec('synthetic-seat', 'studio', 'worker', 'fake', ('local',))
    result, error = program_use.collect(NS(runtime=tmp_path, seats={spec.name: spec}), [])
    assert result == [dict(harness='codex', working_now=None, last_turn_ended=END),
                      dict(harness='fake', working_now=None, last_turn_ended=None)]
    assert error is None


def test_latest_end_compares_instants_not_offset_text(tmp_path):
    with Ledger(tmp_path / 'book') as book:
        add_launch(book, 'synthetic-record', 'codex')
        add_turn(book, 'synthetic-record', 'synthetic-seat', START, '2001-01-01T12:01:00+02:00')
        add_turn(book, 'synthetic-record', 'synthetic-seat', START, '2001-01-01T11:02:00+01:00', 2)
        assert program_use.history(book.db)[0]['codex'] == datetime(2001, 1, 1, 10, 2, tzinfo=timezone.utc)


def test_multiple_launches_assign_each_turn_to_its_program(tmp_path):
    with Ledger(tmp_path / 'book') as book:
        add_launch(book, 'synthetic-record', 'fake')
        add_launch(book, 'synthetic-record', 'codex', '2001-01-01T11:00:00+00:00')
        add_turn(book, 'synthetic-record', 'synthetic-seat', START, END)
        add_turn(book, 'synthetic-record', 'synthetic-seat', '2001-01-01T11:00:01+00:00',
                 '2001-01-01T11:01:00+00:00', 2)
        history, _ = program_use.history(book.db)
        assert history['fake'].isoformat() == END
        assert history['codex'].isoformat() == '2001-01-01T11:01:00+00:00'


def test_existing_launch_writer_keeps_positional_shape(tmp_path):
    with Ledger(tmp_path / 'book') as book:
        add_launch(book, 'synthetic-record', 'fake')
        book.db.execute('INSERT OR REPLACE INTO record_launches VALUES (?,?,?,?)',
                        ('synthetic-record', START, 'synthetic-seat', 'local'))
        assert book.db.execute('SELECT * FROM launch_harnesses').fetchall() == []
        assert len(book.db.execute('PRAGMA table_info(record_launches)').fetchall()) == 4


def test_crossing_launch_interval_has_no_invented_last_use(tmp_path):
    with Ledger(tmp_path / 'book') as book:
        add_launch(book, 'synthetic-record', 'fake')
        add_launch(book, 'synthetic-record', 'codex', '2001-01-01T10:00:30+00:00')
        add_turn(book, 'synthetic-record', 'synthetic-seat', START, END)
        assert program_use.history(book.db)[0] == {'fake': None, 'codex': None}


def test_conflicting_harness_evidence_is_not_last_writer_wins(tmp_path):
    with Ledger(tmp_path / 'book') as book:
        add_launch(book, 'synthetic-record', 'fake')
        add_launch(book, 'synthetic-record', 'codex')
        add_launch(book, 'synthetic-record', 'fake')
        add_turn(book, 'synthetic-record', 'synthetic-seat', START, END)
        assert book.db.execute('SELECT harness FROM launch_harnesses').fetchall() == [('-',)]
        assert program_use.history(book.db)[0] == {}


@pytest.mark.parametrize('binding', [None, 'synthetic-future-program'])
def test_historical_record_filename_does_not_select_harness(tmp_path, binding):
    with Ledger(tmp_path / 'book') as book:
        book.provenance.remember('codex-synthetic-record', 'synthetic-seat', 'local', datetime.fromisoformat(START), harness=binding)
        add_turn(book, 'codex-synthetic-record', 'synthetic-seat', START, END)
        assert program_use.history(book.db)[0] == {}


def test_old_readonly_book_has_no_invented_harness(tmp_path):
    path = tmp_path / 'old-book'
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE record_launches(record,at,seat,pool)')
    db.commit()
    db.close()
    before = path.read_bytes()
    db = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    try:
        assert program_use.history(db) == ({}, None)
    finally:
        db.close()
    assert path.read_bytes() == before


def test_service_import_retains_journal_harness(tmp_path):
    home = tmp_path / 'synthetic-seat' / 'home'
    home.mkdir(parents=True)
    record = home / 'ordinary-record'
    (home.parent / 'record-launches.jsonl').write_text(json.dumps(
        dict(record=str(record), at=START, pool='local', harness='codex')) + '\n')
    seat = NS(home=home, name='synthetic-seat')
    with Ledger(tmp_path / 'book') as book:
        with book.record_pass() as staged:
            assert staged.provenance.sync(seat, None, None, commit=False) == {record}
        assert book.db.execute('SELECT harness FROM launch_harnesses').fetchall() == [('codex',)]
        before = book.db.total_changes
        with book.record_pass() as staged:
            assert staged.provenance.sync(seat, None, None, commit=False) == set()
        assert book.db.total_changes == before


def test_status_text_and_json_share_the_public_program_shape(tmp_path, monkeypatch):
    spec = SeatSpec('synthetic-seat', 'studio', 'worker', 'fake', ('local',))
    ship = NS(runtime=tmp_path, seats={spec.name: spec})
    monkeypatch.setattr(watch, 'seats', lambda *_: [])
    monkeypatch.setattr(watch.post, 'Store', lambda _: NS(all=lambda: []))
    monkeypatch.setattr(watch, 'service_state', lambda _: 'ok')
    monkeypatch.setattr(watch, 'facts', lambda *a, **kw: watch.Facts('ok', 0, 0, 0))
    monkeypatch.setattr(watch, 'headline_readings', lambda _: [])
    monkeypatch.setattr(watch.providers, 'identities', lambda _: {})
    data = watch.status_reading(ship, None)
    assert value(data)['programs'] == [dict(harness='fake', working_now=None, last_turn_ended=None)]
    assert 'program fake: unknown · last turn ended unknown' in watch.render_table(data)
    assert 'last turn' not in data['facts'].line()
