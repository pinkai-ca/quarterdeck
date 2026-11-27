"""Narrow lookups over a synthetic immutable post store."""

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
import fcntl
import sqlite3
from unittest.mock import Mock

import pytest

from quarterdeck import fleet_post, post, post_index


def filed(store, kind, serial, *, to='ada', re=None, date=26):
    at = datetime(2026, 11, date, tzinfo=timezone.utc)
    message = post.Message(f'{kind[:3]}-{at:%Y%m%d-%H%M}-desk-{serial:06x}',
                           kind, 'desk', to, at, f'message {serial}', re)
    store._write(message)
    return message


def test_exact_lookup_indexes_old_and_backdated_messages_without_reinventory(tmp_path, monkeypatch):
    store = post.Store(tmp_path / 'runtime')
    root = filed(store, 'order', 1, to='desk')
    child = filed(store, 'order', 2, to='other', re=root.id)
    late = filed(store, 'report', 3, re=root.id)
    filed(store, 'report', 4, re=child.id)
    filed(store, 'report', 5, re=root.short)
    monkeypatch.setattr(post, 'POST_TIMESTAMP_TICK_NS', 0)
    with post_index.MessageIndex(store) as index:
        assert index.replies(root.id) == [child, late]
        assert index.replies(root.short) != index.replies(root.id)
        assert index.has_short(root.short)

        before = filed(store, 'report', 6, re=root.id, date=1)
        with monkeypatch.context() as patch:
            patch.setattr(index, '_update', Mock(wraps=index._update))
            assert index.replies(root.id) == [before, child, late]
            assert index._update.call_args.kwargs == {'inventory': False}
            index._update.reset_mock()
            assert index.replies(root.id) == [before, child, late]
            index._update.assert_not_called()

    with post_index.MessageIndex(store) as reopened:
        monkeypatch.setattr(reopened, '_update', Mock(side_effect=AssertionError('warm inventory')))
        assert reopened.replies(root.id) == [before, child, late]


def test_recipient_query_and_read_only_store_use_separate_cache(tmp_path, monkeypatch):
    store = post.Store(tmp_path / 'crew' / 'runtime')
    declaration = store.dir.parent.parent.parent / 'ship.toml'
    declaration.parent.mkdir()
    declaration.touch()
    expected = filed(store, 'order', 1, to='ada')
    other = filed(store, 'order', 2, to='other')
    cache = tmp_path / 'view.sqlite3'
    with post_index.MessageIndex(store, cache=cache) as index:
        assert index.for_seat('ada') == [expected]
        index._raw = Mock(wraps=index._raw)
        assert index.for_seat('other') == [other]
        assert [call.args[1] for call in index._raw.call_args_list] == [other.id + '.json']
    assert cache.is_file()
    assert not (store.dir.parent / post_index.CACHE).exists()
    assert not any(b'message 1' in path.read_bytes() for path in tmp_path.glob('view.sqlite3*'))


def test_exact_lookup_includes_context_only_roots(tmp_path):
    store = post.Store(tmp_path / 'crew' / 'runtime')
    declaration = store.dir.parent.parent.parent / 'ship.toml'
    declaration.parent.mkdir()
    declaration.touch()
    root = filed(store, 'order', 1, to='ada@crew')
    raw = (store.dir / (root.id + '.json')).read_bytes()
    imported = dict(format=1, origin='other', sha256=fleet_post.digest(raw), context=True)
    fleet_post.publish(fleet_post.import_path(store.dir.parent.parent, root.id), fleet_post.encoded(imported))

    with post_index.MessageIndex(store) as index:
        assert index.for_seat('ada') == []
        assert index.get(root.id) == replace(root, sender='desk@other', to='ada')
        assert index.get(root.short) is None
        assert index.get('ord-missing-id') is None


def test_unavailable_index_is_distinct_from_a_missing_row(tmp_path):
    store = post.Store(tmp_path / 'runtime')
    order = filed(store, 'order', 1)
    cache = tmp_path / 'absent' / 'index'
    with post_index.MessageIndex(store, cache=cache) as index:
        for read in (lambda: index.replies(order.id), lambda: index.for_seat('ada'), lambda: index.get(order.id)):
            with pytest.raises(post_index.IndexUnavailable):
                read()


@pytest.mark.parametrize('damage', ['file', 'schema', 'snapshot', 'certification', 'missing snapshot'])
def test_damaged_index_rebuilds_from_immutable_store(tmp_path, damage):
    store = post.Store(tmp_path / 'runtime')
    order = filed(store, 'order', 1, to='desk')
    reply = filed(store, 'report', 2, re=order.id)
    cache = tmp_path / 'index.sqlite3'
    with post_index.MessageIndex(store, cache=cache) as index:
        assert index.replies(order.id) == [reply]
    before = {path.name: path.read_bytes() for path in store.dir.iterdir()}

    if damage == 'file':
        cache.write_bytes(b'invalid SQLite header' * 10)
    else:
        with closing(sqlite3.connect(cache)) as db, db:
            db.execute('UPDATE messages SET ref = NULL WHERE name = ?', (reply.id + '.json',))
            if damage == 'schema':
                db.execute('DROP INDEX message_recipient')
            elif damage == 'snapshot':
                db.execute("UPDATE snapshot SET stamp = 'invalid generation'")
            elif damage == 'certification':
                stamp = post_index._stamp(store.dir.stat())
                db.execute('UPDATE snapshot SET certified_at = ?', (max(stamp[2:]) + 1,))
            else:
                db.execute('DELETE FROM snapshot')

    with post_index.MessageIndex(store, cache=cache) as recovered:
        assert recovered.replies(order.id) == store.replies(order)
        assert recovered.get(order.id) == order
        assert recovered.for_seat('ada') == store.for_seat('ada')
    with closing(sqlite3.connect(cache)) as db:
        assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert db.execute('SELECT name FROM messages ORDER BY name').fetchall() == [
            (message.id + '.json',) for message in (order, reply)
        ]
    assert {path.name: path.read_bytes() for path in store.dir.iterdir()} == before


def test_cache_recovery_leaves_the_file_in_place_while_a_reader_holds_the_lock(tmp_path):
    store = post.Store(tmp_path / 'runtime')
    order = filed(store, 'order', 1)
    cache = tmp_path / 'index.sqlite3'
    damage = b'invalid SQLite header' * 10
    cache.write_bytes(damage)

    with open(str(cache) + '.lock', 'wb') as peer:
        fcntl.flock(peer, fcntl.LOCK_SH)
        with post_index.MessageIndex(store, cache=cache) as index:
            with pytest.raises(post_index.IndexUnavailable):
                index.get(order.id)
        assert cache.read_bytes() == damage

    with post_index.MessageIndex(store, cache=cache) as index:
        assert index.get(order.id) == order


def test_busy_sqlite_does_not_trigger_cache_recovery(tmp_path, monkeypatch):
    store = post.Store(tmp_path / 'runtime')
    order = filed(store, 'order', 1)
    cache = tmp_path / 'index.sqlite3'
    with post_index.MessageIndex(store, cache=cache) as index:
        assert index.get(order.id) == order
        replace_cache = Mock(wraps=index._replace_cache)
        monkeypatch.setattr(index, '_replace_cache', replace_cache)
        with closing(sqlite3.connect(cache)) as peer:
            peer.execute('BEGIN IMMEDIATE')
            with pytest.raises(post_index.IndexUnavailable) as unavailable:
                index.get(order.id)
            assert unavailable.value.__cause__.sqlite_errorcode == sqlite3.SQLITE_BUSY
        replace_cache.assert_not_called()
        assert index.get(order.id) == order


@pytest.mark.parametrize('same_tick', [False, True])
def test_append_between_event_drain_and_stamp_is_visible_to_both_readers(tmp_path, monkeypatch, same_tick):
    store = post.Store(tmp_path / 'runtime')
    order = filed(store, 'order', 1)
    directory_stamp = post_index._stamp(store.dir.stat())
    tick = post.POST_TIMESTAMP_TICK_NS
    clock = max(directory_stamp[2:]) + (1 if same_tick else 2 * tick)
    generation = 0
    stamp = post_index._stamp

    def stamped(info):
        value = stamp(info)
        if value[:2] == directory_stamp[:2]:
            return (*directory_stamp[:2], directory_stamp[2] + generation, directory_stamp[3])
        return value

    monkeypatch.setattr(post_index, '_stamp', stamped)
    monkeypatch.setattr(post_index.time, 'time_ns', lambda: clock)

    with post_index.MessageIndex(store) as index:
        assert index.replies(order.id) == []
        names = index.changes.names
        reply = None

        def drain():
            nonlocal generation, reply
            pending = names()
            if reply is None:
                assert pending == set()
                reply = filed(store, 'report', 2, re=order.id, date=1)
                generation = 0 if same_tick else 1
            return pending

        monkeypatch.setattr(index.changes, 'names', drain)
        assert index.replies(order.id) == [reply]

    with post_index.MessageIndex(store) as reopened:
        if not same_tick:
            monkeypatch.setattr(reopened, '_update', Mock(side_effect=AssertionError('warm inventory')))
        assert reopened.replies(order.id) == [reply]


def test_unstable_generation_falls_back_and_keeps_last_committed_snapshot(tmp_path, monkeypatch):
    store = post.Store(tmp_path / 'runtime')
    order = filed(store, 'order', 1)
    cache = tmp_path / 'index.sqlite3'
    with post_index.MessageIndex(store, cache=cache) as index, closing(sqlite3.connect(cache)) as db:
        assert index.replies(order.id) == []
        snapshot = db.execute('SELECT * FROM snapshot').fetchall()
        replies = []
        names = index.changes.names
        stamp = post_index._stamp
        directory = stamp(store.dir.stat())[:2]

        def drain():
            pending = names()
            replies.append(filed(store, 'report', len(replies) + 2, re=order.id))
            return pending

        def stamped(info):
            value = stamp(info)
            return (*value[:2], len(replies), 0) if value[:2] == directory else value

        with monkeypatch.context() as patch:
            patch.setattr(post_index, '_stamp', stamped)
            patch.setattr(index.changes, 'names', drain)
            with pytest.raises(post_index.IndexUnavailable):
                index.replies(order.id)
        assert db.execute('SELECT * FROM snapshot').fetchall() == snapshot
        assert index.replies(order.id) == store.replies(order) == replies
