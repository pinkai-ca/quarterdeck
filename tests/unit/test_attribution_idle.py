"""The no-change gate avoids graph construction without bypassing evidence repair."""

from dataclasses import replace
from datetime import timedelta

import pytest

from quarterdeck import attribution_cache, post
from quarterdeck.harness.fake import FakeHarness
from quarterdeck.harness.types import Input
from quarterdeck.ledger import Ledger
from .test_order_attribution import AT, message, measurement_snapshot, turn


def test_idle_replay_does_not_rebuild_threads_and_all_change_routes_still_replay(tmp_path, monkeypatch):
    store = post.Store(tmp_path)
    order = message(store, 'ord-work')
    message(store, 'rep-close', 'report', 'worker', 'issuer', 1, order.id)
    record = tmp_path / 'record'
    record.write_text('synthetic source')
    source_reads, graphs = [], []
    original = attribution_cache.Threads

    def graph(store):
        graphs.append(True)
        return original(store)

    def inputs(self, path):
        source_reads.append(path)
        return [Input(post.typed([order]), AT + timedelta(seconds=11))]

    monkeypatch.setattr(attribution_cache, 'Threads', graph)
    monkeypatch.setattr(FakeHarness, 'attribution_inputs', inputs)
    bindings = {str(record): {'fake'}}
    with Ledger(tmp_path / 'ledger.sqlite') as book:
        initial = turn()
        book.ingest('worker', 'pool', record, [initial], store)
        replay = lambda: book.attributions.replay(store, tmp_path, bindings)
        assert replay() == 1
        for _ in range(5):
            assert replay() == 0
        assert len(graphs) == 1 and source_reads == [record]
        for change in ('post', 'refresh', 'new-turn', 'source', 'loss', 'restart', 'old-writer'):
            if change == 'post':
                message(store, 'que-related', 'question', 'worker', 'issuer', 12, order.id)
            elif change == 'refresh':
                book.ingest('worker', 'pool', record, [replace(initial, cost=3)], store)
            elif change == 'new-turn':
                book.ingest('worker', 'pool', record, [turn(1, 30, 40)], store)
            elif change == 'source':
                record.write_text('changed synthetic source')
            elif change == 'loss':
                record.unlink()
            elif change == 'restart':
                record.write_text('recovered synthetic source')
                book.attributions = attribution_cache.Attribution(book.db)
            else:
                book.db.execute('UPDATE turns SET cost=4 WHERE turn=0')
            before = len(graphs)
            measured = measurement_snapshot(book)
            replay()
            assert len(graphs) == before + 1, change
            assert measurement_snapshot(book) == measured
            assert book.attributions.details()[0]['orders'] == [order.id]
            if change != 'loss':
                before = len(graphs)
                assert replay() == 0 and len(graphs) == before


def test_failed_source_read_is_retried_without_a_fingerprint_change(tmp_path, monkeypatch):
    store = post.Store(tmp_path)
    order = message(store, 'ord-work')
    message(store, 'rep-close', 'report', 'worker', 'issuer', 1, order.id)
    record = tmp_path / 'record'
    record.write_text('unchanged source')
    calls = []

    def inputs(self, path):
        calls.append(path)
        if len(calls) == 1:
            raise OSError('synthetic temporary source failure')
        return [Input(post.typed([order]), AT + timedelta(seconds=11))]

    monkeypatch.setattr(FakeHarness, 'attribution_inputs', inputs)
    with Ledger(tmp_path / 'ledger.sqlite') as book:
        book.ingest('worker', 'pool', record, [turn()], store)
        bindings = {str(record): {'fake'}}
        assert book.attributions.replay(store, tmp_path, bindings) == 1
        assert book.attributions.details()[0]['orders'] == []
        assert book.attributions.replay(store, tmp_path, bindings) == 1
        assert book.attributions.details()[0]['orders'] == [order.id]
        assert book.attributions.replay(store, tmp_path, bindings) == 0
        assert calls == [record, record]


def test_failed_replay_retries_cached_changed_source(tmp_path, monkeypatch):
    store = post.Store(tmp_path)
    first = message(store, 'ord-first')
    second = message(store, 'ord-second')
    for i, order in enumerate((first, second)):
        message(store, f'rep-{i}', 'report', 'worker', 'issuer', 1, order.id)
    record = tmp_path / 'record'
    record.write_text('first synthetic source')
    current = [first]
    monkeypatch.setattr(FakeHarness, 'attribution_inputs', lambda self, path:
                        [Input(post.typed(current), AT + timedelta(seconds=11))])
    bindings = {str(record): {'fake'}}
    with Ledger(tmp_path / 'ledger.sqlite') as book:
        book.ingest('worker', 'pool', record, [turn()], store)
        assert book.attributions.replay(store, tmp_path, bindings) == 1
        assert book.attributions.details()[0]['orders'] == [first.id]
        measured = measurement_snapshot(book)
        record.write_text('second synthetic source')
        current[:] = [second]
        with monkeypatch.context() as patch:
            def fail(*args):
                raise OSError('synthetic interrupted verdict write')
            patch.setattr(book.attributions, 'put', fail)
            with pytest.raises(OSError):
                book.attributions.replay(store, tmp_path, bindings)
        assert book.attributions.replay(store, tmp_path, bindings) == 1
        assert book.attributions.details()[0]['orders'] == [second.id]
        assert measurement_snapshot(book) == measured
        assert book.attributions.replay(store, tmp_path, bindings) == 0
