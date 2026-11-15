"""Old hand conversations retain their bytes and accounting identities after retirement."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from quarterdeck import hand_transcripts, pr_screen, record_location, record_owner, trusted_files
from quarterdeck.harness import get, reader_stamp
from quarterdeck.harness.base import record_fingerprint
from quarterdeck.harness.opencode import OpenCodeHarness
from quarterdeck.ledger import Ledger
from quarterdeck.service import Service
from quarterdeck.ship import Pool, SeatSpec, Session, Ship, ShipError


def transcript(home, name, kind, at, *, complete=True, task=None):
    if kind == 'pi':
        path = home / 'sessions' / (name + '.jsonl')
        rows = [dict(type='session', id=name, timestamp=at.isoformat(), cwd='/synthetic'),
                dict(type='message', id='u', parentId=None, timestamp=at.isoformat(),
                     message=dict(role='user', content=task or 'Synthetic task ' + name))]
        rows.append(dict(type='message', id='a', parentId='u', timestamp=(at + timedelta(seconds=1)).isoformat(),
            message=dict(role='assistant', model='synthetic-model', provider='synthetic',
                content=[dict(type='text', text='Synthetic answer')], stopReason='stop',
                usage=dict(input=3, output=2, cacheRead=0, cacheWrite=0, totalTokens=5,
                           cost=dict(total=0.25)))))
        if not complete:
            rows.append(dict(type='message', id='u2', parentId='a', timestamp=(at+timedelta(seconds=2)).isoformat(),
                             message=dict(role='user', content='Synthetic unfinished task')))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    else:
        path = home / name / 'opencode' / 'opencode.db'
        path.parent.mkdir(parents=True)
        ms = int(at.timestamp() * 1000)
        with sqlite3.connect(path) as db:
            db.executescript('''CREATE TABLE session (id TEXT, time_created INTEGER, time_updated INTEGER, parent_id TEXT);
                CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
                CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);''')
            db.execute('INSERT INTO session VALUES (?,?,?,NULL)', (name, ms, ms))
            db.execute('INSERT INTO message VALUES (?,?,?,?,?)', ('u', name, ms, ms,
                json.dumps(dict(role='user', time=dict(created=ms), model=dict(modelID='synthetic-model')))))
            db.execute('INSERT INTO part VALUES (?,?,?,?,?,?)', ('text', 'u', name, ms, ms,
                json.dumps(dict(type='text', text=task or 'Synthetic task ' + name))))
            db.execute('INSERT INTO message VALUES (?,?,?,?,?)', ('a', name, ms+1000, ms+1000,
                json.dumps(dict(role='assistant', parentID='u', modelID='synthetic-model', finish='stop',
                    time=dict(created=ms+1000, completed=ms+1001), tokens=dict(input=3, output=2), cost=.25))))
            db.execute('INSERT INTO part VALUES (?,?,?,?,?,?)', ('reply', 'a', name, ms+1000, ms+1000,
                json.dumps(dict(type='text', text='Synthetic answer'))))
            if not complete:
                db.execute('INSERT INTO message VALUES (?,?,?,?,?)', ('u2', name, ms+2000, ms+2000,
                    json.dumps(dict(role='user', time=dict(created=ms+2000)))))
                db.execute('INSERT INTO part VALUES (?,?,?,?,?,?)', ('text2', 'u2', name, ms+2000, ms+2000,
                    json.dumps(dict(type='text', text='Synthetic unfinished task'))))
        db.close()
        # Match the native per-launch ancillary folders without importing native content.
        for folder in ('log', 'repos', 'snapshot'):
            (path.parent / folder).mkdir()
            (path.parent / folder / 'synthetic').write_bytes(b'synthetic retained launch data')
    for source in ([path] if kind == 'pi' else [path, *path.parent.rglob('*')]):
        os.utime(source, (at.timestamp(), at.timestamp()))
    return path


def world(tmp_path, monkeypatch, kind='pi', *, complete=True, age=timedelta(days=9), task=None):
    now = datetime.now(timezone.utc)
    old = now - age
    spec = SeatSpec('hand', 'crew', 'hand', kind, ('local',), cwd=tmp_path)
    ship = Ship(tmp_path, 'synthetic', 'operator', tmp_path,
        {'local': Pool('local', 'synthetic-endpoint', endpoint='http://127.0.0.1:1/v1')},
        {'crew': Session('crew', 'crew')}, {'hand': spec})
    home = ship.runtime / 'seats' / 'hand' / 'home'
    trusted = trusted_files.folder(ship.runtime, 'hand')
    trusted.mkdir(parents=True)
    record = transcript(home, 'old' if kind == 'pi' else 'data-001', kind, old, complete=complete, task=task)
    current = transcript(home, 'current' if kind == 'pi' else 'data-002', kind, now)
    launched = SimpleNamespace(at=(now-timedelta(seconds=1)).isoformat(), pool='local', harness=kind, price=None)
    seat = SimpleNamespace(name='hand', folder=home.parent, trusted=trusted, home=home, spec=spec, harness=get(kind),
        session=SimpleNamespace(human=False), record=lambda: current, launched=lambda: launched, pane=lambda: None)
    (trusted / 'record-launches.jsonl').write_text(''.join(json.dumps(dict(record=str(r),
        at=(at-timedelta(seconds=2)).isoformat(), pool='local', harness=kind)) + '\n'
        for r, at in ((record, old), (current, now))))
    for path in (record, current):
        record_owner.register(trusted, path, kind)
    svc = Service(ship, SimpleNamespace())
    monkeypatch.setattr(svc, 'resolved_seats', lambda: [seat])
    logs = []
    monkeypatch.setattr(svc, 'log', lambda event, **data: logs.append((event, data)))
    svc.account_records(process_table={})
    assert not svc.ledger.catching_up()
    return SimpleNamespace(svc=svc, seat=seat, record=record, current=current, now=now.timestamp(), logs=logs)


def size(folder):
    return sum(p.stat().st_size for p in folder.rglob('*') if p.is_file())


@pytest.mark.parametrize('kind', ['pi', 'opencode'])
def test_accounted_old_record_retires_and_size_falls_current_stays(tmp_path, monkeypatch, kind):
    w = world(tmp_path, monkeypatch, kind)
    try:
        before = size(w.seat.folder)
        native = w.record.read_bytes()
        reading = w.seat.harness.read(w.record)
        inputs = w.seat.harness.attribution_inputs(w.record)
        assert hand_transcripts.run(w.svc, now=w.now) == 1
        kept = record_location.resolve(w.record)
        assert kept != w.record and not w.record.exists() and kept.read_bytes() == native
        assert size(w.seat.folder) < before and w.current.is_file()
        assert w.seat.harness.read(w.record) == reading
        assert w.seat.harness.attribution_inputs(w.record) == inputs
        assert w.record in w.seat.harness.records(w.seat.home)
        assert any(owned.path == w.record and owned.read() == reading for owned in record_owner.records(w.seat))
        assert hand_transcripts.run(w.svc, now=w.now) == 0
    finally:
        w.svc.ledger.close()


@pytest.mark.parametrize('kind', ['pi', 'opencode'])
@pytest.mark.parametrize('hold', ['unaccounted', 'changed', 'current', 'young', 'open', 'catchup', 'active-planner'])
def test_unfinished_or_ineligible_sources_stay(tmp_path, monkeypatch, kind, hold):
    w = world(tmp_path, monkeypatch, kind, complete=hold != 'open')
    try:
        if hold == 'open':
            assert w.svc.ledger.db.execute('SELECT 1 FROM record_accounting WHERE record=?',
                                          (str(w.record),)).fetchone() == (1,)
        if hold == 'unaccounted':
            with w.svc.ledger.record_pass() as book:
                book.mark_accounted(w.record, None)
        elif hold == 'changed':
            os.utime(w.record, (w.now-8*86400, w.now-8*86400))
        elif hold == 'current':
            w.seat.record = lambda: w.record
        elif hold == 'young':
            os.utime(w.record, (w.now, w.now))
        elif hold == 'catchup':
            w.svc.ledger.begin_catchup(['synthetic-new-epoch'])
        elif hold == 'active-planner':
            w.svc.account_driver = SimpleNamespace(active=True)
        assert hand_transcripts.run(w.svc, now=w.now) == 0
        assert w.record.is_file() and record_location.resolve(w.record) == w.record
    finally:
        w.svc.ledger.close()


@pytest.mark.parametrize('kind', ['pi', 'opencode'])
def test_open_program_descriptor_holds_old_record(tmp_path, monkeypatch, kind):
    w = world(tmp_path, monkeypatch, kind)
    try:
        with w.record.open('rb'):
            assert hand_transcripts.run(w.svc, now=w.now) == 0
        assert hand_transcripts.run(w.svc, now=w.now) == 1
    finally:
        w.svc.ledger.close()


@pytest.mark.parametrize('kind', ['pi', 'opencode'])
def test_changed_epoch_replay_of_copy_keeps_all_measurements_byte_identical(tmp_path, monkeypatch, kind):
    w = world(tmp_path, monkeypatch, kind)
    book = w.svc.ledger
    try:
        # Keep a real independent copy of the operational ledger, before retiring.
        copy = tmp_path / 'ledger-copy.sqlite'
        with sqlite3.connect(copy) as target:
            book.db.backup(target)
        tables = ('turns', 'tool_calls', 'turn_costs', 'record_launches', 'launch_prices', 'launch_harnesses')
        def rows(db):
            return {t: json.dumps(db.execute(f'SELECT * FROM {t} ORDER BY 1,2').fetchall(),
                                  sort_keys=True).encode() for t in tables}
        expected = rows(book.db)
        def verdicts(db):
            return json.dumps(db.execute('SELECT record,turn,identity,received,status,orders,reason,grouping,valid '
                                         'FROM turn_attribution ORDER BY record,turn').fetchall()).encode()
        expected_verdicts = verdicts(book.db)
        assert hand_transcripts.run(w.svc, now=w.now) == 1
        book.close()
        w.svc.ledger = book = Ledger(copy)
        # Fresh caches and a changed reader/attribution epoch force actual native replay.
        w.svc.read_marks.clear()
        w.svc.accounting_epoch = ['synthetic-changed-reader-and-attribution-epoch']
        book.begin_catchup(w.svc.accounting_epoch)
        keys = {name: ['synthetic-changed-reader', value] for name, value in reader_stamp.readers().items()}
        monkeypatch.setattr(reader_stamp, 'attributions', lambda: keys)
        w.svc.ledger.attributions.last = None
        w.svc.account_records(process_table={}, source_reader_keys=keys)
        assert not book.catching_up()
        assert rows(book.db) == expected
        assert verdicts(book.db) == expected_verdicts
        assert book.db.execute('SELECT * FROM records').fetchall() == []
        assert book.db.execute('SELECT COUNT(*) FROM turn_attribution WHERE valid=0').fetchone() == (0,)
        # Measurements are also idempotent on a second complete accounting pass.
        w.svc.account_records(process_table={}, source_reader_keys=keys)
        assert rows(book.db) == expected
        # A wholly new ledger also rediscovers and replays retained sources.
        book.close()
        w.svc.ledger = book = Ledger(tmp_path / 'rebuilt-ledger.sqlite')
        book.begin_catchup(w.svc.accounting_epoch)
        w.svc.read_marks.clear()
        w.svc.account_records(process_table={}, source_reader_keys=keys)
        assert rows(book.db) == expected
        assert verdicts(book.db) == expected_verdicts
    finally:
        book.close()


def test_sqlite_bundle_moves_once_with_wal_and_shm(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, 'opencode')
    try:
        # Empty native sidecars are valid; no live SQLite connection holds them.
        for suffix in ('-wal', '-shm'):
            path = Path(str(w.record) + suffix)
            path.write_bytes(b'')
            os.utime(path, (w.now-9*86400, w.now-9*86400))
        w.svc.read_marks.clear()
        w.svc.account_records(process_table={})
        original = {p.relative_to(w.record.parent): p.read_bytes()
                    for p in w.record.parent.rglob('*') if p.is_file()}
        moves = []
        rename = Path.rename
        def observed(source, target):
            moves.append((source, target))
            return rename(source, target)
        monkeypatch.setattr(Path, 'rename', observed)
        assert hand_transcripts.run(w.svc, now=w.now) == 1
        kept = record_location.resolve(w.record)
        assert moves == [(w.record.parent, kept.parent)]
        assert {p.relative_to(kept.parent): p.read_bytes() for p in kept.parent.rglob('*') if p.is_file()} == original
        assert OpenCodeHarness._data_dir(w.seat.home, None, 'hand').name == 'data-003'
        with pytest.raises(ShipError, match='conversation is retired'):
            OpenCodeHarness._data_dir(w.seat.home, 'data-001', 'hand')
        assert w.seat.harness.join(w.seat.home, datetime(2000,1,1,tzinfo=timezone.utc)).record == w.current
    finally:
        w.svc.ledger.close()


def test_pi_retired_conversation_refuses_native_resume(tmp_path, monkeypatch):
    from quarterdeck.endpoint import Choice
    w = world(tmp_path, monkeypatch)
    try:
        assert hand_transcripts.run(w.svc, now=w.now) == 1
        monkeypatch.setattr(Choice, 'read', lambda home: SimpleNamespace(model='synthetic-model'))
        with pytest.raises(ShipError, match='conversation is retired'):
            w.seat.harness.launch(w.seat.spec, SimpleNamespace(name='synthetic'), w.seat.home,
                                 tmp_path / 'brief', 'old', None)
    finally:
        w.svc.ledger.close()


@pytest.mark.parametrize('kind', ['pi', 'opencode'])
def test_unknown_reader_stays_and_retired_binding_cannot_be_rejoined(tmp_path, monkeypatch, kind):
    from quarterdeck.provenance import remember_launch
    from quarterdeck.seat import Seat
    w = world(tmp_path, monkeypatch, kind)
    try:
        record_owner.register(w.seat.trusted, w.record, 'fake')
        assert hand_transcripts.run(w.svc, now=w.now) == 0
        assert w.record.is_file()
        # A separate eligible record exercises both logical and physical retired bindings.
        name = 'eligible' if kind == 'pi' else 'data-003'
        old = datetime.fromtimestamp(w.now, timezone.utc) - timedelta(days=9)
        eligible = transcript(w.seat.home, name, kind, old)
        record_owner.register(w.seat.trusted, eligible, kind)
        with (w.seat.trusted / 'record-launches.jsonl').open('a') as journal:
            journal.write(json.dumps(dict(record=str(eligible), at=(old-timedelta(seconds=2)).isoformat(),
                                          pool='local', harness=kind)) + '\n')
        w.svc.account_records(process_table={})
        assert hand_transcripts.run(w.svc, now=w.now) == 1
        kept = record_location.resolve(eligible)
        for path in (eligible, kept):
            assert record_location.retired(path)
            with pytest.raises(ShipError, match='retired'):
                remember_launch(w.seat.trusted, path, w.seat.launched())
            (w.seat.trusted / 'record').write_text(str(path)+'\n')
            with pytest.raises(ShipError, match='conversation is retired'):
                Seat._resume_id(w.seat)
    finally:
        w.svc.ledger.close()


def test_busy_planner_does_not_delay_next_retirement_opportunity(monkeypatch):
    svc = SimpleNamespace(ledger=SimpleNamespace(catching_up=lambda: False),
                          account_driver=SimpleNamespace(active=True))
    calls = []
    monkeypatch.setattr(hand_transcripts, 'run', lambda service: calls.append(service))
    monkeypatch.setattr(hand_transcripts.time, 'monotonic', lambda: 1000)
    hand_transcripts.due(svc)
    assert calls == [] and not hasattr(svc, 'hand_transcripts_due')
    svc.account_driver.active = False
    hand_transcripts.due(svc)
    assert calls == [svc] and svc.hand_transcripts_due == 1300
    hand_transcripts.due(svc)
    assert calls == [svc]


def test_without_ledger_retirement_step_does_nothing_and_reads_nothing(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail('retirement inspected runtime without a ledger')
    monkeypatch.setattr(hand_transcripts.time, 'monotonic', unexpected)
    monkeypatch.setattr(hand_transcripts.reader_stamp, 'readers', unexpected)
    monkeypatch.setattr(Path, 'iterdir', unexpected)
    monkeypatch.setattr(record_location, 'destination', unexpected)
    service = SimpleNamespace()
    Service.retire_hand_transcripts(service)
    hand_transcripts.due(service)
    assert hand_transcripts.run(service) == 0
    assert vars(service) == {}


# -- a screening launch's record (pr_screen) ------------------------------------------------------------------------

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures' / 'pr_screen'


def case_strings(name):
    """The planted case's own distinctive strings: its title and each line of its files longer than twenty letters."""
    case = json.loads((FIXTURES / name / 'pr.json').read_text())
    return [case['title'], *(line[1:].strip() for f in case['files'] for line in f['content'].splitlines()
                             if line.startswith('+') and len(line.strip()) > 20)]


def holds(home, strings):
    return sorted(str(path.relative_to(home)) for path in home.rglob('*') if path.is_file()
                  and any(s.encode() in path.read_bytes() for s in strings))


@pytest.mark.parametrize('kind', ['pi', 'opencode'])
def test_a_screening_record_leaves_the_home_once_accounted_without_the_weeks_wait(tmp_path, monkeypatch, kind):
    strings = case_strings('note-elsewhere')
    w = world(tmp_path, monkeypatch, kind, age=timedelta(minutes=2), task='\n'.join(strings))
    try:
        assert holds(w.seat.home, strings), 'the screening launch read the case'
        reading = w.seat.harness.read(w.record)
        assert hand_transcripts.run(w.svc, now=w.now) == 0, 'an ordinary record two minutes old waits its week'
        pr_screen.mark_screening(w.svc.runtime, 'hand', [str(w.record)])
        assert pr_screen.screening_left(w.svc.runtime, 'hand')
        assert hand_transcripts.run(w.svc, now=w.now) == 1
        assert holds(w.seat.home, strings) == [], 'nothing under the hand\'s home holds the case\'s text'
        kept = record_location.resolve(w.record)
        assert kept.is_relative_to(w.svc.runtime / 'retired' / 'hand-transcripts')
        assert w.seat.harness.read(w.record) == reading, 'the ledger reads it under its own name, figures kept'
        assert not pr_screen.screening_left(w.svc.runtime, 'hand'), 'nothing left: launches may resume again'
    finally:
        w.svc.ledger.close()


def test_a_screening_record_still_open_stays_and_keeps_every_launch_fresh(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, 'pi', complete=False, age=timedelta(minutes=2))
    try:
        pr_screen.mark_screening(w.svc.runtime, 'hand', [str(w.record)])
        assert hand_transcripts.run(w.svc, now=w.now) == 0, 'an open conversation is never moved'
        assert w.record.is_file() and pr_screen.screening_left(w.svc.runtime, 'hand')
    finally:
        w.svc.ledger.close()


def test_a_screening_record_leaves_even_before_the_next_launch_has_a_record(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, 'pi', age=timedelta(minutes=2))
    try:
        monkeypatch.setattr(w.seat, 'record', lambda: None)  # the fresh launch after the screen has written nothing yet
        assert hand_transcripts.run(w.svc, now=w.now) == 0, 'without a screening record the hand waits, as before'
        pr_screen.mark_screening(w.svc.runtime, 'hand', [str(w.record)])
        assert hand_transcripts.run(w.svc, now=w.now) == 1 and not w.record.exists()
        assert w.current.is_file(), 'only the screening record moves'
    finally:
        w.svc.ledger.close()


@pytest.mark.parametrize('kind', ['pi', 'opencode'])
def test_the_home_sweep_keeps_a_screening_records_whole_unit_for_the_retirement(tmp_path, monkeypatch, kind):
    w = world(tmp_path, monkeypatch, kind, age=timedelta(minutes=2))
    try:
        live = pr_screen.LiveHands(w.svc.ship, Path('qd'), tmp_path / 'hands')
        assert live.home('hand') == w.seat.home
        kept = live.kept_in_home('hand', [str(w.record)])
        unit = w.record if kind == 'pi' else w.record.parent
        assert kept == [str(unit.relative_to(w.seat.home))], 'the adapter\'s own retirement unit, whole'
        if kind == 'opencode':
            (w.record.parent / 'planted.txt').write_text('written by a fooled hand')
            (w.record.parent / 'opencode.db-wal').write_bytes(b'wal')
            kept = live.kept_in_home('hand', [str(w.record)])
            assert sorted(kept) == sorted(str(p.relative_to(w.seat.home)) for p in
                                          (w.record, w.record.parent / 'opencode.db-wal'))
            before = {k: v for k, v in pr_screen.folder_listing(w.seat.home).items() if 'planted' not in k
                      and 'log' not in k and 'repos' not in k and 'snapshot' not in k}
            moved = pr_screen.move_aside_new(w.seat.home, before, tmp_path / 'aside', keep=kept)
            assert w.record.is_file() and (w.record.parent / 'opencode.db-wal').is_file()
            assert not (w.record.parent / 'planted.txt').exists() and moved
            assert w.seat.harness.retirement_bundle(w.seat.home, w.record, record_location.destination(w.record))
    finally:
        w.svc.ledger.close()


def screening_beside(w, kind='pi'):
    """A second earlier launch's record, two minutes old, listed as a screening launch's; accounted."""
    at = datetime.fromtimestamp(w.now, timezone.utc) - timedelta(minutes=2)
    record = transcript(w.seat.home, 'screened' if kind == 'pi' else 'data-003', kind, at)
    with (w.seat.trusted / trusted_files.LAUNCHES).open('a') as stream:  # where the move keeps it
        stream.write(json.dumps(dict(record=str(record), at=(at - timedelta(seconds=2)).isoformat(), pool='local',
                                     harness=kind)) + '\n')
    record_owner.register(w.seat.trusted, record, kind)
    w.svc.account_records(process_table={})
    pr_screen.mark_screening(w.svc.runtime, 'hand', [str(record)])
    return record


def test_a_listed_screening_record_moves_ahead_of_an_older_eligible_one_outside_the_limit(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, 'pi')  # its old record is nine days old: eligible
    try:
        screened = screening_beside(w)
        assert hand_transcripts.run(w.svc, now=w.now, limit=1) == 2, 'the screening walk is outside the limit'
        moved = [data['record'] for event, data in w.logs if event == 'hand-transcript-retired']
        assert moved == [str(screened), str(w.record)], 'the screening record first'
        assert [data.get('screening') for event, data in w.logs if event == 'hand-transcript-retired'] == [True, None]
    finally:
        w.svc.ledger.close()


def test_a_screening_record_moves_through_a_catch_up_and_between_intervals(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, 'pi')
    try:
        screened = screening_beside(w)
        monkeypatch.setattr(w.svc.ledger, 'catching_up', lambda: True)
        w.svc.hand_transcripts_due = float('inf')  # the five-minute interval has not come round
        hand_transcripts.due(w.svc)
        assert not screened.exists() and w.record.is_file(), 'the screening record alone, at once'
    finally:
        w.svc.ledger.close()


def test_a_screening_record_not_yet_accounted_waits_for_that_alone(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, 'pi')
    try:
        screened = screening_beside(w)
        real = w.svc.ledger.accounted
        monkeypatch.setattr(w.svc.ledger, 'accounted',
                            lambda record, checkpoint: record != screened and real(record, checkpoint))
        hand_transcripts.run(w.svc, now=w.now, limit=0)
        assert screened.is_file(), 'not yet accounted: it waits'
        monkeypatch.setattr(w.svc.ledger, 'accounted', real)  # undo() would drop the world's own patches
        assert hand_transcripts.run(w.svc, now=w.now, limit=0) == 1 and not screened.exists(), 'and for that alone'
        assert w.record.is_file(), 'limit 0: the screening walk alone' 
    finally:
        w.svc.ledger.close()
