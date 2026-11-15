"""Deterministic disappearance between listing, stat and read, with real files."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import (cascade_runtime, fleet_post, group_post, pending, post,
                         post_dir, post_retirement as retirement, structure_outcome)
from .test_attempt_scan_progress import archive
from .test_post_retirement import delivery, service


def disappear(path, operation):
    if operation == 'remove':
        path.unlink()
    else:
        destination = path.parent / 'retired' / path.name
        destination.parent.mkdir(exist_ok=True)
        path.rename(destination)


def interleave(monkeypatch, victim, stage, callback):
    """Fire once at the exact per-name operation, after directory enumeration."""
    fired = []
    if stage == 'stat':
        scan = os.scandir
        class Entry:
            def __init__(self, entry, folder):
                self.entry, self.name, self.folder = entry, entry.name, folder
            def stat(self, **kwargs):
                if self.folder / self.name == victim and not fired:
                    fired.append(True)
                    callback()
                return self.entry.stat(**kwargs)
        @contextmanager
        def run(path):
            folder = Path(os.readlink(path)) if str(path).startswith('/proc/self/fd/') else Path(path)
            with scan(path) as entries:
                yield (Entry(entry, folder) for entry in entries)
        monkeypatch.setattr(os, 'scandir', run)
    else:
        original = post_dir.read_text
        def run(path, *args, **kwargs):
            if path == victim and not fired:
                fired.append(True)
                callback()
            return original(path, *args, **kwargs)
        monkeypatch.setattr(post_dir, 'read_text', run)
    return fired


@pytest.mark.parametrize('operation', ['remove', 'retire'])
@pytest.mark.parametrize('warm', [False, True])
@pytest.mark.parametrize('stage', ['stat', 'read'])
def test_attempt_disappearance_evicts_the_parsed_entry(monkeypatch, tmp_path, operation, warm, stage):
    s, _ = archive(tmp_path / 'runtime', 2)
    records = s.attempts.all()
    victim = records[0].path
    fields = json.loads(victim.read_text())
    if not warm:
        s.attempts._cache = {}
    elif stage == 'read':
        victim.write_text(json.dumps(fields) + ' ')  # changed bytes require a fresh parse
    original = victim.read_bytes()
    def change():
        if operation == 'remove':
            victim.unlink()
            return
        # Retirement publishes its index before moving the active original.
        root = s.runtime / 'post' / 'retired'
        (root / 'index').mkdir(parents=True)
        (root / 'index' / 'synthetic-batch.json').write_text(json.dumps(
            dict(format=1, attempts={victim.name: fields})))
        (root / 'attempts').mkdir()
        victim.rename(root / 'attempts' / victim.name)
    fired = interleave(monkeypatch, victim, stage, change)
    rows = s.attempts.all()
    assert fired == [True]
    assert [row.path.name for row in rows] == [records[1].path.name]
    assert victim.name not in s.attempts._cache
    later = s.attempts.all()
    if operation == 'retire':
        retained = next(row for row in later if row.path.name == victim.name)
        assert retained.path.parent == s.runtime / 'post' / 'retired' / 'attempts'
        assert retained.path.read_bytes() == original
    else:
        assert [row.path.name for row in later] == [records[1].path.name]


@pytest.mark.parametrize('operation', ['remove', 'retire'])
@pytest.mark.parametrize('warm', [False, True])
def test_retired_index_disappearance_drops_its_cached_rows(tmp_path, monkeypatch, operation, warm):
    s = service(tmp_path / 'runtime')
    delivery(s)
    retirement.run(s)
    assert s.attempts.all()
    victim, = (s.runtime / 'post' / 'retired' / 'index').glob('*.json')
    if not warm:
        s.attempts._retired_cache = {}
    fired = interleave(monkeypatch, victim, 'stat', lambda: disappear(victim, operation))
    assert s.attempts.all() == []
    assert fired == [True] and victim.name not in s.attempts._retired_cache


@pytest.mark.parametrize('layout', ['runtime', 'plain'])
@pytest.mark.parametrize('operation', ['remove', 'retire'])
def test_directory_entry_stat_skips_only_the_gone_name(tmp_path, monkeypatch, layout, operation):
    folder = tmp_path / layout / 'post' / 'attempts'
    folder.mkdir(parents=True)
    victim = folder / 'gone.json'
    victim.write_text('{}')
    (folder / 'kept.json').write_text('{}')
    scan = os.scandir
    fired = []
    class Entry:
        def __init__(self, entry):
            self.entry, self.name = entry, entry.name
        def stat(self, **kwargs):
            if self.name == victim.name and not fired:
                fired.append(True)
                disappear(victim, operation)
            return self.entry.stat(**kwargs)
    @contextmanager
    def racing_scan(path):
        with scan(path) as entries:
            yield (Entry(entry) for entry in entries)
    monkeypatch.setattr(os, 'scandir', racing_scan)
    names = {name for name, _ in post_dir.list_children(folder)}
    assert fired == [True] and victim.name not in names and 'kept.json' in names


@pytest.mark.parametrize('operation', ['remove', 'retire'])
@pytest.mark.parametrize('warm', [False, True])
def test_store_refresh_skips_disappeared_messages_without_stale_cache(tmp_path, monkeypatch, operation, warm):
    store = post.Store(tmp_path / 'runtime')
    lost = store.new('order', 'lead', 'builder', 'Synthetic task')
    kept = store.new('order', 'lead', 'builder', 'Other synthetic task')
    if warm:
        assert len(store.all()) == 2
    victim = store.dir / (lost.id + '.json')
    fired = interleave(monkeypatch, victim, 'stat', lambda: disappear(victim, operation))
    assert store.all() == [kept]
    assert fired == [True]
    assert store.all() == [kept]
    assert store._cache[1] == [kept]


@pytest.mark.parametrize('operation', ['remove', 'retire'])
def test_pending_journal_gone_during_read_is_not_an_unreadable_journal(tmp_path, monkeypatch, operation):
    s = service(tmp_path / 'runtime')
    _, _, victim = delivery(s)
    _, _, kept = delivery(s)
    fired = interleave(monkeypatch, victim, 'read', lambda: disappear(victim, operation))
    proofs, errors = pending._journals(s.runtime, 'builder')
    assert fired == [True] and errors == []
    assert [proof.path for proof in proofs] == [kept]


@pytest.mark.parametrize('operation', ['remove', 'retire'])
def test_retirement_scan_skips_an_index_that_disappears_before_read(tmp_path, monkeypatch, operation):
    s = service(tmp_path / 'runtime')
    folder = s.runtime / 'post' / 'retired' / 'index'
    folder.mkdir(parents=True)
    victim = folder / 'synthetic-batch.json'
    victim.write_text(json.dumps(dict(format=1, attempts={})))
    fired = interleave(monkeypatch, victim, 'read', lambda: disappear(victim, operation))
    assert retirement.run(s) == dict(attempts=0, journals=0)
    assert fired == [True]


@pytest.mark.parametrize('stage', ['stat', 'read'])
def test_present_unreadable_attempts_still_raise(tmp_path, monkeypatch, stage):
    s, _ = archive(tmp_path / 'runtime', 1)
    row, = s.attempts.all()
    victim = row.path
    if stage == 'read':
        victim.write_text(victim.read_text() + ' ')
    def denied():
        raise PermissionError('synthetic read denied')
    interleave(monkeypatch, victim, stage, denied)
    with pytest.raises(PermissionError, match='synthetic read denied'):
        s.attempts.all()
    assert victim.exists()


def test_missing_dependency_of_a_present_attempt_is_not_silenced(tmp_path, monkeypatch):
    s, _ = archive(tmp_path / 'runtime', 1)
    victim, = s.attempts.dir.glob('*.json')
    def missing_dependency():
        raise FileNotFoundError('synthetic other missing name')
    interleave(monkeypatch, victim, 'read', missing_dependency)
    with pytest.raises(FileNotFoundError, match='synthetic other missing name'):
        s.attempts.all()
    assert victim.exists()


def test_present_unreadable_journal_keeps_its_error(tmp_path, monkeypatch):
    s = service(tmp_path / 'runtime')
    _, _, victim = delivery(s)
    def denied():
        raise PermissionError('synthetic read denied')
    interleave(monkeypatch, victim, 'read', denied)
    assert pending._journals(s.runtime, 'builder') == ([], [victim.name])


@pytest.mark.parametrize('kind', ['drop', 'group', 'cascade', 'wait'])
@pytest.mark.parametrize('operation', ['remove', 'retire'])
def test_derived_post_scan_does_not_publish_a_phantom_error(tmp_path, monkeypatch, kind, operation):
    runtime = tmp_path / 'runtime'
    folders = dict(drop='drop-holds', group='group-holds', cascade='cascade-holds', wait='structure-waits')
    victim = runtime / 'post' / folders[kind] / 'ord-synthetic-sample.json'
    victim.parent.mkdir(parents=True)
    victim.write_text('{')  # a present malformed file must remain an error
    subject = SimpleNamespace(runtime=runtime, seats={})
    readers = dict(drop=lambda: post.drop_alarms(runtime), group=lambda: group_post.holds(runtime),
                   cascade=lambda: cascade_runtime.holds(runtime), wait=lambda: structure_outcome.waits(subject))
    assert readers[kind]()
    if kind == 'cascade':
        original = cascade_runtime.read_file
        def racing(path):
            if path == victim:
                disappear(path, operation)
            return original(path)
        monkeypatch.setattr(cascade_runtime, 'read_file', racing)
    elif kind in ('group', 'wait'):
        original = Path.read_text
        def racing(path, *args, **kwargs):
            if path == victim:
                disappear(path, operation)
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'read_text', racing)
    else:
        interleave(monkeypatch, victim, 'read', lambda: disappear(victim, operation))
    assert readers[kind]() == []


def test_incoming_offer_gone_during_listing_is_not_a_transport_hold(tmp_path, monkeypatch):
    target = SimpleNamespace(runtime=tmp_path / 'target' / 'runtime', folder=tmp_path / 'target')
    source = SimpleNamespace(runtime=tmp_path / 'source' / 'runtime', folder=tmp_path / 'source')
    victim = source.runtime / 'post' / 'outbound' / 'target' / 'synthetic.json'
    victim.parent.mkdir(parents=True)
    victim.write_text('{}')
    monkeypatch.setattr(fleet_post, 'export', lambda *args: [])
    monkeypatch.setattr(fleet_post, 'peers', lambda *args: {'source': source})
    def gone(*args):
        victim.unlink()
        return victim.read_bytes()
    monkeypatch.setattr(fleet_post, '_offer', gone)
    assert fleet_post.run(target, SimpleNamespace()) == []
