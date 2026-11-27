"""Restart accounting reuses committed observations, while grown sources are read fully."""

from dataclasses import replace
from datetime import timedelta
import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

from quarterdeck.ledger import Ledger
from quarterdeck import pane_death, record_owner, watch
from quarterdeck.harness import reader_stamp
from quarterdeck.post import Message
from quarterdeck.tmux import Pane
from tests.unit.test_ledger_pass_boundaries import AT, world, turn, writes


def restart(w):
    """Discard the same process-local state a new service starts without."""
    path = w.svc.ledger.path
    w.svc.ledger.close()
    w.svc.ledger = Ledger(path)
    w.svc.read_marks = {}
    w.svc.account_errors = {}


def test_second_start_reads_no_unchanged_turns_and_writes_nothing(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, records=('one', 'two'))
    w.svc.account_records()
    assert w.reads == w.paths
    inputs = list(w.input_reads)
    restart(w)
    trace = []
    w.svc.ledger.db.set_trace_callback(trace.append)
    w.svc.account_records()
    assert w.reads == w.paths
    assert w.input_reads == inputs
    assert writes(trace) == []


def _launch(at, pool='synthetic-pool-q'):
    return SimpleNamespace(at=at, pool=pool, harness='fake', price=None, pane='%1', pid=1,
                           resume=None, argv=['fake'], version=None)


def test_failed_pane_death_marker_keeps_accounting_and_retries(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    launch = _launch(AT.isoformat())
    pane = Pane(launch.pane, 'Crew', 'dead-worker', launch.pid, 80, 24, False, True, 7, int(AT.timestamp()))
    seat = SimpleNamespace(name='dead-worker', trusted=w.seat.trusted.with_name('dead-worker'),
                           spec=SimpleNamespace(up=True), session=w.seat.session, retiring=lambda: None,
                           pane=lambda: pane, launched=lambda: launch, record=lambda: None)
    marker = seat.trusted / pane_death.FILE
    marker.mkdir(parents=True)  # a directory at the marker path prevents replacement
    reading = SimpleNamespace(selected=[seat], contexts={seat.name: {'launched': launch}})
    sources = {seat.name: [], w.seat.name: list(w.owned.values())}
    w.svc.resolved_seats = lambda: [seat, w.seat]

    try:
        w.svc.account_records(source_records=sources)
        assert w.svc.ledger.ingested(w.paths[0]) == 1
        assert watch._pane_exits(reading, {seat.name: pane}) == {seat.name: (AT, 7)}
        assert not watch._pane_exits(reading, {})

        w.svc.account_records(source_records=sources)
        marker.rmdir()
        w.svc.account_records(source_records=sources)
        assert watch._pane_exits(reading, {}) == {seat.name: (AT, 7)}
        (death,) = [fields for event, fields in w.logs if event == 'pane-dead']
        assert death['seat'] == seat.name and death['status'] == 7
        (error,) = [fields for event, fields in w.logs if event == 'error']
        assert error['step'] == 'pane-death' and error['seat'] == seat.name and pane_death.FILE in error['error']
    finally:
        w.svc.ledger.close()


def _rows(db, table):
    return db.execute(f'SELECT * FROM {table}').fetchall()


def _old_business(db, current, finished):
    keys = (str(current), str(finished))
    turns = db.execute(
        'SELECT record, turn, seat, pool, written, cost FROM turns WHERE record IN (?,?) ORDER BY record, turn',
        keys).fetchall()
    costs = db.execute(
        'SELECT record, turn, recorded_cost, cost_basis, valid FROM turn_costs WHERE record IN (?,?) ORDER BY record, turn',
        keys).fetchall()
    tools = db.execute(
        'SELECT record, turn, name, seconds, failed FROM tool_calls WHERE record IN (?,?) ORDER BY record, turn, name',
        keys).fetchall()
    return turns, costs, tools


def test_finished_records_stay_when_the_seat_is_down_or_relaunched(tmp_path, monkeypatch):
    """A checkpoint follows the launch joined to the record, not the seat's current one."""
    w = world(tmp_path, monkeypatch, records=('current-q', 'finished-q'))
    current, finished = w.paths
    old_at = (AT - timedelta(hours=2)).isoformat()
    current_at = (AT - timedelta(minutes=1)).isoformat()
    w.journal.write_text(''.join(json.dumps(row) + '\n' for row in (
        dict(record=str(finished), at=old_at, pool='synthetic-pool-q', harness='fake'),
        dict(record=str(current), at=current_at, pool='synthetic-pool-q', harness='fake', price=None),
    )))
    w.seat.launched = lambda: _launch(current_at)
    w.seat.record = lambda: current
    w.svc.account_records()
    assert w.reads == [current, finished]
    restart(w)
    w.seat.launched = lambda: None
    trace = []
    w.svc.ledger.db.set_trace_callback(trace.append)
    kept = {table: _rows(w.svc.ledger.db, table) for table in ('turns', 'turn_costs', 'tool_calls')}
    w.svc.account_records()
    assert w.reads == [current, finished], 'a down seat re-reads no finished record'
    assert writes(trace) == []
    assert {table: _rows(w.svc.ledger.db, table) for table in kept} == kept
    # A newer launch joins a different record. The two older records stay checkpointed.
    fresh = current.with_name('fresh-q')
    fresh.write_text('synthetic fresh-q first revision')
    relaunched_at = AT.isoformat()
    w.journal.write_text(w.journal.read_text() + json.dumps(dict(
        record=str(fresh), at=relaunched_at, pool='synthetic-pool-r', harness='fake', price=None)) + '\n')
    w.sources[fresh] = (replace(w.sources[current][0]), [turn(0)])
    w.inputs[fresh] = []
    w.owned[fresh] = record_owner.Owned(fresh, 'fake', w.owned[current].reader)
    w.seat.launched = lambda: _launch(relaunched_at, 'synthetic-pool-r')
    w.seat.record = lambda: fresh
    restart(w)
    before = _old_business(w.svc.ledger.db, current, finished)
    w.svc.account_records()
    assert w.reads == [current, finished, fresh]
    assert _old_business(w.svc.ledger.db, current, finished) == before
    # Whitespace in the journal is not a new launch.
    restart(w)
    w.journal.write_text(w.journal.read_text() + '\n')
    seen = list(w.reads)
    w.svc.account_records()
    assert w.reads == seen


def test_a_price_joined_to_one_record_rereads_only_that_record(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, records=('current-q', 'finished-q'))
    current, finished = w.paths
    old_at = (AT - timedelta(hours=2)).isoformat()
    current_at = (AT - timedelta(minutes=1)).isoformat()
    rows = [
        dict(record=str(finished), at=old_at, pool='synthetic-pool-q', harness='fake'),
        dict(record=str(current), at=current_at, pool='synthetic-pool-q', harness='fake', price=None),
    ]
    w.journal.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    w.seat.launched = lambda: _launch(current_at)
    w.svc.account_records()
    rows[0]['price'] = {'synthetic-model-q': 1}
    w.journal.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    restart(w)
    w.svc.account_records()
    assert w.reads == [current, finished, finished]


def test_grown_record_rechecks_all_its_turns_and_writes_only_the_new_one(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, records=('one', 'two'))
    w.svc.account_records()
    restart(w)
    path = w.paths[0]
    path.write_text(path.read_text() + '\nnext synthetic turn')
    w.sources[path] = (replace(w.sources[path][0], turns=2), [turn(0), turn(1)])
    before = w.svc.ledger.db.total_changes
    _, notices, _ = w.svc.account_records()
    assert w.reads == [*w.paths, path]
    assert [data['turns'] for event, data in notices if event == 'ledger'] == [1]
    assert w.svc.ledger.ingested(path) == 2
    assert w.svc.ledger.db.total_changes > before
    restart(w)
    trace = []
    w.svc.ledger.db.set_trace_callback(trace.append)
    w.svc.account_records()
    assert w.reads == [*w.paths, path]
    assert writes(trace) == []


@pytest.mark.parametrize('change', ['mtime', 'replacement', 'reader', 'launch', 'missing', 'malformed'])
def test_new_or_missing_evidence_requires_reading(tmp_path, monkeypatch, change):
    w = world(tmp_path, monkeypatch)
    path = w.paths[0]
    w.svc.account_records()
    if change == 'mtime':
        s = path.stat()
        os.utime(path, ns=(s.st_atime_ns, s.st_mtime_ns + 1))
    elif change == 'replacement':
        other = path.with_name('replacement')
        other.write_bytes(path.read_bytes())
        other.replace(path)
    elif change == 'reader':
        keys = reader_stamp.readers()
        keys['fake'] = ['synthetic changed reader implementation']
        monkeypatch.setattr(reader_stamp, 'readers', lambda root=None: keys)
    elif change == 'launch':
        event = json.loads(w.journal.read_text())
        event['price'] = {'synthetic-model-q': 1}
        w.journal.write_text(json.dumps(event) + '\n')
    elif change == 'missing':
        w.svc.ledger.db.execute('DELETE FROM record_accounting')
        w.svc.ledger.db.commit()
    else:
        w.svc.ledger.db.execute("UPDATE record_accounting SET evidence='malformed'")
        w.svc.ledger.db.commit()
    restart(w)
    w.svc.account_records()
    assert w.reads == [path, path]


def test_old_writer_invalidates_record_checkpoint(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    w.svc.account_records()
    with sqlite3.connect(w.svc.ledger.path) as old:
        old.execute('UPDATE turns SET written=0')
    restart(w)
    w.svc.account_records()
    assert w.reads == [w.paths[0], w.paths[0]]
    assert w.svc.ledger.db.execute('SELECT written FROM turns').fetchone() == (4,)


def test_failed_transaction_leaves_no_durable_checkpoint(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    def fail(*args, **kwargs):
        raise RuntimeError('synthetic replay failure')
    with monkeypatch.context() as patch:
        patch.setattr(type(w.svc.ledger.attributions), 'replay', fail)
        with pytest.raises(RuntimeError, match='synthetic replay failure'):
            w.svc.account_records()
    assert w.svc.ledger.db.execute('SELECT COUNT(*) FROM record_accounting').fetchone() == (0,)
    assert w.svc.ledger.db.execute('SELECT COUNT(*) FROM attribution_records').fetchone() == (0,)
    restart(w)
    w.svc.account_records()
    assert w.reads == [w.paths[0], w.paths[0]]


def test_record_growing_during_read_does_not_checkpoint_later_bytes(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    reader = next(iter(w.owned.values())).reader
    original = reader.read
    def read(path):
        result = original(path)
        path.write_text(path.read_text() + '\ngrew during read')
        return result
    monkeypatch.setattr(reader, 'read', read)
    w.svc.account_records()
    assert w.svc.ledger.db.execute('SELECT COUNT(*) FROM record_accounting').fetchone() == (0,)
    restart(w)
    w.svc.account_records()
    assert len(w.reads) == 2


def test_incomplete_accounting_is_retried_after_restart(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    path = w.paths[0]
    w.sources[path] = (replace(w.sources[path][0], turns=None), [turn(0)])
    w.svc.account_records()
    restart(w)
    w.svc.account_records()
    assert w.reads == [path, path]


def test_rechecking_identical_turns_does_not_rewrite_them(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    w.svc.account_records()
    trace = []
    w.svc.ledger.db.set_trace_callback(trace.append)
    assert w.svc.ledger.ingest(w.seat.name, '-', w.paths[0], [turn(0)], w.svc.store) == 0
    assert writes(trace) == []


@pytest.mark.parametrize('kind', ['unrelated', 'later', 'closing-later', 'affected'])
def test_restart_only_replays_records_affected_by_a_post(tmp_path, monkeypatch, kind):
    w = world(tmp_path, monkeypatch, records=('one', 'two'))
    other = SimpleNamespace(**{**vars(w.seat), 'name': 'synthetic-other-seat-q',
                               'home': w.seat.home.parent.parent / 'synthetic-other-seat-q' / 'home',
                               'trusted': w.seat.trusted.parent / 'synthetic-other-seat-q',
                               'record': lambda: w.paths[1]})
    other.home.mkdir(parents=True)
    other.trusted.mkdir(parents=True)
    owned = list(w.owned.values())
    monkeypatch.setattr(w.svc, 'resolved_seats', lambda: [w.seat, other])
    monkeypatch.setattr(record_owner, 'records', lambda seat: [owned[0] if seat is w.seat else owned[1]])
    if kind == 'closing-later':
        w.svc.store.write(Message('synthetic-claimed-order', 'order', 'synthetic-operator',
                                  w.seat.name, AT - timedelta(seconds=2), 'synthetic work'))
        w.svc.store.write(Message('synthetic-claim', 'ack', w.seat.name, 'synthetic-operator',
                                  AT - timedelta(seconds=1), 'synthetic claim', re='synthetic-claimed-order'))
    w.svc.account_records()
    inputs = list(w.input_reads)
    message = Message('synthetic-added-message', 'order', 'synthetic-operator',
                      'synthetic-unrelated-seat' if kind == 'unrelated' else w.seat.name,
                      AT + timedelta(seconds=30 if kind == 'later' else 1), 'synthetic work')
    if kind == 'closing-later':
        message = replace(message, kind='report', sender=w.seat.name, to='synthetic-operator',
                          at=AT + timedelta(seconds=30), re='synthetic-claimed-order')
    w.svc.store.write(message)
    restart(w)
    before = w.svc.ledger.db.total_changes
    w.svc.account_records()
    assert w.reads == w.paths
    assert w.input_reads == inputs + ([w.paths[0]] if kind == 'affected' else [])
    if kind != 'affected':
        assert w.svc.ledger.db.total_changes == before


def test_changed_attribution_input_is_replayed_after_restart(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    w.svc.account_records()
    w.paths[0].write_text('synthetic changed source')
    restart(w)
    w.svc.account_records()
    assert w.input_reads == [w.paths[0], w.paths[0]]


def test_failed_attribution_source_never_receives_a_certificate(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    w.inputs[w.paths[0]] = OSError('synthetic source unavailable')
    w.svc.account_records()
    assert w.svc.ledger.db.execute('SELECT COUNT(*) FROM attribution_records').fetchone() == (0,)
    restart(w)
    w.svc.account_records()
    assert w.input_reads == [w.paths[0], w.paths[0]]


def test_attribution_source_growing_during_read_is_not_certified(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    reader = type(next(iter(w.owned.values())).reader)
    original = reader.attribution_inputs
    def inputs(self, path):
        result = original(self, path)
        path.write_text(path.read_text() + '\ngrew during attribution')
        return result
    monkeypatch.setattr(reader, 'attribution_inputs', inputs)
    w.svc.account_records()
    assert w.svc.ledger.db.execute('SELECT COUNT(*) FROM attribution_records').fetchone() == (0,)
