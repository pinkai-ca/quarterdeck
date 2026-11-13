"""Equal coarse directory times are not proof that a recent listing is complete."""

from datetime import datetime, timezone
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import post

NS = 1_000_000_000
CHANGE = 100 * NS
WINDOW = 2 * NS


@pytest.fixture
def boundary(tmp_path, monkeypatch):
    store = post.Store(tmp_path)
    store.dir.mkdir(parents=True)
    info = store.dir.stat()
    clock = SimpleNamespace(now=CHANGE, mtime=CHANGE, ctime=CHANGE)
    scans = []
    original_stat, original_scan = Path.stat, os.scandir

    def stat(path, *args, **kwargs):
        if path == store.dir:
            return SimpleNamespace(st_mode=info.st_mode, st_dev=info.st_dev, st_ino=info.st_ino,
                                   st_mtime_ns=clock.mtime, st_ctime_ns=clock.ctime)
        return original_stat(path, *args, **kwargs)

    def scan(path):
        if isinstance(path, (str, os.PathLike)) and Path(path) == store.dir:
            scans.append(clock.now)
        return original_scan(path)

    def publish(name):
        message = post.Message(name, 'order', 'issuer', 'worker',
                               datetime(2032, 1, 1, tzinfo=timezone.utc), 'synthetic')
        store.write(message)  # real atomic publication, only the directory times are simulated
        return message

    monkeypatch.setattr(Path, 'stat', stat)
    monkeypatch.setattr(os, 'scandir', scan)
    monkeypatch.setattr(post.time, 'time_ns', lambda: clock.now)
    return store, clock, scans, publish


@pytest.mark.parametrize('read_during_tick', [False, True])
def test_second_equal_stamp_publish_survives_read_and_age(boundary, read_during_tick):
    store, clock, scans, publish = boundary
    first = publish('ord-first')
    assert store.all() == [first]
    clock.now += NS // 2
    second = publish('ord-second')
    if read_during_tick:
        assert store.all() == [first, second]
    before = len(scans)
    clock.now = CHANGE + WINDOW + 1
    assert store.all() == [first, second]
    assert len(scans) == before + 1, 'an unsafe listing must be refreshed, not merely aged'
    before = len(scans)
    clock.now += 100 * NS
    for _ in range(5):
        assert store.all() == [first, second]
    assert len(scans) == before, 'an old certified directory remains constant-cost'


@pytest.mark.parametrize('offset, expected_scans', [(-1, 2), (0, 2), (1, 1)])
def test_listing_must_start_strictly_after_the_supported_tick(boundary, offset, expected_scans):
    store, clock, scans, publish = boundary
    first = publish('ord-first')
    clock.now = CHANGE + WINDOW + offset
    assert store.all() == store.all() == [first]
    assert len(scans) == expected_scans


def test_future_timestamp_and_observed_clock_rollback_remain_untrusted(boundary):
    store, clock, scans, publish = boundary
    first = publish('ord-first')
    clock.now = CHANGE - NS
    assert store.all() == store.all() == [first]
    assert len(scans) == 2
    clock.now = CHANGE + 5 * NS
    assert store.all() == [first]
    before = len(scans)
    assert store.all() == [first] and len(scans) == before
    clock.now = CHANGE + NS  # roll back into the bucket used by a second publication
    second = publish('ord-second')
    assert store.all() == [first, second]
    clock.now = CHANGE + 10 * NS
    before = len(scans)
    assert store.all() == [first, second] and len(scans) == before + 1


@pytest.mark.parametrize('finish', [CHANGE + NS, CHANGE + 100 * NS])
def test_clock_step_during_listing_cannot_certify_a_racy_snapshot(boundary, monkeypatch, finish):
    store, clock, scans, publish = boundary
    first = publish('ord-first')
    clock.now = CHANGE + (5 * NS if finish < CHANGE + 2 * NS else 0)
    original = Path.read_text
    later = []

    def read(path, *args, **kwargs):
        text = original(path, *args, **kwargs)
        if path.parent == store.dir and not later:
            # Names were already listed. Publish in the coarse bucket, then step
            # the clock before the read finishes; it must not certify this list.
            clock.now = CHANGE + NS // 2
            later.append(publish('ord-second'))
            clock.now = finish
        return text

    monkeypatch.setattr(Path, 'read_text', read)
    assert store.all() == [first]
    clock.now = CHANGE + 200 * NS
    assert store.all() == [first, later[0]]
    assert len(scans) == 2


def test_failed_racy_refresh_never_certifies_the_old_listing(boundary, monkeypatch):
    store, clock, scans, publish = boundary
    first = publish('ord-first')
    assert store.all() == [first]
    clock.now = CHANGE + WINDOW + 1
    with monkeypatch.context() as patch:
        def fail(path):
            raise PermissionError('synthetic failed listing')
        patch.setattr(os, 'scandir', fail)
        with pytest.raises(PermissionError):
            store.all()
    second = publish('ord-second')
    assert store.all() == [first, second]


@pytest.mark.parametrize('field', ['mtime', 'ctime'])
def test_both_directory_times_bound_certification(boundary, field):
    store, clock, scans, publish = boundary
    first = publish('ord-first')
    setattr(clock, field, CHANGE + 5 * NS)
    clock.now = CHANGE + 5 * NS + WINDOW
    assert store.all() == store.all() == [first]
    assert len(scans) == 2
    clock.now += 1
    assert store.all() == store.all() == [first]
    assert len(scans) == 3


def test_clock_rollback_during_stat_invalidates_a_trusted_listing(boundary, monkeypatch):
    store, clock, scans, publish = boundary
    first = publish('ord-first')
    clock.now = CHANGE + 5 * NS
    assert store.all() == [first]
    original = Path.stat
    later = []

    def stat(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == store.dir and not later:
            later.append(None)  # claimed first: the publication below may stat the directory itself
            clock.now = CHANGE + NS
            later[0] = publish('ord-second')
        return info

    monkeypatch.setattr(Path, 'stat', stat)
    clock.now += 5 * NS
    assert store.all() == [first, later[0]]
    assert len(scans) == 2
    clock.now = CHANGE + 100 * NS
    assert store.all() == [first, later[0]]
    assert len(scans) == 3, 'the rollback pass did not certify its listing'
