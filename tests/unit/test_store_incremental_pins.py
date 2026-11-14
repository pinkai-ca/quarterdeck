"""The Store's per-file cache never serves a stale or outside byte (La'an's probe of the incremental refresh, pinned).

The cache is the one place that skips the guarded read, so every way a store name can change under a
warm, certified cache is tried here: what the name points at, what kind of file it is, its bytes, its
import and forward records, the ship file, and the clock. Each case changes the name so that the fields
the cache does not look at stay the same, and each fails when the identity key or the context is
loosened (the change's report says how that was checked).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import os
import signal
import time

import pytest

from quarterdeck import fleet_post, post
from quarterdeck.ship import ShipError

AGE = 10_000_000_000  # the clock stands ten seconds past every file's timestamps, so a reading certifies


@pytest.fixture
def warm(tmp_path, monkeypatch):
    """A ship's store of three orders, read twice so that every name is certified, and a count of the reads after."""
    ship = tmp_path / 'probe'
    ship.mkdir()
    (ship / 'ship.toml').write_text('')
    runtime = ship / 'runtime'
    store = post.Store(runtime)
    store.dir.mkdir(parents=True)
    messages = [post.Message(f'ord-20260101-0000-source-{i:04x}', 'order', 'source', 'worker',
                             datetime(2026, 1, 1, tzinfo=timezone.utc), f'body {i}') for i in range(3)]
    for message in messages:
        (store.dir / f'{message.id}.json').write_text(message.to_json())
    clock = [time.time_ns() + AGE]
    monkeypatch.setattr(post.time, 'time_ns', lambda: clock[0])
    store.all()
    store._cache_at = None
    assert store.all() == messages
    assert len(store._validated) == 3 and all(entry[3] is not None for entry in store._validated.values()), \
        'the fixture must start from a certified cache, or no case proves anything'
    original = post.post_dir.read_text
    reads = []

    def read(path, *args, **kwargs):
        if path.parent == store.dir:
            reads.append(path.name)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(post.post_dir, 'read_text', read)
    store._cache_at = None  # every case asks for an ordinary refresh, as a later pass would
    return tmp_path, runtime, store, messages, reads, clock


def keep_times(path, stat):
    """Put back a name's own access and modification times, never following a link."""
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns), follow_symlinks=False)


def bodies(store):
    return sorted(message.body for message in store.all())


def test_the_warm_cache_reads_nothing_when_nothing_changed(warm):
    _, _, store, _, reads, _ = warm
    assert bodies(store) == ['body 0', 'body 1', 'body 2'] and reads == []


def test_a_link_to_an_outside_file_is_held_and_its_bytes_never_served(warm):
    base, _, store, messages, _, _ = warm
    path = store.dir / f'{messages[0].id}.json'
    before = path.lstat()
    secret = base / 'secret.json'
    secret.write_text(replace(messages[0], body='SECRET').to_json())
    keep_times(secret, before)
    path.unlink()
    path.symlink_to(secret)
    keep_times(path, before)  # the link's own times are the file's it replaced
    for _ in range(2):  # the second pass, too, holds rather than serving what the first one left
        store._cache_at = None
        with pytest.raises(ShipError, match='post link held'):
            store.all()


def test_a_fifo_is_dropped_without_waiting_on_it(warm):
    _, _, store, messages, _, _ = warm
    path = store.dir / f'{messages[0].id}.json'
    before = path.lstat()
    path.unlink()
    os.mkfifo(path)
    keep_times(path, before)
    previous = signal.signal(signal.SIGALRM, lambda *_: pytest.fail('the refresh waited on a FIFO'))
    signal.alarm(5)
    try:
        assert bodies(store) == ['body 1', 'body 2']
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    assert f'{messages[0].id}.json' not in store._validated


def test_a_hard_link_to_another_file_serves_the_new_bytes(warm):
    base, _, store, messages, reads, _ = warm
    path = store.dir / f'{messages[0].id}.json'
    before = path.lstat()
    other = base / 'other.json'
    other.write_text(replace(messages[0], body='body H').to_json())  # the same length as 'body 0'
    keep_times(other, before)
    path.unlink()
    os.link(other, path)
    after = path.lstat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    assert bodies(store) == ['body 1', 'body 2', 'body H']
    assert sorted(reads) == sorted(f'{m.id}.json' for m in messages), 'one change re-reads every name'


def test_a_same_size_rewrite_with_its_mtime_restored_serves_the_new_bytes_by_ctime(warm):
    _, _, store, messages, reads, _ = warm
    path = store.dir / f'{messages[0].id}.json'
    before = path.lstat()
    time.sleep(0.01)  # the ctime the rewrite leaves must differ from the one the cache holds
    path.write_text(replace(messages[0], body='body X').to_json())
    keep_times(path, before)
    after = path.lstat()
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (before.st_ino, before.st_size, before.st_mtime_ns)
    assert after.st_ctime_ns != before.st_ctime_ns
    assert bodies(store) == ['body 1', 'body 2', 'body X']
    assert len(reads) == 3


def test_an_import_record_added_with_a_wrong_sha_is_refused(warm):
    _, runtime, store, messages, _, _ = warm
    imports = runtime / 'post' / 'imports'
    imports.mkdir(parents=True)
    (imports / f'{messages[0].id}.json').write_text(
        json.dumps(dict(origin='probe', sha256='0' * 64, context=False)))
    with pytest.raises(ShipError, match='imported post differs from its immutable source'):
        store.all()


def test_a_forward_record_added_projects_the_message_again(warm):
    _, runtime, store, messages, _, _ = warm
    forwarded = runtime / 'post' / 'forwarded'
    forwarded.mkdir(parents=True)
    data = (store.dir / f'{messages[0].id}.json').read_bytes()
    (forwarded / f'{messages[0].id}.json').write_text(json.dumps({
        'format': 1, 'id': messages[0].id, 'sha256': fleet_post.digest(data),
        'from': 'worker@probe', 'to': 'other@elsewhere', 'via': ['worker@probe', 'other@elsewhere']}))
    found = {message.id: message.to for message in store.all()}
    assert found == {messages[0].id: 'other@elsewhere', messages[1].id: 'worker', messages[2].id: 'worker'}


def test_a_changed_ship_file_reads_every_name_again(warm):
    base, runtime, store, messages, reads, _ = warm
    file = runtime.parent / 'ship.toml'
    file.write_text('[forward]\nworker = "other@elsewhere"\n')
    assert bodies(store) == ['body 0', 'body 1', 'body 2']
    assert sorted(reads) == sorted(f'{m.id}.json' for m in messages)
    assert {message.to for message in store.all()} == {'other@elsewhere'}, 'open orders follow the new declaration'


def test_a_clock_moved_back_reads_every_name_again(warm):
    _, _, store, messages, reads, clock = warm
    clock[0] -= 60_000_000_000
    assert bodies(store) == ['body 0', 'body 1', 'body 2']
    assert sorted(reads) == sorted(f'{m.id}.json' for m in messages)
