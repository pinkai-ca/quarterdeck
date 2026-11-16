"""One inventory supplies each seat's active planning rows; writers still reread."""

from dataclasses import asdict, replace
import json
from types import SimpleNamespace

from quarterdeck import lifecycle, pending, post
from quarterdeck.seat import SelectionError


def attempt(runtime, name, number, *, archived=False, result=None):
    folder = runtime / 'post' / ('retired/attempts' if archived else 'attempts')
    return post.Attempt('synthetic-message-' + str(number), name, '2032-01-01T00:00:00+00:00',
                        path=folder / ('synthetic-attempt-' + str(number) + '.json'), result=result)


def test_one_inventory_is_partitioned_by_owner_and_active_location(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    records, supplied, claimed = [], {}, []
    for name in ('former-a', 'former-b', 'working', 'uncertain', 'busy'):
        records.extend((attempt(runtime, name, len(records)),
                        attempt(runtime, name, len(records) + 1, result='delivered'),
                        attempt(runtime, name, len(records) + 2, archived=True)))
    (runtime / 'post' / 'pending' / 'journal-only').mkdir(parents=True)
    class Reader:
        dir = runtime / 'post' / 'attempts'
        reads = 0
        def all(self):
            self.reads += 1
            return records
        def abandon(self, name, reason, *, attempts):
            supplied[name] = list(attempts)
            return []
    reader = Reader()
    declared = dict.fromkeys(('working', 'uncertain', 'busy'))
    def seat(name):
        if name == 'uncertain':
            raise SelectionError('synthetic selection not ready')
        return SimpleNamespace(name=name)
    def reconcile(service, subject, known, *, latest):
        claimed.append(subject.name)
        assert set(known) == {row.path.name for row in records}  # global receipt lookup stays available
        assert set(latest) == {row.message for row in records}
        if subject.name == 'busy':
            raise lifecycle.Busy('synthetic lifecycle already claimed')
        return {row.path for row in known.values() if row.seat == subject.name}
    monkeypatch.setattr(pending, '_reconcile_claimed', reconcile)
    service = SimpleNamespace(runtime=runtime, attempts=reader, ship=SimpleNamespace(seats=declared),
                              seat=seat, hold_selection=lambda _: None)
    paths = pending.reconcile(service)
    assert reader.reads == 1
    assert paths == {row.path for row in records}
    assert claimed == ['working', 'busy']
    assert set(supplied) == {'former-a', 'former-b', 'journal-only'}
    assert supplied['journal-only'] == []
    for name in ('former-a', 'former-b'):
        assert supplied[name] == [row for row in records if row.seat == name and row.path.parent == reader.dir]
        assert len(supplied[name]) == 2  # terminal decisions remain the writer's, rather than a new filter here


def test_a_receipt_finished_after_planning_is_not_abandoned(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    reader = post.Attempts(runtime)
    old = attempt(runtime, 'former', 0)
    reader.dir.mkdir(parents=True)
    reader._write(old)
    stale = reader.all()
    reads, finished = [], []
    def inventory():
        reads.append(True)
        current = replace(old, result='delivered', detail='synthetic concurrent completion')
        post.Attempts(runtime)._write(current)
        finished.append(current.path.read_bytes())
        return stale
    monkeypatch.setattr(reader, 'all', inventory)
    service = SimpleNamespace(runtime=runtime, attempts=reader, ship=SimpleNamespace(seats={}))
    paths = pending.reconcile(service)
    assert reads == [True]
    assert paths == {old.path}
    assert old.path.read_bytes() == finished[0]
    assert json.loads(old.path.read_text())['result'] == 'delivered'
    assert not (runtime / 'log' / 'service.jsonl').exists()


def test_abandonment_still_reads_a_planned_candidate_under_its_lock(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    reader = post.Attempts(runtime)
    old = attempt(runtime, 'former', 0, result='queued')
    reader.dir.mkdir(parents=True)
    reader._write(old)
    locked = []
    original = reader._locked
    from contextlib import contextmanager
    @contextmanager
    def lock(*args, **kwargs):
        with original(*args, **kwargs):
            locked.append(True)
            try:
                yield
            finally:
                locked.pop()
    read = pending.post_dir.read_text
    candidate_reads = []
    def text(path, *args, **kwargs):
        if path == old.path:
            candidate_reads.append(bool(locked))
        return read(path, *args, **kwargs)
    monkeypatch.setattr(reader, '_locked', lock)
    monkeypatch.setattr(pending.post_dir, 'read_text', text)
    service = SimpleNamespace(runtime=runtime, attempts=reader, ship=SimpleNamespace(seats={}))
    pending.reconcile(service)
    assert candidate_reads == [False, True]  # inventory, then fresh candidate under the existing write lock
    fields = json.loads(old.path.read_text())
    assert fields['result'] == 'abandoned'
    assert fields['detail'].endswith('; previous result: queued')
