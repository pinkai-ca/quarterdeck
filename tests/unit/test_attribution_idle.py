"""The no-change gate avoids graph construction without bypassing evidence repair."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from quarterdeck import attribution_cache, post
from quarterdeck.harness.fake import FakeHarness
from quarterdeck.harness.types import Input
from quarterdeck.ledger import Ledger
from tests.measure_status_service import identity as file_identity
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
            # A source stamp still forces a fresh read. Identical current input
            # evidence needs no graph; actual input/error/ledger/post changes do.
            assert len(graphs) == before + (change != 'source'), change
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


@pytest.mark.parametrize('failure', ['put', 'post-read', 'pending-query'])
def test_failed_replay_retries_cached_changed_source(tmp_path, monkeypatch, failure):
    store = post.Store(tmp_path)
    first = message(store, 'ord-first')
    second = message(store, 'ord-second')
    for i, order in enumerate((first, second)):
        message(store, f'rep-{i}', 'report', 'worker', 'issuer', 1, order.id)
    record = tmp_path / 'record'
    record.write_text('first synthetic source')
    current = [first]
    reads, graphs = [], []
    original = attribution_cache.Threads

    def inputs(self, path):
        reads.append(path)
        return [Input(post.typed(current), AT + timedelta(seconds=11))]

    def graph(store):
        graphs.append(True)
        return original(store)

    monkeypatch.setattr(FakeHarness, 'attribution_inputs', inputs)
    monkeypatch.setattr(attribution_cache, 'Threads', graph)
    bindings = {str(record): {'fake'}}
    with Ledger(tmp_path / 'ledger.sqlite') as book:
        book.ingest('worker', 'pool', record, [turn()], store)
        assert book.attributions.replay(store, tmp_path, bindings) == 1
        assert book.attributions.details()[0]['orders'] == [first.id]
        measured = measurement_snapshot(book)
        record.write_text('second synthetic source')
        source_stamp = file_identity(record.stat())
        posts = store.all()
        current[:] = [second]
        with monkeypatch.context() as patch:
            def fail(*args):
                raise PermissionError('synthetic interrupted replay')
            if failure == 'put':
                patch.setattr(book.attributions, 'put', fail)
            elif failure == 'post-read':
                patch.setattr(store, 'all', fail)
            else:
                def execute(sql, *args):
                    if 'count(*)' in sql:
                        fail()
                    return book.db.execute(sql, *args)
                patch.setattr(book.attributions, 'db', SimpleNamespace(execute=execute))
            with pytest.raises(OSError):
                book.attributions.replay(store, tmp_path, bindings)
        assert reads == [record, record], 'new incoming evidence was cached before the interruption'
        before = len(graphs)
        assert book.attributions.replay(store, tmp_path, bindings) == 1
        assert book.attributions.details()[0]['orders'] == [second.id]
        assert measurement_snapshot(book) == measured
        assert file_identity(record.stat()) == source_stamp and store.all() == posts
        assert len(graphs) == before + 1 and reads == [record, record]
        before = len(graphs)
        assert book.attributions.replay(store, tmp_path, bindings) == 0
        assert len(graphs) == before and reads == [record, record]
