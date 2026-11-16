"""The ledger shows each span of a record it could not read because it passed its read bound.

Every turn is accounted, or counted unattributed and reported (law: every-turn-accounted). A span the reader never
read is neither unless the ledger itself shows it: a row of its own with the seat, the record, the pool, its size, the
bound, when it was first seen past the bound and the turns counted before it. Its turns since are unknown, and no
figure is made for them. The table is additive: existing rows and readers are unchanged.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

from quarterdeck import ledger_reading
from quarterdeck.harness import record_file
from quarterdeck.ledger import Ledger

from .test_ledger import accounting_example, sql_writes

MiB = 1024 * 1024


def test_a_span_is_kept_with_its_figures_and_never_a_figure_for_its_turns(tmp_path):
    path, record = tmp_path / 'ledger.sqlite', tmp_path / 'record-z'
    with Ledger(path) as book:
        book.mark_unread_span(record, 'worker-z', 'pool-z', 130 * MiB, 128 * MiB, '2026-11-15T21:00:00+00:00')
        [span] = book.unread_spans()
        assert span == dict(record=str(record), seat='worker-z', pool='pool-z', size=130 * MiB, bound=128 * MiB,
                            passed_at='2026-11-15T21:00:00+00:00', seen_at='2026-11-15T21:00:00+00:00', counted=0,
                            turns_since=None)
        trace, before = [], book.db.total_changes
        book.db.set_trace_callback(trace.append)
        book.mark_unread_span(record, 'worker-z', 'pool-z', 130 * MiB, 128 * MiB, '2026-11-15T21:05:00+00:00')
        assert sql_writes(trace) == [] and book.db.total_changes == before, 'the same span: nothing written'
        book.db.set_trace_callback(None)
        book.mark_unread_span(record, 'worker-z', 'pool-z', 140 * MiB, 128 * MiB, '2026-11-15T21:10:00+00:00')
        [grown] = book.unread_spans()
        assert (grown['size'], grown['passed_at'], grown['seen_at']) == (
            140 * MiB, '2026-11-15T21:00:00+00:00', '2026-11-15T21:10:00+00:00'), 'when it passed stays'
        book.clear_unread_span(record)
        assert book.unread_spans() == []
    with Ledger.reading(path) as reading:
        assert reading.unread_spans() == []


def test_an_earlier_ledger_gains_the_table_and_keeps_its_rows_and_an_earlier_reader_reads_on(tmp_path):
    path, record = tmp_path / 'ledger.sqlite', tmp_path / 'record-z'
    with Ledger(path) as book:
        book.mark_record(record, 'worker-z', 'pool-z', 'synthetic unavailable measurement')
        book.db.execute('DROP TABLE unread_spans')  # the file as an earlier copy wrote it
        book.db.commit()
    with Ledger.reading(path) as reading:
        assert reading.unread_spans() == [], 'a reading of an earlier file: none, never an error'
    with Ledger(path) as book:  # the service opens it: the table is added, nothing else changes
        assert book.unread_spans() == []
        assert book.incomplete_records() == [(str(record), 'worker-z', 'pool-z', 'synthetic unavailable measurement')]
        book.mark_unread_span(record, 'worker-z', 'pool-z', 130 * MiB, 128 * MiB, '2026-11-15T21:00:00+00:00')
    # A reader of the earlier layout reads the tables it knows, as before.
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as earlier:
        assert earlier.execute('SELECT record, seat, pool, error FROM records').fetchall() == [
            (str(record), 'worker-z', 'pool-z', 'synthetic unavailable measurement')]
        assert earlier.execute('SELECT COUNT(*) FROM turns').fetchone() == (0,)


def test_the_service_keeps_a_span_past_the_bound_and_clears_it_when_read_whole(tmp_path, monkeypatch):
    svc, seat, record, turns, reads, at = accounting_example(tmp_path, monkeypatch, 2)
    svc.account()
    assert svc.ledger.ingested(record) == 2 and svc.ledger.unread_spans() == []
    harness = seat.harness
    read = harness.read

    def past(path):
        raise record_file.past_bound(path, 130 * MiB, 128 * MiB)
    monkeypatch.setattr(harness, 'read', past)
    record.write_text('synthetic record grown past its bound')
    svc.read_marks.clear(); svc.account_errors.clear()
    svc.account()
    [span] = svc.ledger.unread_spans()
    [(_, _, pool, error)] = svc.ledger.incomplete_records()
    assert 'record past the read bound' in error
    assert (span['record'], span['seat'], span['pool'], span['size'], span['bound'], span['counted']) == (
        str(record), 'worker-z', pool, 130 * MiB, 128 * MiB, 2), 'the pool as the accounting knows the record'
    assert span['passed_at'] and span['turns_since'] is None
    assert svc.ledger.ingested(record) == 2, 'the turns counted before stay; none is made up'

    monkeypatch.setattr(harness, 'read', read)
    record.write_text('synthetic record read whole again')
    svc.read_marks.clear(); svc.account_errors.clear()
    svc.account()
    assert svc.ledger.unread_spans() == [] and svc.ledger.incomplete_records() == []
    svc.ledger.close()


def test_qd_ledger_and_the_pool_reading_name_each_span(tmp_path):
    from types import SimpleNamespace
    path, record = tmp_path / 'ledger.sqlite', tmp_path / 'seats' / 'worker-z' / 'home' / 'record-z.jsonl'
    with Ledger(path) as book:
        book.mark_unread_span(record, 'worker-z', 'pool-z', 130 * MiB, 128 * MiB, '2026-11-15T21:00:00+00:00')
    pool = SimpleNamespace(name='pool-z', kind='subscription', provider='synthetic')
    with Ledger.reading(path) as reading:
        data = ledger_reading.collect(reading)
        assert data['unread_spans'][0]['size'] == 130 * MiB
        note = ('unread span: record-z.jsonl (worker-z, pool-z) · 130.0 MiB past its read bound of 128.0 MiB, first '
                'seen 2026-11-15T21:00:00+00:00; 0 turns counted before it, its turns since unknown')
        assert note in ledger_reading.render(data)
        rollup = ledger_reading.collect(reading, by='seat')
        assert note in ledger_reading.render(rollup)
    pools = dict(data, pools=[dict(name='pool-z', provider_name='Synthetic', kind='subscription', native_harness=None,
                                    unread_spans=1)], windows=[], usage_attempts=[], observed_walls=[],
                 usage_absent_reason=None)
    assert ledger_reading.render_pools(pools)[0].endswith(' · 1 unread span: turns unknown')
