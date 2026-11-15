"""A warm reread uses the listing's identity without a second pathname stat."""

from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time

import pytest

from quarterdeck import post


@pytest.mark.parametrize('folder', ['store', 'attempts'])
def test_reread_stats_each_listed_name_once(tmp_path, monkeypatch, folder):
    runtime = tmp_path / 'runtime'
    reader = post.Store(runtime) if folder == 'store' else post.Attempts(runtime)
    reader.dir.mkdir(parents=True)
    for i in range(3):
        name = f'ord-20260101-0000-source-{i:04x}'
        if folder == 'store':
            row = post.Message(name, 'order', 'source', 'worker',
                               datetime(2026, 1, 1, tzinfo=timezone.utc), 'Task')
            text = row.to_json()
        else:
            row = post.Attempt(name, 'worker', '2026-01-01T00:00:00+00:00',
                               '%1', 100, '2026-01-01T00:00:00+00:00', result='delivered')
            text = json.dumps({k: v for k, v in asdict(row).items() if k != 'path'})
        (reader.dir / f'{name}.json').write_text(text)
    (reader.dir / 'notes').write_text('ignored')
    (reader.dir / 'nested').mkdir()
    now = time.time_ns() + 10_000_000_000
    monkeypatch.setattr(post.time, 'time_ns', lambda: now)
    expected = reader.all()
    names = {p.name for p in reader.dir.iterdir()}
    counts, follows = Counter(), []
    scan, pathname_stat = os.scandir, os.stat

    class Entry:
        def __init__(self, entry):
            self.entry, self.name = entry, entry.name

        def stat(self, *, follow_symlinks=True):
            counts[self.name] += 1
            follows.append(follow_symlinks)
            return self.entry.stat(follow_symlinks=follow_symlinks)

    @contextmanager
    def counted_scan(path):
        actual = Path(os.readlink(path)) if str(path).startswith('/proc/self/fd/') else Path(path)
        with scan(path) as entries:
            yield (Entry(e) for e in entries) if actual == reader.dir else entries

    def counted_stat(path, *args, **kwargs):
        if isinstance(path, (str, bytes, os.PathLike)):
            located = Path(os.fsdecode(path))
            if located.parent == reader.dir:
                counts[located.name] += 1
        return pathname_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, 'scandir', counted_scan)
    monkeypatch.setattr(os, 'stat', counted_stat)
    for _ in range(2):
        counts.clear()
        follows.clear()
        if folder == 'store':
            reader._cache_at = None
        assert reader.all() == expected
        assert counts == Counter({name: 1 for name in names})
        assert follows == [False] * len(names)


def test_store_directory_gone_before_listing_returns_no_messages(tmp_path, monkeypatch):
    store = post.Store(tmp_path / 'runtime')
    store.new('order', 'source', 'worker', 'Task')
    assert len(store.all()) == 1
    store._cache_at = None
    listing = post.post_dir.list_children

    def moved(directory, **kwargs):
        if directory == store.dir:
            directory.rename(directory.with_name('moved-store'))
        return listing(directory, **kwargs)

    monkeypatch.setattr(post.post_dir, 'list_children', moved)
    assert store.all() == []
