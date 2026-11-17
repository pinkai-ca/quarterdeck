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


# -- as far as the reader sent or received it ---------------------------------------------


def scoped_thread():
    return [message('order-root', sender='lead', to='builder'),
            message('copy-for-peer', parent='order-root', moment=1, sender='lead', to='peer'),
            message('report-from-peer', parent='copy-for-peer', moment=2, sender='peer', to='lead', kind='report'),
            message('report-from-builder', parent='order-root', moment=3, sender='builder', to='lead', kind='report')]


def test_qd_thread_keeps_only_what_the_reader_sent_or_received_and_names_the_rest_by_id(tmp_path):
    # law: context-is-cache -- one command prints the thread whole, as far as the seat sent or received it
    messages = scoped_thread()
    store = store_posts(tmp_path, messages)
    reading = post_threads.whole(store, 'report-from-builder', 'builder')
    assert [m.id for m in reading.messages] == ['order-root', 'report-from-builder']
    assert reading.others == ('order copy-for-peer', 'report report-from-peer')
    rendered = post_threads.render(reading)
    for other in messages[1:3]:
        assert other.body not in rendered and other.subject not in rendered, 'no body, no subject'
        assert rendered.count(other.id) == 1, 'one line naming that it exists'
        line, = [line for line in rendered.splitlines() if other.id in line]
        assert line == f'qd > {other.kind} {other.id} {post_threads.NOT_YOURS}' and f'from {other.sender}' not in line
    assert rendered.count(messages[0].body) == 1 and rendered.count(messages[3].body) == 1
    assert rendered.splitlines()[0].endswith('; 2 more in it, named below by id only')
    lead = post_threads.render(post_threads.whole(store, 'order-root', 'lead'))
    assert all(lead.count(m.body) == 1 for m in messages), 'the lead sent or received every one of them'
    assert post_threads.NOT_YOURS not in lead


def test_the_index_lists_only_the_seats_own_messages_and_no_foreign_subject(tmp_path):
    messages = scoped_thread()
    store = store_posts(tmp_path, messages)
    [row] = post_threads.index_rows(store, (messages[0],), (), frozenset(), seat='builder')
    assert row.amendments == ('report-from-builder',) and row.subject == 'Synthetic subject for order-root'
    [peer] = post_threads.index_rows(store, (), (messages[1],), frozenset(), seat='peer')
    # copy-for-peer is the peer's open message (active), not an amendment of itself; its report amends it.
    assert peer.id == 'order-root' and peer.amendments == ('report-from-peer',)
    assert peer.active == ('copy-for-peer',)
    assert peer.subject is None, 'a root the seat neither sent nor received gives no subject'
    [unscoped] = post_threads.index_rows(store, (messages[0],), (), frozenset())
    assert len(unscoped.amendments) == 3, 'without a seat the old reading is unchanged'


def test_a_delivered_recovery_continuation_is_labelled_and_an_unread_one_is_not(tmp_path):
    from quarterdeck.harness.pi_thinking import Client as PiThinking
    root = message('order-root', sender='lead', to='builder')
    continuation = post.Message('report-continued', 'report', post.SHIP, 'builder',
                                datetime(2020, 1, 1, 0, 0, 5, tzinfo=timezone.utc), PiThinking.continuation,
                                re='order-root')
    store = store_posts(tmp_path, [root, continuation])
    unread = post_threads.render(post_threads.whole(store, 'order-root', 'builder'))
    assert PiThinking.continuation in unread and post_threads.CONTINUATION_LABEL not in unread
    attempts = post.Attempts(store.dir.parent.parent)
    attempts.close(attempts.open(continuation, 'builder', '%synthetic', 1234, 'synthetic-launch'), 'delivered')
    reading = post_threads.whole(store, 'order-root', 'builder')
    assert reading.continuations == ('report-continued',)
    rendered = post_threads.render(reading)
    assert rendered.count(PiThinking.continuation) == 1, 'labelled, never withheld'
    lines = rendered.splitlines()
    assert lines[lines.index(post_threads.CONTINUATION_LABEL) + 1].startswith('qd > report from ship')
    other = post_threads.render(post_threads.whole(store, 'order-root', 'lead'))
    assert post_threads.CONTINUATION_LABEL not in other, 'only for the seat it was delivered to'


def grading_thread(closed=300):
    """The shape of a long thread a hand grades in: a root the hand never had open, many grading orders to the hand
    each re the root and each closed by its report, and one open order with an ack and a correction below it."""
    messages = [message('order-root', sender='issuer', to='lead')]
    for n in range(closed):
        messages += [message(f'grade-{n:04d}', parent='order-root', moment=2 * n + 1, sender='lead', to='hand'),
                     message(f'report-{n:04d}', parent=f'grade-{n:04d}', moment=2 * n + 2, sender='hand', to='lead',
                             kind='report')]
    late = 2 * closed + 1
    messages += [message('grade-open', parent='order-root', moment=late, sender='lead', to='hand'),
                 message('ack-open', parent='grade-open', moment=late + 1, sender='hand', to='lead', kind='ack'),
                 message('correction-open', parent='grade-open', moment=late + 2, sender='lead', to='hand'),
                 message('answer-to-correction', parent='correction-open', moment=late + 3, sender='hand', to='lead',
                         kind='answer')]
    return messages


def test_a_line_names_only_what_amends_its_open_messages_not_the_rest_of_the_thread(tmp_path):
    messages = grading_thread()
    store = store_posts(tmp_path, messages)
    by_name = {item.id: item for item in messages}
    open_ = (by_name['grade-open'], by_name['correction-open'])
    [row] = post_threads.index_rows(store, open_, (), frozenset(), seat='hand')
    assert row.id == 'order-root' and set(row.active) == {'grade-open', 'correction-open'}
    # What descends from an open message, at any depth: its ack, the correction and its answer. No closed grading
    # order or report, though each is the hand's and each is re the root; qd thread prints those.
    assert row.amendments == ('ack-open', 'correction-open', 'answer-to-correction')


def test_a_reply_to_the_root_counts_only_where_the_root_is_itself_open(tmp_path):
    messages = [message('order-root', sender='lead', to='builder'),
                message('reply-to-root', parent='order-root', moment=1, sender='builder', to='lead', kind='report')]
    store = store_posts(tmp_path, messages)
    [row] = post_threads.index_rows(store, (messages[0],), (), frozenset(), seat='builder')
    assert row.amendments == ('reply-to-root',), 'the root is open: what answers it amends it'
    later = message('later-order', parent='order-root', moment=2, sender='lead', to='builder')
    store.write(later)
    [row] = post_threads.index_rows(store, (later,), (), frozenset(), seat='builder')
    assert row.amendments == (), 'the root is not open here: a reply to it is the thread history, not an amendment'


def view_posts(tmp_path, messages):
    """A seat's view of the post as the service writes it: its own messages, its shorts and its account of refusals."""
    from quarterdeck import post_view
    store = store_posts(tmp_path, messages)
    (store.dir.parent / post_view.SHORTS).write_text('')
    (store.dir.parent / post_view.REFUSED).write_text('{"journal": "read", "withheld": [], "told": [], "senders": {}}')
    return store


def test_a_view_prints_the_seats_part_under_an_ancestor_it_lacks_and_names_that_ancestor_by_id(tmp_path):
    # law: context-is-cache -- in a jailed view, as far as the seat sent or received it, never a refusal
    graded = message('order-graded', parent='order-unseen-root', moment=1)
    ack = message('ack-taken', parent=graded.id, moment=2, sender='builder', to='lead', kind='ack')
    sibling = message('order-sibling', parent='order-unseen-root', moment=3)
    store = view_posts(tmp_path, [graded, ack, sibling, message('order-unrelated', moment=4)])
    for reference in (graded.id, ack.id, sibling.id):
        reading = post_threads.whole(store, reference, 'builder')
        assert reading.root == 'order-unseen-root' and reading.unshown == ('order-unseen-root',)
        assert [item.id for item in reading.messages] == [graded.id, ack.id, sibling.id] and reading.others == ()
        rendered = post_threads.render(reading)
        assert f'qd > order-unseen-root {post_threads.NOT_SHOWN}\n' in rendered
        assert all(rendered.count(item.body) == 1 for item in (graded, ack, sibling))
        assert 'order-unrelated' not in rendered and rendered.endswith('qd > end of thread order-unseen-root\n')


def test_outside_a_view_the_same_missing_ancestor_is_still_refused(tmp_path):
    store = store_posts(tmp_path, [message('order-graded', parent='order-unseen-root')])
    with pytest.raises(ShipError, match='thread ancestry is incomplete: missing order-unseen-root'):
        post_threads.whole(store, 'order-graded', 'builder')


def test_a_view_still_refuses_a_cycle(tmp_path):
    store = view_posts(tmp_path, [message('order-one', parent='answer-two'),
                                  message('answer-two', parent='order-one', kind='answer')])
    with pytest.raises(ShipError, match='thread ancestry has a cycle'):
        post_threads.whole(store, 'order-one', 'builder')
