"""Ordinary synthetic histories: incremental certificates equal full attribution."""

from datetime import datetime, timedelta, timezone
import json
import random
from types import SimpleNamespace

import pytest

from quarterdeck import harness
from quarterdeck.attribution_cache import TurnIntervals
from quarterdeck.harness import reader_stamp
from quarterdeck.ledger import Ledger
from quarterdeck.order_attribution import moment
from quarterdeck.post_message import Message


ORIGIN = datetime(2025, 1, 1, tzinfo=timezone.utc)


def at(minutes):
    return (ORIGIN + timedelta(minutes=minutes)).isoformat() if minutes is not None else None


class History:
    def __init__(self, root, monkeypatch, mode):
        self.root, self.mode = root, mode
        self.book = Ledger(root / 'ledger.sqlite')
        self.cache = self.book.attributions
        self.messages = [Message('ord-ordinary-work', 'order', 'supervisor', 'builder',
                                 ORIGIN - timedelta(minutes=20), 'Ordinary synthetic work')]
        self.store = SimpleNamespace(all=lambda: self.messages)
        self.reads = []
        self.visits = []
        replay_row = self.cache.replay_row
        def visit(record, *args, **kwargs):
            self.visits.append(record.rsplit('/', 1)[-1])
            return replay_row(record, *args, **kwargs)
        monkeypatch.setattr(self.cache, 'replay_row', visit)
        self.journal = root / 'seats' / 'builder' / 'record-launches.jsonl'
        self.journal.parent.mkdir(parents=True)
        self.journal.write_text('')
        def inputs(record):
            self.reads.append(record.name)
            return []
        monkeypatch.setattr(harness, 'get', lambda name: SimpleNamespace(attribution_inputs=inputs))
        monkeypatch.setattr(reader_stamp, 'attributions',
                            lambda: {'codex': ['ordinary-reader', 'ordinary-engine']})

    def turn(self, name, start, end, *, index=1, seat='builder'):
        record = self.root / name
        if not record.exists():
            record.write_text('Ordinary synthetic native record.\n')
            with self.journal.open('a') as stream:
                stream.write(json.dumps(dict(record=str(record), harness='codex')) + '\n')
        self.book.db.execute('''INSERT OR REPLACE INTO turns
            (record,turn,seat,pool,order_id,started,ended) VALUES (?,?,?,?,?,?,?)''',
                             (str(record), index, seat, 'ordinary-pool', '-', at(start), at(end)))
        return record

    def posted(self, name, minute):
        self.messages.append(Message('ans-ordinary-' + name, 'answer', 'builder', 'supervisor',
                                     ORIGIN + timedelta(minutes=minute), 'Ordinary progress',
                                     re='ord-ordinary-work'))

    def replay(self, *, full=False):
        if full:
            self.book.db.execute('DELETE FROM attribution_records')
            self.cache.sources.clear()
            self.cache.last = None
        self.reads.clear()
        self.visits.clear()
        if self.mode == 'stream':
            self.cache.replay_stream(self.store, self.root, 'ordinary-framework',
                                     lambda *args: None, lambda: None)
        else:
            self.cache.replay(self.store, self.root, framework='ordinary-framework')
        return list(dict.fromkeys(self.visits))

    def rows(self):
        return json.dumps(self.book.db.execute(
            'SELECT * FROM turn_attribution ORDER BY record,turn').fetchall(),
                          ensure_ascii=False, separators=(',', ':')).encode()

    def status(self, name):
        return self.book.db.execute('SELECT status,reason FROM turn_attribution WHERE record=?',
                                    (str(self.root / name),)).fetchone()


@pytest.fixture(params=('stream', 'ordinary'))
def history(tmp_path, monkeypatch, request):
    value = History(tmp_path, monkeypatch, request.param)
    yield value
    value.book.close()


def test_new_disjoint_turn_keeps_long_history_and_equals_full_replay(history):
    for i in range(128):
        history.turn(f'ordinary-history-{i:03}', i * 20, i * 20 + 10)
        history.posted(f'history-{i:03}', i * 20 + 5)
    assert len(history.replay()) == 128
    old = dict(history.book.db.execute('SELECT record,evidence FROM attribution_records'))
    history.turn('ordinary-future', 3000, 3010)
    history.posted('future', 3005)
    assert history.replay() == ['ordinary-future']
    assert history.reads == ['ordinary-future']
    assert all(history.book.db.execute('SELECT evidence FROM attribution_records WHERE record=?',
                                      (record,)).fetchone() == (evidence,)
               for record, evidence in old.items())
    incremental = history.rows()
    assert len(history.replay(full=True)) == 129
    assert history.rows() == incremental


@pytest.mark.parametrize('before,after,expected', [
    ((4, None), (4, 6), 'unresolved'),  # an open turn closes over the old post
    ((4, 6), (7, 9), 'attributed'),    # another record's bound edit removes overlap
    ((6, 4), (4, 6), 'unresolved'),    # reversed bounds were not a closed interval
    ((None, 6), (4, 6), 'unresolved'),
    ((11, 12), (5, 5), 'unresolved'),  # inclusive, zero-length interval at the post
])
def test_changed_other_record_replays_affected_history(history, before, after, expected):
    history.turn('ordinary-older', 0, 10)
    history.posted('older', 5)
    history.turn('ordinary-other', *before)
    history.turn('ordinary-disjoint', 100, 110)
    history.posted('disjoint', 105)
    history.replay()
    history.turn('ordinary-other', *after)
    assert set(history.replay()) == {'ordinary-older', 'ordinary-other'}
    assert history.status('ordinary-older')[0] == expected
    incremental = history.rows()
    history.replay(full=True)
    assert history.rows() == incremental


def test_removing_overlap_replays_the_older_record(history):
    history.turn('ordinary-older', 0, 10)
    history.posted('older', 5)
    other = history.turn('ordinary-other', 4, 6)
    history.replay()
    assert history.status('ordinary-older')[0] == 'unresolved'
    history.book.db.execute('DELETE FROM turns WHERE record=?', (str(other),))
    assert history.replay() == ['ordinary-older']
    assert history.status('ordinary-older')[0] == 'attributed'
    incremental = history.rows()
    history.replay(full=True)
    assert history.rows() == incremental


def test_overlapping_other_seat_does_not_replay_history(history):
    history.turn('ordinary-older', 0, 10)
    history.posted('older', 5)
    history.replay()
    history.turn('ordinary-other-seat', 4, 6, seat='another-builder')
    assert history.replay() == ['ordinary-other-seat']
    incremental = history.rows()
    history.replay(full=True)
    assert history.rows() == incremental


def test_overlapping_turn_without_old_post_keeps_older_record(history):
    history.turn('ordinary-older', 0, 10)
    history.replay()
    history.turn('ordinary-other', 4, 6)
    assert history.replay() == ['ordinary-other']
    incremental = history.rows()
    history.replay(full=True)
    assert history.rows() == incremental


def test_backfilled_closing_report_replays_old_open_order_fallback(history):
    history.turn('ordinary-older', 0, 10)
    history.replay()
    assert history.status('ordinary-older')[0] == 'attributed'
    history.messages.append(Message('rep-ordinary-close', 'report', 'builder', 'supervisor',
                                     ORIGIN - timedelta(minutes=1), 'Ordinary work finished',
                                     re='ord-ordinary-work'))
    assert history.replay() == ['ordinary-older']
    assert history.status('ordinary-older')[0] == 'unassociated'
    incremental = history.rows()
    history.replay(full=True)
    assert history.rows() == incremental


def test_own_record_bounds_and_native_bytes_still_invalidate(history):
    record = history.turn('ordinary-older', 0, 10)
    history.posted('older', 5)
    history.replay()
    # The same record's other turn can make its incoming evidence ambiguous,
    # even without a post at any of that other turn's timestamps.
    history.turn('ordinary-older', 7, 9, index=2)
    assert history.replay() == ['ordinary-older']
    record.write_text('Changed ordinary synthetic native record.\n')
    assert history.replay() == ['ordinary-older']
    incremental = history.rows()
    history.replay(full=True)
    assert history.rows() == incremental


def test_interval_index_equals_the_original_closed_interval_predicate():
    rng = random.Random(41)
    rows = [dict(started=at(rng.randrange(-50, 50)), ended=at(rng.randrange(-50, 50)))
            for _ in range(150)]
    rows += [dict(started=at(0), ended=None), dict(started=None, ended=at(1)),
             dict(started='2025-01-01T00:00:00', ended=at(10)),
             dict(started='invalid', ended=at(10)),
             dict(started='2025-01-01T01:00:00+01:00', ended=at(0))]
    intervals = TurnIntervals(rows)
    for minute in range(-55, 56):
        point = moment(at(minute))
        expected = sum(moment(row['started']) is not None and moment(row['ended']) is not None
                       and moment(row['started']) <= point <= moment(row['ended']) for row in rows)
        assert intervals.count(point) == expected
