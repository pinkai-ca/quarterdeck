"""Historical reconciliation keeps one parsed attempt inventory."""
from dataclasses import asdict
import json
from types import SimpleNamespace

from quarterdeck import pending, post, post_dir


def history(runtime):
    reader = post.Attempts(runtime)
    reader.dir.mkdir(parents=True)
    for number in range(6):
        attempt = post.Attempt(f'ord-history-{number}', f'former-{number % 3}',
                               '2032-01-01T00:00:00+00:00', result='delivered',
                               path=reader.dir / f'ord-history-{number}-1.json')
        data = asdict(attempt)
        data.pop('path')
        attempt.path.write_text(json.dumps(data))
    return reader


def observe_reads(monkeypatch, folder):
    reads, original = [], post_dir.read_text
    def read(path, *args, **kwargs):
        if path.parent == folder:
            reads.append(path.name)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(post_dir, 'read_text', read)
    return reads


def test_reconcile_parses_history_once_across_undeclared_recipients(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    reader = history(runtime)
    reads = observe_reads(monkeypatch, reader.dir)
    service = SimpleNamespace(runtime=runtime, attempts=reader, ship=SimpleNamespace(seats={}))
    paths = pending.reconcile(service)
    assert len(paths) == 6
    assert len(reads) == 6 and len(set(reads)) == 6
    assert len(reader.all()) == 6
    assert len(reads) == 6


def test_standalone_abandonment_still_constructs_its_reader(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    reader = history(runtime)
    reads = observe_reads(monkeypatch, reader.dir)
    before = {p.name: p.read_bytes() for p in reader.dir.glob('*.json')}
    pending.abandon(runtime, 'former-0', 'synthetic maintenance')
    assert len(reads) == 6
    assert {p.name: p.read_bytes() for p in reader.dir.glob('*.json')} == before
