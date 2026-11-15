"""Warm model passes reuse only settled, unchanged native record readings."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import model_mismatch, post
from quarterdeck.service import Service


def setup_pass(tmp_path, monkeypatch):
    record = tmp_path / 'record.jsonl'
    record.write_text('sample-a')
    pointer = [record]
    reads = []

    def read(path):
        reads.append(path)
        return path.read_text().strip()

    harness = SimpleNamespace(name='fake', record_model=read,
                              model_identity=lambda value, **kwargs: value)
    launch = SimpleNamespace(harness='fake', version='1')
    pane = SimpleNamespace(id='%1', pid=123, dead=False)
    seat = SimpleNamespace(name='reader', harness=harness,
                           spec=SimpleNamespace(model='sample-a', model_mode='named'),
                           ship=SimpleNamespace(runtime=tmp_path / 'runtime'),
                           launched=lambda: launch, pane=lambda: pane, record=lambda: pointer[0])
    service = object.__new__(Service)
    service.resolved_seats = lambda: [seat]
    service.systems_recipients = lambda seat: ['systems']
    service.store = post.Store(seat.ship.runtime)
    service.log = lambda *args, **kwargs: pytest.fail(str(kwargs))
    monkeypatch.setattr(model_mismatch, 'Door', lambda seat: SimpleNamespace(screen=lambda: None))
    now = [max(record.stat().st_mtime_ns, record.stat().st_ctime_ns)
           + post.POST_TIMESTAMP_TICK_NS + 1_000_000_000]
    monkeypatch.setattr(model_mismatch, 'time', SimpleNamespace(time_ns=lambda: now[0]))
    return service, seat, pointer, reads, now


def test_unchanged_record_read_once_over_several_service_passes(tmp_path, monkeypatch):
    service, seat, pointer, reads, _ = setup_pass(tmp_path, monkeypatch)
    lstats = []
    original = Path.lstat

    def lstat(path, *args, **kwargs):
        if path == pointer[0]:
            lstats.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'lstat', lstat)
    for _ in range(4):
        service.model_differences()
    assert reads == [pointer[0]]
    assert lstats == [pointer[0]] * 4
    assert service.store.all() == []


@pytest.mark.parametrize('change', ['append', 'replace', 'same-size-restored-mtime', 'pointer'])
def test_changed_record_read_afresh_and_fixed_notice_is_not_repeated(tmp_path, monkeypatch, change):
    service, seat, pointer, reads, _ = setup_pass(tmp_path, monkeypatch)
    service.model_differences()
    record = pointer[0]
    before = record.stat()
    if change == 'append':
        with record.open('a') as stream:
            stream.write('-changed')
        model = 'sample-a-changed'
    else:
        model = 'sample-b'
        if change in ('replace', 'pointer'):
            replacement = tmp_path / 'replacement.jsonl'
            replacement.write_text(model)
            if change == 'replace':
                os.replace(replacement, record)
                assert record.stat().st_ino != before.st_ino
            else:
                pointer[0] = replacement
        else:
            record.write_text(model)
            os.utime(record, ns=(before.st_atime_ns, before.st_mtime_ns))
            assert record.stat().st_size == before.st_size
            assert record.stat().st_ctime_ns != before.st_ctime_ns
    service.model_differences()
    assert reads == [record, pointer[0]]
    messages = service.store.all()
    assert len(messages) == 1
    assert messages[0].body == f'reader: model mismatch; ship declares sample-a, program reports {model} in its record'
    for _ in range(3):
        service.model_differences()
    assert reads == [record, pointer[0]]
    assert [m.to_json() for m in service.store.all()] == [m.to_json() for m in messages]


def test_removed_record_discards_reading_and_recreated_record_is_read(tmp_path, monkeypatch):
    service, seat, pointer, reads, _ = setup_pass(tmp_path, monkeypatch)
    service.model_differences()
    record = pointer[0]
    record.unlink()
    service.model_differences()
    assert not service.model_records._readings
    record.write_text('sample-b')
    service.model_differences()
    assert reads == [record, record]
    assert len(service.store.all()) == 1


@pytest.mark.parametrize('age', [0, post.POST_TIMESTAMP_TICK_NS])
def test_read_within_timestamp_tick_must_be_read_again_even_after_waiting(tmp_path, monkeypatch, age):
    service, seat, pointer, reads, now = setup_pass(tmp_path, monkeypatch)
    record = pointer[0]
    stamp = record.lstat()
    now[0] = max(stamp.st_mtime_ns, stamp.st_ctime_ns) + age
    # A coarse filesystem can hide a same-size change inside a timestamp tick.
    original = Path.lstat
    monkeypatch.setattr(Path, 'lstat', lambda p, **kw: stamp if p == record else original(p, **kw))
    service.model_differences()
    record.write_text('sample-b')
    now[0] = max(stamp.st_mtime_ns, stamp.st_ctime_ns) + post.POST_TIMESTAMP_TICK_NS + 1
    service.model_differences()
    service.model_differences()
    assert reads == [record, record]
    assert len(service.store.all()) == 1


@pytest.mark.parametrize('failure', ['exception', 'unknown'])
def test_failed_or_unknown_record_reading_is_retried(tmp_path, monkeypatch, failure):
    service, seat, pointer, reads, _ = setup_pass(tmp_path, monkeypatch)
    real_read = seat.harness.record_model

    def fail(path):
        reads.append(path)
        if failure == 'exception':
            raise OSError('temporary read failure')
        return None

    seat.harness.record_model = fail
    service.model_differences()
    assert not service.model_records._readings
    seat.harness.record_model = real_read
    service.model_differences()
    service.model_differences()
    assert reads == [pointer[0], pointer[0]]


def test_missing_pointer_discards_previous_reading(tmp_path, monkeypatch):
    service, seat, pointer, reads, _ = setup_pass(tmp_path, monkeypatch)
    record = pointer[0]
    service.model_differences()
    pointer[0] = None
    service.model_differences()
    assert not service.model_records._readings
    pointer[0] = record
    service.model_differences()
    assert reads == [record, record]


def test_cached_record_still_checks_launch_and_screen_each_pass(tmp_path, monkeypatch):
    service, seat, pointer, reads, _ = setup_pass(tmp_path, monkeypatch)
    service.model_differences()
    seat.spec.model = 'sample-b'
    service.model_differences()
    assert len(service.store.all()) == 1
    seat.pane = lambda: SimpleNamespace(id='%1', pid=123, dead=True)
    assert model_mismatch.observe(seat, records=service.model_records).status == 'unknown'
    assert reads == [pointer[0]]


@pytest.mark.parametrize('change', ['append', 'replace', 'remove', 'create'])
def test_wal_change_reads_model_afresh_while_main_record_is_unchanged(tmp_path, monkeypatch, change):
    service, seat, pointer, reads, _ = setup_pass(tmp_path, monkeypatch)
    record = pointer[0]
    wal = Path(f'{record}-wal')
    if change != 'create':
        wal.write_text('sample-a')

    def read(path):
        reads.append(path)
        return wal.read_text() if wal.exists() else path.read_text()

    seat.harness.record_model = read
    before = record.stat()
    service.model_differences()
    service.model_differences()
    assert reads == [record]
    if change == 'remove':
        wal.unlink()
    elif change == 'replace':
        replacement = tmp_path / 'replacement-wal'
        replacement.write_text('sample-b')
        os.replace(replacement, wal)
    elif change == 'append':
        with wal.open('a') as stream:
            stream.write('-changed')
    else:
        wal.write_text('sample-b')
    service.model_differences()
    service.model_differences()
    assert reads == [record, record]
    after = record.stat()
    assert (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) == (
        before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)


@pytest.mark.parametrize('age', [0, post.POST_TIMESTAMP_TICK_NS])
def test_recent_wal_prevents_reuse_of_an_old_main_record_reading(tmp_path, monkeypatch, age):
    service, seat, pointer, reads, now = setup_pass(tmp_path, monkeypatch)
    record = pointer[0]
    wal = Path(f'{record}-wal')
    wal.write_text('sample-a')
    old_info = record.lstat()
    wal_info = SimpleNamespace(st_mode=old_info.st_mode, st_dev=old_info.st_dev,
                              st_ino=old_info.st_ino + 1, st_size=8,
                              st_mtime_ns=now[0], st_ctime_ns=now[0])
    original = Path.lstat
    monkeypatch.setattr(Path, 'lstat', lambda p, **kw: wal_info if p == wal else original(p, **kw))
    now[0] += age
    service.model_differences()
    now[0] = max(wal_info.st_mtime_ns, wal_info.st_ctime_ns) + post.POST_TIMESTAMP_TICK_NS + 1
    service.model_differences()
    service.model_differences()
    assert reads == [record, record]


def test_linked_record_keeps_native_reading_behavior_without_caching(tmp_path, monkeypatch):
    service, seat, pointer, reads, _ = setup_pass(tmp_path, monkeypatch)
    record = pointer[0]
    target = tmp_path / 'native.jsonl'
    record.rename(target)
    record.symlink_to(target)
    service.model_differences()
    target.write_text('sample-b')
    service.model_differences()
    assert reads == [record, record]
    assert not service.model_records._readings
    assert len(service.store.all()) == 1


def test_rollback_journal_discards_cached_model_until_native_reader_can_read_again(tmp_path, monkeypatch):
    import sqlite3
    from quarterdeck.harness.sqlite_snapshot import snapshot

    service, seat, pointer, reads, _ = setup_pass(tmp_path, monkeypatch)
    record = pointer[0]
    record.unlink()
    with sqlite3.connect(record) as database:
        database.execute('CREATE TABLE model (value TEXT)')
        database.execute("INSERT INTO model VALUES ('sample-a')")

    def read(path):
        reads.append(path)
        _, data = snapshot(path)
        with sqlite3.connect(':memory:') as database:
            database.deserialize(data)
            return database.execute('SELECT value FROM model').fetchone()[0]

    seat.harness.record_model = read
    service.model_differences()
    service.model_differences()
    assert reads == [record]
    journal = Path(f'{record}-journal')
    journal.write_bytes(b'rollback in progress')
    service.model_differences()
    service.model_differences()
    assert reads == [record] * 3
    assert not service.model_records._readings
    journal.unlink()
    service.model_differences()
    service.model_differences()
    assert reads == [record] * 4
