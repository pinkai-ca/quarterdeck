"""Native SQLite generates every WAL here; no application decoder constructs the fixtures."""
import sqlite3
from pathlib import Path

import pytest

from quarterdeck.harness.synthetic_record import AntigravityShape
from quarterdeck.harness import antigravity, antigravity_wire as wire
from quarterdeck.harness.base import RecordError, record_fingerprint


def contents(db, wal):
    con = sqlite3.connect(':memory:')
    try:
        con.deserialize(wire._apply_wal('synthetic', db, wal))
        assert con.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        return con.execute('SELECT count(*) FROM samples').fetchone()[0]
    finally:
        con.close()


def test_snapshot_applies_only_committed_frames_and_handles_checkpoint_reset(tmp_path):
    path = tmp_path / 'native.db'
    con = sqlite3.connect(path)
    try:
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('PRAGMA wal_autocheckpoint=0')
        con.execute('PRAGMA cache_size=1')
        con.execute('CREATE TABLE samples (data BLOB)')
        con.commit()
        con.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        base = path.read_bytes()
        wal = Path(f'{path}-wal')
        con.execute('INSERT INTO samples VALUES (zeroblob(9000))')
        con.commit()
        committed = wal.read_bytes()
        assert contents(base, committed) == 1
        # Native SQLite spills uncommitted pages with a very small page cache.
        con.executemany('INSERT INTO samples VALUES (zeroblob(9000))', [()] * 20)
        pending = wal.read_bytes()
        assert len(pending) > len(committed)
        assert contents(base, pending) == 1
        assert contents(base, pending[:-15]) == 1, 'an incomplete tail cannot commit a transaction'
        con.commit()
        finished = wal.read_bytes()
        assert contents(base, finished) == 21
        assert contents(base, finished[:-1]) == 1, 'missing final commit frame leaves prior transaction visible'
        con.execute('PRAGMA wal_checkpoint(RESTART)')
        con.execute('INSERT INTO samples VALUES (zeroblob(100))')
        con.commit()
        assert contents(path.read_bytes(), wal.read_bytes()) == 22, 'ignore stale frames after a WAL reset'
        con.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        assert contents(path.read_bytes(), wal.read_bytes()) == 22
    finally:
        con.close()
    assert not Path(f'{path}-wal').exists()
    assert contents(path.read_bytes(), None) == 22


def test_invalid_wal_header_and_same_generation_checksum_failure_are_unknown(tmp_path):
    path = tmp_path / 'native.db'
    con = sqlite3.connect(path)
    try:
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('CREATE TABLE samples (n INTEGER)')
        con.commit()
        db, wal = path.read_bytes(), Path(f'{path}-wal').read_bytes()
        for offset in (0, 12, 32 + 24 + 50):
            corrupt = bytearray(wal)
            corrupt[offset] ^= 1
            with pytest.raises(RecordError, match='invalid WAL'):
                contents(db, corrupt)
        with pytest.raises(RecordError, match='truncated WAL'):
            contents(db, wal[:20])
    finally:
        con.close()


def test_concurrent_changes_are_retried_and_failures_are_not_cached(tmp_path, monkeypatch):
    rec = AntigravityShape(tmp_path / 'home', tmp_path, 'synthetic', 'fake-model')
    rec.begin('question', 'fake-model', 'high')
    rec.end('answer')
    from quarterdeck.harness import sqlite_snapshot  # the snapshot and its retry live there, shared with OpenCode

    calls = []
    real = sqlite_snapshot.record_fingerprint

    def changing(path):
        calls.append(path)
        fp = real(path)
        return (*fp, len(calls))

    monkeypatch.setattr(sqlite_snapshot, 'record_fingerprint', changing)
    with pytest.raises(RecordError, match='changed concurrently'):
        antigravity.read(rec.path)
    assert len(calls) == 6 and rec.path not in antigravity._CACHE
    monkeypatch.setattr(sqlite_snapshot, 'record_fingerprint', real)
    assert antigravity.state(rec.path).turns == 1


def test_source_and_sidecars_are_unchanged_and_wal_recreation_invalidates_cache(tmp_path):
    rec = AntigravityShape(tmp_path / 'home', tmp_path, 'synthetic', 'fake-model')
    rec.begin('question', 'fake-model', 'high')
    rec.end('answer')
    writer = sqlite3.connect(rec.path)
    try:
        writer.execute('UPDATE steps SET status=0 WHERE idx=0')
        writer.commit()
        paths = [rec.path, Path(f'{rec.path}-wal'), Path(f'{rec.path}-shm')]
        before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths]
        assert antigravity.state(rec.path).turns is None
        assert [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths] == before  # law: one-folder
        old_fp = record_fingerprint(rec.path)
    finally:
        writer.close()
    assert record_fingerprint(rec.path) != old_fp
    assert antigravity.state(rec.path).turns is None
    writer = sqlite3.connect(rec.path)
    try:
        writer.execute('UPDATE steps SET status=3 WHERE idx=0')
        writer.commit()
        assert antigravity.state(rec.path).turns == 1
    finally:
        writer.close()
