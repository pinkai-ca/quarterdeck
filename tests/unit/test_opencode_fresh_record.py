"""Fresh-home reader candidates, using synthetic SQLite rows and an event-driven clock only."""

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from quarterdeck import check
from quarterdeck.door import Delivery
from quarterdeck.harness.base import RecordError
from quarterdeck.harness.opencode import OpenCodeHarness

STAMP = 978307200000


class FreshDatabase:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.writer = sqlite3.connect(path)
        self.writer.execute('PRAGMA journal_mode=WAL')
        self.writer.execute('PRAGMA wal_autocheckpoint=0')
        self.writer.executescript('''
            CREATE TABLE session (id TEXT PRIMARY KEY, time_created INTEGER, time_updated INTEGER, parent_id TEXT);
            CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
            CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
        ''')
        self.writer.commit()

    def start(self, at=STAMP):
        self.at = at
        user = dict(role='user', time=dict(created=at), model=dict(modelID='synthetic-model'))
        self.assistant = dict(role='assistant', parentID='message-input', modelID='synthetic-model',
                              time=dict(created=at + 1, completed=at + 2), finish='tool-calls')
        self.writer.execute('INSERT INTO session VALUES (?, ?, ?, NULL)', ('session-fresh', at, at))
        for name, when, data in [('message-input', at, user), ('message-answer', at + 1, self.assistant)]:
            self.writer.execute('INSERT INTO message VALUES (?, ?, ?, ?, ?)',
                                (name, 'session-fresh', when, when, json.dumps(data)))
        for name, message, text in [('part-input', 'message-input', 'synthetic task'),
                                    ('part-output', 'message-answer', 'synthetic reply')]:
            self.writer.execute('INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)',
                                (name, message, 'session-fresh', at, at, json.dumps(dict(type='text', text=text))))
        self.writer.commit()

    def stop(self, after=3):
        self.assistant['finish'] = 'stop'
        self.assistant['time']['completed'] = self.at + after
        self.writer.execute('UPDATE message SET time_updated=?, data=? WHERE id=?',
                            (self.at + after, json.dumps(self.assistant), 'message-answer'))
        self.writer.commit()

    def close(self):
        self.writer.close()


@pytest.fixture
def databases():
    opened = []

    def create(path):
        result = FreshDatabase(path)
        opened.append(result)
        return result

    yield create
    for item in opened:
        item.close()


def test_first_session_joins_its_new_data_directory(tmp_path, databases):
    home = tmp_path / 'synthetic-home'
    harness = OpenCodeHarness()
    old = databases(home / 'data-000' / 'opencode' / 'opencode.db')
    old.start(STAMP - 10000)
    old.stop()
    launched = datetime.fromtimestamp(STAMP / 1000, timezone.utc)
    assert harness.join(home, launched).record is None
    selected = harness._data_dir(home, None, 'synthetic-seat')
    assert selected == home / 'data-001'
    fresh = databases(selected / 'opencode' / 'opencode.db')
    assert harness.join(home, launched).record is None
    fresh.start()
    fresh.stop()
    assert harness.join(home, launched).record == fresh.path
    assert harness.session_id(fresh.path) == 'session-fresh'
    assert harness.state(fresh.path).turns == 1


@pytest.mark.parametrize('before', ['missing', 'schema-only', 'empty-tables'])
def test_a_read_before_first_history_does_not_cache_zero(tmp_path, databases, before):
    path = tmp_path / 'opencode.db'
    harness = OpenCodeHarness()
    if before == 'schema-only':
        with sqlite3.connect(path) as initial:
            initial.execute('CREATE TABLE bootstrap (value TEXT)')
    if before == 'empty-tables':
        fresh = databases(path)
        assert harness.state(path).turns == 0
    else:
        with pytest.raises(RecordError):
            harness.state(path)
        fresh = databases(path)
    fresh.start()
    fresh.stop()
    state = harness.state(path)
    assert state.turns == 1
    assert state.turn_open is False


def test_an_existing_zero_turn_read_refreshes_from_uncheckpointed_wal(tmp_path, databases):
    fresh = databases(tmp_path / 'opencode.db')
    fresh.start()
    fresh.writer.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    harness = OpenCodeHarness()
    assert harness.state(fresh.path).turns == 0
    assert harness.state(fresh.path).turn_open is True
    before = fresh.path.read_bytes()
    fresh.stop()
    assert fresh.path.read_bytes() == before
    assert Path(str(fresh.path) + '-wal').stat().st_size > 32
    state = harness.state(fresh.path)
    assert state.turns == 1
    assert state.turn_open is False


@pytest.mark.parametrize(('stop_seconds', 'passed'), [(59, True), (61, False)])
def test_the_probe_rechecks_closure_for_its_declared_window(tmp_path, databases, monkeypatch,
                                                           stop_seconds, passed):
    fresh = databases(tmp_path / 'opencode.db')
    fresh.start()
    runtime = tmp_path / 'synthetic-runtime'
    ship = SimpleNamespace(runtime=runtime)
    seat = SimpleNamespace(name='synthetic-seat', harness=OpenCodeHarness(), is_up=True,
                           spec=SimpleNamespace(model=None), up=lambda **kw: 'synthetic launch',
                           launched=lambda: SimpleNamespace(answered=[]), record=lambda: fresh.path)
    store = check.post.Store(runtime)
    monkeypatch.setattr(check.post, 'Store', lambda folder: SimpleNamespace(
        new=store.new, closing_report=lambda order: SimpleNamespace(short='synthetic-report', body='written')))
    monkeypatch.setattr(check.post, 'Attempts', lambda folder: SimpleNamespace(
        open=lambda *args: SimpleNamespace(), close=lambda *args: None))
    monkeypatch.setattr(check.pending, 'current_identity', lambda seat: dict(
        pane='synthetic-pane', pid=123, launch='2001-01-01T00:00:00+00:00'))
    proof = SimpleNamespace(data=dict(phase='submitted'), finish=lambda result: None)
    monkeypatch.setattr(check.pending, 'Proof', SimpleNamespace(begin=lambda *args: proof))
    monkeypatch.setattr(check, 'secrets', SimpleNamespace(token_hex=lambda size: 'synthetic-proof'))

    class Door:
        def __init__(self, seat):
            pass

        def reading(self):
            return SimpleNamespace(open=True)

        def deliver(self, text, *, proof):
            (runtime / 'check' / 'synthetic-seat.txt').write_text('qd-check-synthetic-proof\n')
            return Delivery('delivered', 'synthetic transport')

    monkeypatch.setattr(check, 'Door', Door)
    clock, waits = [0], []

    def wait_for(predicate, seconds):
        waits.append(seconds)
        deadline = clock[0] + seconds
        while clock[0] < deadline:
            if clock[0] >= stop_seconds:
                fresh.stop(after=stop_seconds * 1000)
            value = predicate()
            if value:
                return value
            clock[0] += 1
        return None

    monkeypatch.setattr(check, 'wait_for', wait_for)
    results = check.probe(ship, seat)
    assert all(item.passed for item in results[:-1])
    assert results[-1].name == 'record joined, the turn ended and read'
    assert results[-1].passed is passed
    assert f'{1 if passed else 0} turns' in results[-1].detail
    assert waits[-1] == 60
    assert clock[0] == (59 if passed else 60)
