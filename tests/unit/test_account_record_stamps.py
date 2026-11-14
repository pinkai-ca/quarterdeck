"""Restart accounting reuses committed observations, while grown sources are read fully."""

from dataclasses import replace
from datetime import timedelta
import os
import sqlite3
from types import SimpleNamespace

import pytest

from quarterdeck.ledger import Ledger
from quarterdeck import record_owner
from quarterdeck.post import Message
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


@pytest.mark.parametrize('change', ['mtime', 'replacement', 'framework', 'launch', 'missing', 'malformed'])
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
    elif change == 'framework':
        w.svc.status_framework = ['synthetic changed implementation']
    elif change == 'launch':
        w.journal.write_text(w.journal.read_text() + '\n')
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


@pytest.mark.parametrize('kind', ['unrelated', 'later', 'affected'])
def test_restart_only_replays_records_affected_by_a_post(tmp_path, monkeypatch, kind):
    w = world(tmp_path, monkeypatch, records=('one', 'two'))
    other = SimpleNamespace(**{**vars(w.seat), 'name': 'synthetic-other-seat-q',
                               'home': w.seat.home.parent.parent / 'synthetic-other-seat-q' / 'home',
                               'record': lambda: w.paths[1]})
    other.home.mkdir(parents=True)
    owned = list(w.owned.values())
    monkeypatch.setattr(w.svc, 'resolved_seats', lambda: [w.seat, other])
    monkeypatch.setattr(record_owner, 'records', lambda seat: [owned[0] if seat is w.seat else owned[1]])
    w.svc.account_records()
    inputs = list(w.input_reads)
    message = Message('synthetic-added-message', 'order', 'synthetic-operator',
                      'synthetic-unrelated-seat' if kind == 'unrelated' else w.seat.name,
                      AT + timedelta(seconds=30 if kind == 'later' else 1), 'synthetic work')
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
