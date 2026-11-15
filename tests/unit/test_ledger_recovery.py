"""Malformed accounting is preserved and replayed, without readers moving a live book."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from quarterdeck import cli, ledger_recovery, post, record_owner, servicectl, watch
from quarterdeck.ledger import Ledger
from quarterdeck.seat import Seat
from quarterdeck.service import Service
from quarterdeck.ship import Pool, SeatSpec, Session, Ship, ShipError
from .test_ledger import worked_record

BROKEN = b'not a SQLite database\x00synthetic original bytes\xff'


def corrupt(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(BROKEN)
    return path


def test_service_start_keeps_malformed_bytes_and_rebuilds_real_program_records(tmp_path, monkeypatch):
    def forbidden(_):
        pytest.fail('ordinary recovery forced a sync')
    monkeypatch.setattr('os.fsync', forbidden)
    monkeypatch.setattr('os.fdatasync', forbidden)
    spec = SeatSpec('worker', 'crew', 'worker', 'fake', ('synthetic',), cwd=tmp_path)
    ship = Ship(tmp_path, 'synthetic', 'operator', tmp_path,
                {'synthetic': Pool('synthetic', 'synthetic')}, {'crew': Session('crew', 'crew')}, {'worker': spec})
    seat = Seat(ship, spec, SimpleNamespace())
    seat.folder.mkdir(parents=True)
    seat.trusted.mkdir(parents=True, exist_ok=True)
    store = post.Store(ship.runtime)
    record = worked_record(seat.folder, store, ['one completed turn', 'another completed turn'])
    record_owner.register(seat.trusted, record, 'fake')
    (seat.trusted / 'record-launches.jsonl').write_text(json.dumps(dict(record=str(record),
        at=datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat(), pool='synthetic', harness='fake')) + '\n')
    path = corrupt(ship.runtime / 'ledger.sqlite')
    svc = Service(ship, SimpleNamespace())  # the real service's startup path
    try:
        saved, = ledger_recovery.pending(ship.runtime)
        assert (saved / path.name).read_bytes() == BROKEN
        assert svc.ledger.catching_up()
        svc.account()  # normal accounting uses the native-shaped record and saved launch history
        rows = svc.ledger.rollup('seat')
        assert [(r.key, r.turns, r.tool_calls) for r in rows] == [('worker', 2, 4)]
        assert not svc.ledger.catching_up()
        assert path.read_bytes().startswith(b'SQLite format 3\x00')
        facts = watch.facts(ship, SimpleNamespace())
        assert str((saved / path.name).relative_to(ship.runtime)) in facts.line()
        assert (saved / path.name).read_bytes() == BROKEN
    finally:
        svc.ledger.close()


def test_quick_check_finds_a_bad_data_page_outside_the_schema(tmp_path):
    path = tmp_path / 'ledger.sqlite'
    with Ledger(path) as book:
        book.db.execute('CREATE TABLE synthetic_payload (text TEXT)')
        book.db.execute('INSERT INTO synthetic_payload VALUES (?)', ('x' * 9000,))
        book.db.commit()
        page, = book.db.execute("SELECT rootpage FROM sqlite_master WHERE name='synthetic_payload'").fetchone()
        size, = book.db.execute('PRAGMA page_size').fetchone()
    data = bytearray(path.read_bytes())
    data[(page - 1) * size] = 255
    path.write_bytes(data)
    # The schema is readable: the full quick_check, rather than a header query, finds this damage.
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as raw:
        assert raw.execute('SELECT COUNT(*) FROM sqlite_master').fetchone()[0] > 0
    with Ledger(path) as book:
        assert book.rollup('seat') == [] and book.catching_up()
    saved, = ledger_recovery.pending(tmp_path)
    assert (saved / path.name).read_bytes() == bytes(data)


def test_readers_neither_scan_nor_move_and_explicit_recovery_keeps_alarm_until_clear(drydock, monkeypatch, capsys):
    monkeypatch.delenv('QD_SHIP', raising=False)
    monkeypatch.delenv('QD_SEAT', raising=False)
    path = corrupt(drydock / 'runtime' / 'ledger.sqlite')
    def no_scan(_):
        pytest.fail('reader scanned the entire ledger')
    with monkeypatch.context() as patch:
        patch.setattr(ledger_recovery, 'check', no_scan)
        assert cli.main(['--ship', str(drydock), 'ledger']) == 2
        assert 'ledger malformed' in capsys.readouterr().err
        assert cli.main(['--ship', str(drydock), 'status', '--line']) == 0
        assert 'ledger malformed' in capsys.readouterr().out
        assert path.read_bytes() == BROKEN and ledger_recovery.pending(path.parent) == []
    assert cli.main(['--ship', str(drydock), 'ledger', '--recover']) == 0
    capsys.readouterr()
    folder, = ledger_recovery.pending(path.parent)
    before = (folder / path.name).read_bytes()
    for _ in range(2):
        assert cli.main(['--ship', str(drydock), 'status', '--line']) == 0
        assert str((folder / path.name).relative_to(path.parent)) in capsys.readouterr().out
    assert cli.main(['--ship', str(drydock), 'ledger', '--clear-recovery']) == 0
    assert 'evidence kept' in capsys.readouterr().out
    assert ledger_recovery.alarms(path.parent) == []
    assert (folder / path.name).read_bytes() == before == BROKEN


@pytest.mark.parametrize('code', [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_FULL,
                                  sqlite3.SQLITE_IOERR, sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_READONLY])
def test_non_corruption_errors_are_not_quarantined(tmp_path, monkeypatch, code):
    path = corrupt(tmp_path / 'ledger.sqlite')
    error = sqlite3.OperationalError('synthetic unavailable storage')
    error.sqlite_errorcode = code
    def fail(_):
        raise error
    monkeypatch.setattr(ledger_recovery, 'check', fail)
    with pytest.raises(sqlite3.OperationalError):
        Ledger(path)
    assert path.read_bytes() == BROKEN and ledger_recovery.pending(tmp_path) == []


def test_a_live_service_prevents_writer_recovery(tmp_path):
    path = corrupt(tmp_path / 'ledger.sqlite')
    with servicectl.acquire_lock(tmp_path), pytest.raises(ShipError, match='another service'):
        Ledger(path)
    assert path.read_bytes() == BROKEN and ledger_recovery.pending(tmp_path) == []


@pytest.mark.parametrize('cut', range(8))
def test_interrupted_quarantine_keeps_every_file_and_a_repeated_open_finishes(tmp_path, monkeypatch, cut):
    path = corrupt(tmp_path / 'ledger.sqlite')
    originals = {path.name: BROKEN}
    for suffix in ('-journal', '-wal', '-shm'):
        name = path.name + suffix
        originals[name] = ('synthetic bytes ' + suffix).encode()
        (tmp_path / name).write_bytes(originals[name])
    # Isolate the archive boundary from SQLite's handling of arbitrary synthetic
    # sidecars. The real SQLite corruption detector is exercised above.
    check = ledger_recovery.check
    monkeypatch.setattr(ledger_recovery, 'check',
        lambda p: False if p.exists() and p.read_bytes() == BROKEN else check(p))
    real, steps = Path.rename, []
    class Interrupted(BaseException):
        pass
    def move(source, destination):
        if len(steps) == cut:
            raise Interrupted()
        steps.append('before')
        result = real(source, destination)
        if len(steps) == cut:
            raise Interrupted()
        steps.append('after')
        return result
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'rename', move)
        with pytest.raises(Interrupted):
            Ledger(path)
    for name, raw in originals.items():
        copies = [p for p in tmp_path.rglob(name) if p.read_bytes() == raw]
        assert len(copies) == 1, (name, copies)
    with Ledger(path) as book:
        assert book.catching_up()
    assert path.read_bytes().startswith(b'SQLite format 3\x00')
    for name, raw in originals.items():
        assert sum(p.read_bytes() == raw for p in (tmp_path / 'evidence').rglob(name)) == 1


def test_recovery_alarm_keeps_its_path_when_status_line_overflows(tmp_path):
    path = corrupt(tmp_path / 'ledger.sqlite')
    with Ledger(path):
        pass
    alarm, = ledger_recovery.alarms(tmp_path)
    facts = watch.Facts('ok', 1, 0, 0, pending=['synthetic long wait ' * 30], ledger_recovery=[alarm])
    assert alarm in facts.line()


@pytest.mark.parametrize('damaged', [False, True], ids=['healthy-hot-journal', 'corrupt-hot-journal'])
def test_hot_journal_rolls_back_and_keeps_original_bytes_only_when_corrupt(tmp_path, damaged):
    path = tmp_path / 'ledger.sqlite'
    with Ledger(path) as book:
        book.db.execute('CREATE TABLE synthetic_payload (text TEXT)')
        book.db.executemany('INSERT INTO synthetic_payload VALUES (?)', [('original-' + str(i) + 'x' * 9000,) for i in range(64)])
        book.db.execute('CREATE TABLE synthetic_unrelated (text TEXT)')
        book.db.execute("INSERT INTO synthetic_unrelated VALUES ('unmodified by the later transaction')")
        book.db.commit()
        page, = book.db.execute("SELECT rootpage FROM sqlite_master WHERE name='synthetic_unrelated'").fetchone()
        size, = book.db.execute('PRAGMA page_size').fetchone()
    code = """import os,sqlite3,sys
book=sqlite3.connect(sys.argv[1])
book.execute('PRAGMA synchronous=OFF')
book.execute('PRAGMA cache_size=1')
book.execute('BEGIN IMMEDIATE')
book.execute('UPDATE synthetic_payload SET text=?', ('uncommitted-'+'y'*9000,))
os._exit(0)
"""
    subprocess.run([sys.executable, '-I', '-c', code, str(path)], check=True)
    assert Path(str(path) + '-journal').exists(), 'the process left a real spilled rollback journal'
    if damaged:
        data = bytearray(path.read_bytes())
        data[(page - 1) * size] = 255  # outside the pages the hot journal can restore
        path.write_bytes(data)
    originals = {p.name: p.read_bytes() for p in [path, Path(str(path) + '-journal')]}
    with pytest.raises(sqlite3.DatabaseError) as failure:
        ledger_recovery.check(path)
    assert failure.value.sqlite_errorcode == sqlite3.SQLITE_READONLY_ROLLBACK
    with Ledger(path) as book:
        if not damaged:
            rows = book.db.execute('SELECT text FROM synthetic_payload').fetchall()
            assert len(rows) == 64 and all(row[0].startswith('original-') for row in rows)
        else:
            assert book.catching_up() and book.rollup('seat') == []
    if damaged:
        folder, = ledger_recovery.pending(tmp_path)
        assert {p.name: p.read_bytes() for p in (folder / 'pre-rollback').iterdir()} == originals
        assert (folder / path.name).is_file()
        assert ledger_recovery.alarms(tmp_path)
    else:
        assert ledger_recovery.pending(tmp_path) == []
        assert list((tmp_path / 'evidence').iterdir()) == []
