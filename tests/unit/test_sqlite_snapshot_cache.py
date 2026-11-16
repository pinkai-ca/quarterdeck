"""OpenCode's database is snapshotted once per change of the database and its -wal. An injected clock, never wall time."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from quarterdeck.harness import opencode_record, record_file, sqlite_snapshot
from quarterdeck.harness.base import RecordError, record_fingerprint


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    sqlite_snapshot.forget()
    taken = []
    original = sqlite_snapshot.snapshot

    def counted(record, bound=record_file.DATABASE_BOUND):
        taken.append(Path(record))
        return original(record, bound)

    monkeypatch.setattr(sqlite_snapshot, 'snapshot', counted)
    yield taken
    sqlite_snapshot.forget()


def database(path: Path, *, wal: bool = True) -> sqlite3.Connection:
    """An OpenCode-shaped database: session, message and part, one of each, left open so its -wal stays."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    if wal:
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('PRAGMA wal_autocheckpoint=0')
    con.execute('CREATE TABLE session (id TEXT, time_created INT, time_updated INT, parent_id TEXT)')
    con.execute('CREATE TABLE message (id TEXT, session_id TEXT, time_created INT, time_updated INT, data TEXT)')
    con.execute('CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, time_created INT, time_updated INT, '
                'data TEXT)')
    con.execute("INSERT INTO session VALUES ('root', 1, 1, NULL)")
    con.execute("INSERT INTO message VALUES ('m1', 'root', 2, 2, '{\"role\": \"user\"}')")
    con.execute("INSERT INTO part VALUES ('p1', 'm1', 'root', 3, 3, '{\"type\": \"text\", \"text\": \"hi\"}')")
    con.commit()
    return con


def newest(record: Path) -> int:
    """The newest mtime or ctime of the database and its -wal."""
    return max((max(info[3], info[5]) for info in record_fingerprint(record) if info[0]), default=0)


def clock(monkeypatch, at):
    monkeypatch.setattr(sqlite_snapshot, '_now', lambda: at())


def settled(monkeypatch, record):
    """A clock always past the tick of the files as they stand now and after any later write."""
    clock(monkeypatch, lambda: newest(record) + sqlite_snapshot.TICK_NS + 1)


def test_an_unchanged_database_is_snapshotted_once(tmp_path, monkeypatch, fresh_cache):
    record = tmp_path / 'data-001' / 'opencode' / 'opencode.db'
    con = database(record)
    settled(monkeypatch, record)
    first = sqlite_snapshot.cached(record)
    second = sqlite_snapshot.cached(record)
    assert fresh_cache == [record]
    assert first[0] == second[0] and first[1] == second[1] and first[2] is second[2] is not None
    # The rows a reader builds come from the kept snapshot; the queried rows are kept beside it, unchanged.
    rows = opencode_record._Rows(record)
    again = opencode_record._Rows(record)
    assert fresh_cache == [record]
    assert rows.sessions == again.sessions == [('root', 1, 1, None)]
    assert rows.messages == again.messages and rows.parts == again.parts
    rows.messages[0][4]['role'] = 'changed by one caller'  # never reaches the next
    rows.sessions.append(('mine', 0, 0, None))
    assert opencode_record._Rows(record).messages[0][4] == {'role': 'user'}
    assert opencode_record._Rows(record).sessions == [('root', 1, 1, None)]
    con.close()


@pytest.mark.parametrize('change', ['wal', 'database', 'wal-appears', 'wal-goes'])
def test_a_write_to_the_database_or_its_wal_takes_a_fresh_snapshot(tmp_path, monkeypatch, fresh_cache, change):
    record = tmp_path / 'data-001' / 'opencode' / 'opencode.db'
    con = database(record, wal=change != 'wal-appears')
    settled(monkeypatch, record)
    before = opencode_record._Rows(record)
    if change == 'wal':  # a committed write goes to the log
        con.execute("INSERT INTO message VALUES ('m2', 'root', 4, 4, '{\"role\": \"assistant\"}')")
        con.commit()
    elif change == 'database':  # the log checkpointed into the database file
        con.execute("INSERT INTO message VALUES ('m2', 'root', 4, 4, '{\"role\": \"assistant\"}')")
        con.commit()
        con.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    elif change == 'wal-appears':
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('PRAGMA wal_autocheckpoint=0')
        con.execute("INSERT INTO message VALUES ('m2', 'root', 4, 4, '{\"role\": \"assistant\"}')")
        con.commit()
        assert Path(f'{record}-wal').exists()
    else:
        con.close()  # the last connection checkpoints and removes its log
        assert not Path(f'{record}-wal').exists()
    after = opencode_record._Rows(record)
    assert fresh_cache == [record, record], 'a changed stamp is a fresh snapshot'
    if change != 'wal-goes':
        assert [row[0] for row in after.messages] == ['m1', 'm2'] != [row[0] for row in before.messages]
    opencode_record._Rows(record)
    assert fresh_cache == [record, record], 'and that one is kept in its turn'
    con.close()  # the last connection's close checkpoints and removes the log: a change of its own


@pytest.mark.parametrize('age', [0, sqlite_snapshot.TICK_NS - 1, sqlite_snapshot.TICK_NS])
def test_a_stamp_inside_the_tick_is_never_kept(tmp_path, monkeypatch, fresh_cache, age):
    record = tmp_path / 'data-001' / 'opencode' / 'opencode.db'
    con = database(record)
    clock(monkeypatch, lambda: newest(record) + age)  # the read begins within the tick of the newest stamp
    assert sqlite_snapshot.cached(record)[2] is None
    sqlite_snapshot.cached(record)
    opencode_record._Rows(record)
    assert fresh_cache == [record, record, record]
    settled(monkeypatch, record)  # once past the tick, the next read is kept
    sqlite_snapshot.cached(record)
    sqlite_snapshot.cached(record)
    assert len(fresh_cache) == 4
    con.close()


def test_a_removed_database_drops_its_entry(tmp_path, monkeypatch, fresh_cache):
    record = tmp_path / 'data-001' / 'opencode' / 'opencode.db'
    con = database(record)
    settled(monkeypatch, record)
    sqlite_snapshot.cached(record)
    assert str(record) in sqlite_snapshot._entries
    con.close()
    record.unlink()
    with pytest.raises(RecordError, match='does not exist'):
        sqlite_snapshot.cached(record)
    assert str(record) not in sqlite_snapshot._entries
    with pytest.raises(RecordError):
        opencode_record._Rows(record)


def test_another_bound_or_an_unreadable_read_is_never_answered_from_the_cache(tmp_path, monkeypatch, fresh_cache):
    record = tmp_path / 'data-001' / 'opencode' / 'opencode.db'
    con = database(record)
    settled(monkeypatch, record)
    sqlite_snapshot.cached(record)
    with pytest.raises(record_file.PastBound):
        sqlite_snapshot.cached(record, 16)  # what the kept read could carry is no answer for a smaller bound
    assert str(record) not in sqlite_snapshot._entries
    sqlite_snapshot.cached(record)
    assert fresh_cache == [record, record, record]
    con.close()


def test_the_cache_is_bounded_by_bytes_and_entries_least_recent_first(tmp_path, monkeypatch, fresh_cache):
    records = [tmp_path / f'data-{n:03d}' / 'opencode' / 'opencode.db' for n in range(3)]
    connections = [database(record) for record in records]
    clock(monkeypatch, lambda: max(newest(record) for record in records) + sqlite_snapshot.TICK_NS + 1)
    size = len(sqlite_snapshot.cached(records[0])[1])
    monkeypatch.setattr(sqlite_snapshot, 'CACHE_BYTES', 2 * size)
    sqlite_snapshot.cached(records[1])
    sqlite_snapshot.cached(records[0])  # most recently used again
    sqlite_snapshot.cached(records[2])  # the third over the bound: records[1], least recent, goes
    assert list(sqlite_snapshot._entries) == [str(records[0]), str(records[2])]
    monkeypatch.setattr(sqlite_snapshot, 'CACHE_ENTRIES', 1)
    sqlite_snapshot.cached(records[1])
    assert list(sqlite_snapshot._entries) == [str(records[1])]
    for con in connections:
        con.close()


def test_every_opencode_reading_goes_through_the_cache(tmp_path, monkeypatch, fresh_cache):
    """The readers that build rows: first_timestamp, session_id, inputs, and the reader's state and turns."""
    record = tmp_path / 'data-001' / 'opencode' / 'opencode.db'
    con = database(record)
    settled(monkeypatch, record)
    opencode_record.first_timestamp(record)
    opencode_record.session_id(record)
    opencode_record.inputs(record)
    opencode_record.state(record)
    opencode_record.turns(record)
    assert fresh_cache == [record]
    con.close()
    assert not os.path.exists(f'{record}-wal')


def test_kept_rows_count_toward_the_bound_and_go_with_their_snapshot(tmp_path, monkeypatch, fresh_cache):
    records = [tmp_path / f'data-{n:03d}' / 'opencode' / 'opencode.db' for n in range(2)]
    connections = [database(record) for record in records]
    clock(monkeypatch, lambda: max(newest(record) for record in records) + sqlite_snapshot.TICK_NS + 1)
    opencode_record._Rows(records[0])
    entry = sqlite_snapshot._entries[str(records[0])]
    rows = entry.derived(('opencode-rows', False))
    assert rows is not None and entry.derived_size == opencode_record._measured(rows) > 0
    assert entry.size() == len(entry.merged) + entry.derived_size
    # A bound that holds both snapshots' bytes, but not both with the first one's rows: the rows push it over.
    sqlite_snapshot.forget()
    opencode_record._Rows(records[0])
    first = sqlite_snapshot._entries[str(records[0])]
    snapshot_only = len(sqlite_snapshot.cached(records[1])[1])
    monkeypatch.setattr(sqlite_snapshot, 'CACHE_BYTES', len(first.merged) + snapshot_only + first.derived_size - 1)
    sqlite_snapshot._entries.move_to_end(str(records[0]), last=False)  # the first, least recently used
    opencode_record._Rows(records[1])  # its rows are counted too: the first entry, rows and all, is evicted
    assert list(sqlite_snapshot._entries) == [str(records[1])]
    assert first.derived(('opencode-rows', False)) is not None  # held only by what still names it, not the cache
    for con in connections:
        con.close()


def test_one_derived_set_per_entry_the_latest(tmp_path, monkeypatch, fresh_cache):
    record = tmp_path / 'data-001' / 'opencode' / 'opencode.db'
    con = database(record)
    settled(monkeypatch, record)
    opencode_record._Rows(record)
    entry = sqlite_snapshot._entries[str(record)]
    plain = entry.derived_size
    assert entry.derived(('opencode-rows', False)) is not None
    opencode_record._Rows(record, resume_metadata=True)  # another reading replaces the first, never beside it
    assert entry.derived(('opencode-rows', False)) is None
    assert entry.derived(('opencode-rows', True)) is not None and entry.derived_size >= plain
    assert entry.size() == len(entry.merged) + entry.derived_size
    assert fresh_cache == [record]
    again = opencode_record._Rows(record)  # and back: the same snapshot, rows queried once more
    assert entry.derived(('opencode-rows', False)) is not None and entry.derived(('opencode-rows', True)) is None
    assert again.sessions == [('root', 1, 1, None)] and fresh_cache == [record]
    con.close()
