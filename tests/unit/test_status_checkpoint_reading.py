"""One status snapshot, fresh later readings, and certified zero-token history."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from quarterdeck import post, record_owner, ship as shipmod, watch
from quarterdeck.harness import reading_scope, synthetic_record
from quarterdeck.harness.fake import FakeHarness
from quarterdeck.harness.types import State
from quarterdeck.seat import Seat
from quarterdeck.service import Service

pytestmark = pytest.mark.usefixtures('synthetic_disk_room')


def test_one_reading_shares_posts_across_stores_and_the_next_sees_arrivals(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    first = post.Message('synthetic-first', 'order', 'issuer', 'worker', post.now(), 'synthetic work')
    post.Store(runtime).write(first)
    calls = []
    original = post.Store._load_current
    def load(self):
        calls.append(self.dir)
        return original(self)
    monkeypatch.setattr(post.Store, '_load_current', load)
    with post.reading_pass():
        assert post.Store(runtime).all() == [first]
        post.Store(runtime).all().clear()
        assert post.Store(runtime).open_for('worker') == [first]
        assert len(calls) == 1
    second = post.Message('synthetic-second', 'answer', 'issuer', 'worker', post.now(), 'synthetic arrival')
    post.Store(runtime).write(second)
    with post.reading_pass():
        assert post.Store(runtime).all() == [first, second]
        assert len(calls) == 2


def test_native_scope_reuses_only_stable_results_and_expires_at_exit(tmp_path, monkeypatch):
    record = tmp_path / 'synthetic.jsonl'
    record.write_text('first')
    clock = [time.time_ns() + 3_000_000_000]
    monkeypatch.setattr(reading_scope, 'time', SimpleNamespace(time_ns=lambda: clock[0]))
    calls = []
    def load():
        calls.append(True)
        return State(model=record.read_text()), []
    with reading_scope.reading_pass():
        state, turns = reading_scope.read('synthetic', record, load)
        state.model = 'caller changed its container'
        turns.append('caller addition')
        state, turns = reading_scope.read('synthetic', record, load)
        assert state.model == 'first' and turns == [] and len(calls) == 1
        record.write_text('second')
        assert reading_scope.read('synthetic', record, load)[0].model == 'second'
        assert len(calls) == 2
    reading_scope.read('synthetic', record, load)
    reading_scope.read('synthetic', record, load)
    assert len(calls) == 4, 'actions outside the presentation keep their normal reader path'


def test_recent_native_metadata_stays_uncertified(tmp_path, monkeypatch):
    record = tmp_path / 'synthetic.jsonl'
    record.write_text('synthetic')
    now = time.time_ns()
    monkeypatch.setattr(reading_scope, 'time', SimpleNamespace(time_ns=lambda: now))
    calls = []
    def load():
        calls.append(True)
        return State(), []
    with reading_scope.reading_pass():
        reading_scope.read('synthetic', record, load)
        reading_scope.read('synthetic', record, load)
    assert len(calls) == 2


def test_status_reuses_accounted_history_even_when_today_is_zero(tmp_path, monkeypatch):
    pool = shipmod.Pool('fake', 'synthetic')
    session = shipmod.Session('crew', 'Synthetic')
    spec = shipmod.SeatSpec('worker', 'crew', 'worker', 'fake', ('fake',), up=False)
    ship = shipmod.Ship(tmp_path, 'synthetic', 'operator', tmp_path, {'fake': pool}, {'crew': session}, {'worker': spec})
    tmux = SimpleNamespace(pane=lambda ident: None)
    seat = Seat(ship, spec, tmux)
    real_now = synthetic_record.now
    monkeypatch.setattr(synthetic_record, 'now', lambda: datetime.now(timezone.utc) - timedelta(days=1))
    writers = []
    for number in range(20):
        writer = synthetic_record.ClaudeShape(seat.home, tmp_path, f'synthetic-{number}', 'synthetic-model')
        writer.begin('synthetic input', 'synthetic-model', 'high')
        writer.end('synthetic output')
        record_owner.register(seat.folder, writer.path, 'fake')
        writers.append(writer)
    (seat.folder / 'record').write_text(str(writers[-1].path))
    svc = Service(ship, tmux)
    svc.resolved_seats = lambda: [seat]
    try:
        svc.account_records()
        assert svc.ledger.today('worker', datetime.now().date().isoformat()).known == 0
        assert svc.ledger.db.execute('SELECT COUNT(*) FROM record_accounting').fetchone() == (20,)
    finally:
        svc.ledger.close()
    reads = []
    snapshots = []
    from quarterdeck.harness import claude
    native_snapshot = claude._Reader.read_snapshot
    def snapshot(cls, record, parsed):
        snapshots.append(record)
        return native_snapshot(record, parsed)
    monkeypatch.setattr(claude._Reader, 'read_snapshot', classmethod(snapshot))
    settled = time.time_ns() + 3_000_000_000
    monkeypatch.setattr(reading_scope, 'time', SimpleNamespace(time_ns=lambda: settled))
    native_read = FakeHarness.read
    def read(self, record):
        reads.append(record)
        return native_read(self, record)
    monkeypatch.setattr(FakeHarness, 'read', read)
    data = watch.status_reading(ship, tmux)
    assert data['seats'][0].today.known == 0
    assert reads == [], 'unchanged certified histories are not reparsed to rediscover a measured zero'
    assert snapshots == [writers[-1].path], 'all current-record consumers share one stable native result'
    monkeypatch.setattr(synthetic_record, 'now', real_now)
    writers[0].begin('synthetic new input', 'synthetic-model', 'high')
    writers[0].end('synthetic new output')
    data = watch.status_reading(ship, tmux)
    assert data['seats'][0].today.known > 0
    assert writers[0].path in reads, 'a changed source keeps the ordinary uncached reading path'
