"""Preparation survives a slice, but only a complete fresh scan is evidence."""

from collections import Counter
from dataclasses import asdict
import json
from types import SimpleNamespace

import pytest

from quarterdeck import post, post_dir, post_retirement as retirement


def archive(runtime, count):
    store, attempts = post.Store(runtime), post.Attempts(runtime)
    message = store.new('order', 'lead', 'builder', 'Synthetic completed task')
    store.new('report', 'builder', 'lead', 'Done', re=message.id)
    attempts.dir.mkdir(parents=True)
    for index in range(count):
        attempt = post.Attempt(message.id, 'builder', f'2000-01-01T00:00:00.{index:06d}+00:00',
                               '%1', 100, '2000-01-01T00:00:00+00:00', result='delivered')
        fields = {key: value for key, value in asdict(attempt).items() if key != 'path'}
        (attempts.dir / f'{message.id}-{index + 1}.json').write_text(json.dumps(fields))
    return SimpleNamespace(runtime=runtime, store=store, attempts=attempts,
                           ledger=SimpleNamespace(catching_up=lambda: False)), message


def test_cold_retirement_makes_progress_across_default_slices(tmp_path, monkeypatch, retirement_clock):
    service, _ = archive(tmp_path, 600)
    reads = Counter()
    read = post_dir.read_text
    def costly_parse(path, *args, **kwargs):
        if path.parent == service.attempts.dir:
            reads[path.name] += 1
            retirement_clock.now += 0.006
        return read(path, *args, **kwargs)
    monkeypatch.setattr(post_dir, 'read_text', costly_parse)

    results = []
    for _ in range(12):
        result = retirement.run(service)
        results.append(result)
        if sum(r['attempts'] for r in results) == 600:
            break
    assert results[0] == dict(attempts=0, journals=0, paused=True)
    assert sum(r['attempts'] for r in results) == 600
    assert len(results) > 1
    assert max(reads.values()) == 2  # one parse, then the locked pre-move content check
    assert len(service.attempts.all()) == 600


def interrupted_scan(attempts):
    calls = 0
    def progress():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise retirement.SliceEnded()
    with pytest.raises(retirement.SliceEnded):
        attempts.all(progress=progress)


def test_yielded_cache_rechecks_edits_additions_and_removals(tmp_path, retirement_clock):
    service, message = archive(tmp_path, 201)
    attempts = service.attempts
    interrupted_scan(attempts)
    assert len(attempts._cache) == 200
    first, second = [attempts.dir / name for name in list(attempts._cache)[:2]]
    fields = json.loads(first.read_text())
    fields['result'] = 'blind'
    first.write_text(json.dumps(fields))
    second.unlink()
    added = attempts.dir / f'{message.id}-202.json'
    added.write_text(json.dumps(fields | {'result': None}))

    rows = attempts.all()
    by_name = {a.path.name: a for a in rows}
    assert len(rows) == 201
    assert by_name[first.name].result == 'blind'
    assert second.name not in by_name
    assert by_name[added.name].result is None
    assert second.name not in attempts._cache
    # A later pending attempt keeps every receipt of this message in place.
    assert retirement.run(service) == dict(attempts=0, journals=0)


def test_yielded_cache_cannot_hide_a_read_error_or_return_partial_history(tmp_path, retirement_clock):
    service, _ = archive(tmp_path, 201)
    interrupted_scan(service.attempts)
    path = service.attempts.dir / next(iter(service.attempts._cache))
    path.write_text('{')
    with pytest.raises(json.JSONDecodeError):
        service.attempts.all()
    with pytest.raises(json.JSONDecodeError):
        retirement.run(service)
    assert len(list(service.attempts.dir.glob('*.json'))) == 201
    assert not (tmp_path / 'post/retired/index').exists()
