"""Old hand conversations retain their bytes and accounting identities after retirement."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from quarterdeck import hand_transcripts, record_location, record_owner
from quarterdeck.harness import get, reader_stamp
from quarterdeck.harness.base import record_fingerprint
from quarterdeck.harness.opencode import OpenCodeHarness
from quarterdeck.ledger import Ledger
from quarterdeck.service import Service
from quarterdeck.ship import Pool, SeatSpec, Session, Ship, ShipError


def transcript(home, name, kind, at, *, complete=True):
    if kind == 'pi':
        path = home / 'sessions' / (name + '.jsonl')
        rows = [dict(type='session', id=name, timestamp=at.isoformat(), cwd='/synthetic'),
                dict(type='message', id='u', parentId=None, timestamp=at.isoformat(),
                     message=dict(role='user', content='Synthetic task ' + name))]
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
                json.dumps(dict(type='text', text='Synthetic task ' + name))))
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


def world(tmp_path, monkeypatch, kind='pi', *, complete=True):
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=9)
    spec = SeatSpec('hand', 'crew', 'hand', kind, ('local',), cwd=tmp_path)
    ship = Ship(tmp_path, 'synthetic', 'operator', tmp_path,
        {'local': Pool('local', 'synthetic-endpoint', endpoint='http://127.0.0.1:1/v1')},
        {'crew': Session('crew', 'crew')}, {'hand': spec})
    home = ship.runtime / 'seats' / 'hand' / 'home'
    record = transcript(home, 'old' if kind == 'pi' else 'data-001', kind, old, complete=complete)
    current = transcript(home, 'current' if kind == 'pi' else 'data-002', kind, now)
    launched = SimpleNamespace(at=(now-timedelta(seconds=1)).isoformat(), pool='local', harness=kind, price=None)
    seat = SimpleNamespace(name='hand', folder=home.parent, home=home, spec=spec, harness=get(kind),
        session=SimpleNamespace(human=False), record=lambda: current, launched=lambda: launched, pane=lambda: None)
    (home.parent / 'record-launches.jsonl').write_text(''.join(json.dumps(dict(record=str(r),
        at=(at-timedelta(seconds=2)).isoformat(), pool='local', harness=kind)) + '\n'
        for r, at in ((record, old), (current, now))))
    for path in (record, current):
        record_owner.register(home.parent, path, kind)
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
        record_owner.register(w.seat.folder, w.record, 'fake')
        assert hand_transcripts.run(w.svc, now=w.now) == 0
        assert w.record.is_file()
        # A separate eligible record exercises both logical and physical retired bindings.
        name = 'eligible' if kind == 'pi' else 'data-003'
        old = datetime.fromtimestamp(w.now, timezone.utc) - timedelta(days=9)
        eligible = transcript(w.seat.home, name, kind, old)
        record_owner.register(w.seat.folder, eligible, kind)
        with (w.seat.folder / 'record-launches.jsonl').open('a') as journal:
            journal.write(json.dumps(dict(record=str(eligible), at=(old-timedelta(seconds=2)).isoformat(),
                                          pool='local', harness=kind)) + '\n')
        w.svc.account_records(process_table={})
        assert hand_transcripts.run(w.svc, now=w.now) == 1
        kept = record_location.resolve(eligible)
        for path in (eligible, kept):
            assert record_location.retired(path)
            with pytest.raises(ShipError, match='retired'):
                remember_launch(w.seat.folder, path, w.seat.launched())
            (w.seat.folder / 'record').write_text(str(path)+'\n')
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
