"""The service keeps a native record's reading across its status collections while the record is unchanged, and reads
a changed one again. The memo holds only what the last collection read. Synthetic records; a stand-in reader."""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import watch
from quarterdeck.harness import reading_scope


class Reader:
    """A stand-in adapter: counts its whole reads of a record."""
    loads = 0

    @classmethod
    def read(cls, record):
        def load():
            cls.loads += 1
            return SimpleNamespace(size=Path(record).stat().st_size), []
        return reading_scope.read(cls, record, load)


@pytest.fixture(autouse=True)
def reader(monkeypatch):
    Reader.loads = 0
    # The memo is what is tested, not the 2 s rule that keeps a just-written record from a reading's cache.
    monkeypatch.setattr(reading_scope, '_TIMESTAMP_TICK_NS', 0)
    return Reader


def collect(memo, *records):
    with watch.held_readings(memo):
        for record in records:
            Reader.read(record)


def record_at(tmp_path, name='record.jsonl', text='{"a": 1}\n'):
    path = tmp_path / name
    path.write_text(text)
    os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    return path


def test_an_unchanged_record_is_read_once_across_collections(tmp_path):
    memo, record = watch.HeldReadings(), record_at(tmp_path)
    for _ in range(4):
        collect(memo, record)
    assert Reader.loads == 1


@pytest.mark.parametrize('change', ['size', 'mtime', 'inode', 'gone'])
def test_a_changed_record_is_always_read_again(tmp_path, change):
    memo, record = watch.HeldReadings(), record_at(tmp_path)
    collect(memo, record)
    if change == 'size':
        record.write_text('{"a": 1, "b": 2}\n')
        os.utime(record, ns=(1_000_000_000, 1_000_000_000))
    elif change == 'mtime':
        os.utime(record, ns=(2_000_000_000, 2_000_000_000))
    elif change == 'inode':  # replaced whole, same size and same mtime: only the inode tells
        replacement = record_at(tmp_path, 'replacement.jsonl', '{"a": 2}\n')
        os.replace(replacement, record)
        os.utime(record, ns=(1_000_000_000, 1_000_000_000))
    else:
        record.unlink()
        record_at(tmp_path)  # back, as a new file
    collect(memo, record)
    assert Reader.loads == 2


def test_another_path_is_its_own_reading(tmp_path):
    memo = watch.HeldReadings()
    collect(memo, record_at(tmp_path, 'one.jsonl'), record_at(tmp_path, 'two.jsonl'))
    assert Reader.loads == 2


def test_the_memo_holds_only_what_the_last_collection_read(tmp_path):
    memo = watch.HeldReadings()
    one, two = record_at(tmp_path, 'one.jsonl'), record_at(tmp_path, 'two.jsonl')
    collect(memo, one, two)
    assert len(memo) == 2
    collect(memo, one)
    assert [key[1] for key in memo] == [one], 'a record the last collection did not read is let go'


def test_outside_a_held_collection_nothing_is_kept(tmp_path):
    record = record_at(tmp_path)
    for _ in range(2):
        with reading_scope.reading_pass():
            Reader.read(record)
    assert Reader.loads == 2, 'an ordinary reading pass keeps nothing past its end, as before'


def test_the_service_holds_the_memo_around_its_status_publication(monkeypatch):
    from quarterdeck import presentation
    from quarterdeck.service import Service
    svc = Service.__new__(Service)
    svc.ship, svc.tmux, svc.status_framework, svc.store, svc.attempts = object(), object(), None, object(), object()
    seen = []
    def publish(*args, **kwargs):
        seen.append(reading_scope._READING.get())
        return True
    monkeypatch.setattr(presentation, 'publish', publish)
    svc.publish_status()
    svc.publish_status()
    assert seen[0] is not None and seen[0] is seen[1] is svc.status_readings
