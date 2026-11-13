"""Invented post graphs; full thread reading is a read, never a receipt."""

from datetime import datetime, timedelta, timezone

import pytest

from quarterdeck import post, post_threads
from quarterdeck.ship import ShipError


def message(name, *, parent=None, moment=0, sender='lead', to='builder', kind='order'):
    return post.Message(name, kind, sender, to, datetime(2020, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=moment),
                        'Invented complete body for ' + name, re=parent, subject='Synthetic subject for ' + name)


def store_posts(tmp_path, messages):
    store = post.Store(tmp_path / 'runtime')
    for item in messages:
        store.write(item)
    return store


def test_command_reading_contains_ancestors_and_every_reply_once(tmp_path):
    # law: context-is-cache -- a reply lookup must not omit its root, siblings or later amendments
    messages = [message('order-root'),
                message('question-bridge', parent='order-root', moment=1, sender='builder', to='lead', kind='question'),
                message('answer-reply', parent='question-bridge', moment=2, kind='answer'),
                message('report-sibling', parent='order-root', moment=3, kind='report'),
                message('order-unrelated', moment=4)]
    store = store_posts(tmp_path, messages)
    before = {path: path.read_bytes() for path in store.dir.iterdir()}
    reading = post_threads.whole(store, 'answer-reply', 'builder')
    assert reading.root == 'order-root'
    assert [item.id for item in reading.messages] == [item.id for item in messages[:-1]]
    rendered = post_threads.render(reading)
    assert all(rendered.count(item.body) == 1 for item in messages[:-1])
    assert messages[-1].body not in rendered
    assert rendered.endswith('qd > end of thread order-root\n')
    assert {path: path.read_bytes() for path in store.dir.iterdir()} == before
    assert not (store.dir.parent / 'attempts').exists()


def test_parents_precede_children_even_when_timestamps_move_backwards(tmp_path):
    root = message('order-root', moment=20)
    child = message('answer-child', parent=root.id, moment=1, kind='answer')
    grandchild = message('report-grandchild', parent=child.id, moment=0, kind='report')
    reading = post_threads.whole(store_posts(tmp_path, [root, child, grandchild]), grandchild.id, 'builder')
    assert [item.id for item in reading.messages] == [root.id, child.id, grandchild.id]


def test_whole_thread_preserves_original_body_whitespace(tmp_path):
    # law: context-is-cache -- even the end of a stored body is part of the whole thread
    original = message('order-exact')
    original = post.Message(**{**original.__dict__, 'body': '  Invented indented text.\n\n   '})
    reading = post_threads.whole(store_posts(tmp_path, [original]), original.id, 'builder')
    assert original.body in post_threads.render(reading)
    assert post.typed([original]) == post.typed([original], whole=False), 'ordinary transport matching keeps its historical form'


def test_missing_ancestor_is_explicit_instead_of_a_partial_thread(tmp_path):
    store = store_posts(tmp_path, [message('answer-orphan', parent='order-missing')])
    with pytest.raises(ShipError, match='thread ancestry is incomplete'):
        post_threads.whole(store, 'answer-orphan', 'builder')


def test_cycle_is_explicit_instead_of_an_infinite_or_partial_read(tmp_path):
    store = store_posts(tmp_path, [message('order-one', parent='answer-two'),
                                   message('answer-two', parent='order-one', kind='answer')])
    with pytest.raises(ShipError, match='thread ancestry has a cycle'):
        post_threads.whole(store, 'order-one', 'builder')


def test_index_has_metadata_and_amendment_ids_without_ordinary_bodies(tmp_path):
    root = message('order-root')
    root = post.Message(**{**root.__dict__, 'due': 'tomorrow 12:00'})
    answer = message('answer-amendment', parent=root.id, moment=1, kind='answer')
    report = message('report-standalone', moment=2, kind='report')
    rows = post_threads.index_rows(store_posts(tmp_path, [root, answer, report]), (root,),
                                   (root, answer, report), frozenset())
    assert [row.id for row in rows] == [root.id, report.id]
    assert rows[0].amendments == (answer.id,)
    assert rows[0].due == 'tomorrow 12:00'
    assert rows[0].sender == root.sender and rows[0].subject == root.subject
    assert root.body not in repr(rows) and answer.body not in repr(rows)
