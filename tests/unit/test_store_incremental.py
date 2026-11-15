"""Ordinary immutable Store refresh and metadata invalidation."""
from dataclasses import replace
from datetime import datetime, timezone
import time

import pytest

from quarterdeck import post


def seed(tmp_path, monkeypatch, *, aged=True):
    runtime = tmp_path / 'runtime'
    store = post.Store(runtime)
    store.dir.mkdir(parents=True)
    messages = [post.Message(f'ord-20260101-0000-source-{i:04x}', 'order', 'source',
                             'worker', datetime(2026, 1, 1, tzinfo=timezone.utc), f'body {i}')
                for i in range(3)]
    for message in messages:
        (store.dir / f'{message.id}.json').write_text(message.to_json())
    clock = [time.time_ns() + (10_000_000_000 if aged else 0)]
    monkeypatch.setattr(post.time, 'time_ns', lambda: clock[0])
    original = post.post_dir.read_text
    reads = []
    def read(path, *args, **kwargs):
        if path.parent == store.dir:
            reads.append(path.name)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(post.post_dir, 'read_text', read)
    assert store.all() == messages
    reads.clear()
    return store, messages, reads, clock


def invalidate(store):
    # Force an ordinary refresh independent of the filesystem timestamp bucket.
    store._cache_at = None


def test_new_message_reads_only_new_name(tmp_path, monkeypatch):
    store, messages, reads, _ = seed(tmp_path, monkeypatch)
    added = replace(messages[-1], id='synthetic-answer', kind='answer',
                    sender='worker', to='source', re=messages[0].id)
    store._write(added)
    assert added in store.all()
    assert reads == [f'{added.id}.json']
    assert store.replies(messages[0]) == [added]


@pytest.mark.parametrize('change', ['replace', 'rewrite', 'remove'])
def test_changed_validated_file_rereads_every_remaining_name(tmp_path, monkeypatch, change):
    store, messages, reads, _ = seed(tmp_path, monkeypatch)
    path = store.dir / f'{messages[0].id}.json'
    updated = replace(messages[0], body='new body')
    if change == 'replace':
        temporary = path.with_suffix('.temporary')
        temporary.write_text(updated.to_json())
        temporary.replace(path)
    elif change == 'rewrite':
        path.write_text(updated.to_json())
    else:
        path.unlink()
    invalidate(store)
    found = store.all()
    assert len(reads) == (2 if change == 'remove' else 3)
    assert (updated in found) == (change != 'remove')


def test_recent_identity_is_read_again_until_certified(tmp_path, monkeypatch):
    store, _, reads, clock = seed(tmp_path, monkeypatch, aged=False)
    invalidate(store)
    store.all()
    assert len(reads) == 3
    clock[0] += 10_000_000_000
    reads.clear()
    invalidate(store)
    store.all()
    assert len(reads) == 3
    reads.clear()
    invalidate(store)
    store.all()
    assert reads == []


def test_malformed_refresh_does_not_retain_a_partial_certificate(tmp_path, monkeypatch):
    store, messages, reads, _ = seed(tmp_path, monkeypatch)
    path = store.dir / f'{messages[1].id}.json'
    path.write_text('{')
    invalidate(store)
    with pytest.raises(ValueError):
        store.all()
    assert store._validated == {}
    path.write_text(messages[1].to_json())
    reads.clear()
    assert store.all() == messages
    assert len(reads) == 3


@pytest.mark.parametrize('age', [0, post.POST_TIMESTAMP_TICK_NS])
def test_changed_identity_within_tick_cannot_certify_the_listing(tmp_path, monkeypatch, age):
    from contextlib import contextmanager
    import os
    from pathlib import Path
    from types import SimpleNamespace

    store, messages, reads, clock = seed(tmp_path, monkeypatch)
    path = store.dir / f'{messages[0].id}.json'
    changed = replace(messages[0], body='body changed')
    path.write_text(changed.to_json())
    info = path.lstat()
    # Coarse timestamps can conceal a second same-size write inside this tick.
    observed = SimpleNamespace(**{name: getattr(info, name) for name in (
        'st_dev', 'st_ino', 'st_size', 'st_mode', 'st_mtime_ns', 'st_ctime_ns')})
    observed.st_mtime_ns = observed.st_ctime_ns = clock[0] - age
    original = Path.lstat
    monkeypatch.setattr(Path, 'lstat', lambda p, *a, **kw:
                        observed if p == path else original(p, *a, **kw))
    # Supply the same coarse timestamp at the listing and post-read check.
    scan = os.scandir
    class Entry:
        def __init__(self, entry, folder):
            self.entry, self.name, self.folder = entry, entry.name, folder
        def stat(self, **kwargs):
            return observed if self.folder / self.name == path else self.entry.stat(**kwargs)
    @contextmanager
    def coarse_scan(folder):
        located = Path(os.readlink(folder)) if str(folder).startswith('/proc/self/fd/') else Path(folder)
        with scan(folder) as entries:
            yield (Entry(entry, located) for entry in entries)
    monkeypatch.setattr(os, 'scandir', coarse_scan)
    invalidate(store)
    assert changed in store.all()
    assert len(reads) == 3
    assert store._validated[path.name][3] is None
    assert store._cache_at is None

    newest = replace(messages[0], body='body newest!')
    assert len(newest.to_json()) == len(changed.to_json())
    path.write_text(newest.to_json())
    clock[0] += post.POST_TIMESTAMP_TICK_NS + 1
    reads.clear()
    # No forced invalidation: the unsafe file must keep the listing untrusted.
    assert newest in store.all()
    assert reads == [path.name]
    assert store._validated[path.name][3] is not None
    reads.clear()
    assert newest in store.all()
    assert reads == []
