"""Synthetic immutable-post graphs; no native programs or delivery side effects."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from quarterdeck import post, thread_context


def graph(tmp_path):
    store = post.Store(tmp_path)
    clock = datetime(2025, 1, 1, tzinfo=timezone.utc)
    count = 0

    def add(kind, sender, to, body, parent=None, *, at=None):
        nonlocal count
        count += 1
        message = post.Message(f'{kind[:3]}-synthetic-{count:04x}', kind, sender, to,
                               at or clock + timedelta(seconds=count), body,
                               re=parent.id if parent else None)
        store.write(message)
        return message

    return store, add


def test_amendments_and_own_question_bridge_have_distinct_delivery_eligibility(tmp_path):
    store, add = graph(tmp_path)
    root = add('order', 'issuer', 'worker', 'Build the ordinary widget.')
    question = add('question', 'worker', 'issuer', 'Which widget size?', root)
    private = add('question', 'issuer', 'other', 'Private coordination.', question)
    answer = add('answer', 'issuer', 'worker', 'Use the small size.', private)
    unused = add('question', 'worker', 'issuer', 'Unrelated historical question.', root)
    ack = add('ack', 'issuer', 'worker', 'Acknowledgment only.', root)
    selection = thread_context.select(store, 'worker')
    assert selection.incoming == [root, answer]
    assert selection.entries == (thread_context.Entry(root), thread_context.Entry(question, True),
                                 thread_context.Entry(answer))
    rendered = selection.render()
    assert rendered.index(root.body) < rendered.index(question.body) < rendered.index(answer.body)
    assert 'context only; no delivery or reply requested' in rendered
    assert post.typed([question]) not in rendered
    assert private.body not in rendered and unused.body not in rendered and ack.body not in rendered
    assert [message.to for message in selection.incoming] == ['worker', 'worker']
    assert not list((tmp_path / 'post' / 'attempts').glob('*.json')), 'selection creates no attempts'


def test_new_context_carries_old_consumed_amendment_once_and_resume_uses_only_supplied_consumption(tmp_path):
    store, add = graph(tmp_path)
    root = add('order', 'issuer', 'worker', 'Open task.')
    question = add('question', 'worker', 'issuer', 'Which option?', root)
    answer = add('answer', 'issuer', 'worker', 'Choose option two.', question)
    attempts = post.Attempts(tmp_path)
    for message in (root, answer):
        attempts.close(attempts.open(message, 'worker', '%1', 100, root.at.isoformat()), 'delivered')
    fresh = thread_context.select(store, 'worker', [root, answer, answer])
    assert fresh.incoming == [root, answer], 'old receipts cannot suppress a new context'
    assert fresh.render().count(answer.body) == 1
    resumed = thread_context.select(store, 'worker', [answer], consumed={root.id, answer.id})
    assert resumed.incoming == [root] and question.body not in resumed.render()
    assert attempts.latest() == {root.id: 'delivered', answer.id: 'delivered'}


def test_nested_open_roots_unread_and_queued_are_deduplicated_in_thread_order(tmp_path):
    store, add = graph(tmp_path)
    root = add('order', 'issuer', 'worker', 'First task.')
    second = add('order', 'issuer', 'worker', 'Second independent task.')
    nested = add('question', 'issuer', 'worker', 'Nested obligation.', root)
    amendment = add('answer', 'issuer', 'worker', 'Amended first task.', root)
    unread = add('report', 'issuer', 'worker', 'Independent unread information.')
    selection = thread_context.select(store, 'worker', [amendment, nested, unread, root, unread])
    assert selection.roots == (root, second, nested)
    assert selection.incoming == [root, nested, amendment, second, unread]
    assert len({entry.message.id for entry in selection.entries}) == len(selection.entries)
    assert selection == thread_context.select(store, 'worker', [unread, amendment, nested, unread])


def test_canonical_closure_and_explicit_links_define_roots(tmp_path):
    store, add = graph(tmp_path)
    closed = add('order', 'issuer', 'worker', 'Completed task.')
    add('report', 'worker', 'issuer', 'Done.', closed)
    omitted = add('answer', 'issuer', 'worker', 'Old amendment.', closed)
    question = add('question', 'issuer', 'worker', 'Answered question.')
    add('answer', 'worker', 'issuer', 'Answer.', question)
    root = add('order', 'issuer', 'worker', 'Current task.')
    add('report', 'other', 'issuer', 'Foreign report cannot close.', root)
    unrelated = add('answer', 'issuer', 'worker', f'Mentions {root.id} without a reply link.')
    selection = thread_context.select(store, 'worker')
    assert selection.roots == (root,) and selection.incoming == [root]
    assert omitted.body not in selection.render() and unrelated.body not in selection.render()


def test_equal_timestamp_thread_members_use_immutable_id_order(tmp_path):
    store, add = graph(tmp_path)
    root = add('order', 'issuer', 'worker', 'Task.')
    later = add('answer', 'issuer', 'worker', 'Second amendment.', root)
    earlier = replace(later, id='ans-synthetic-0000', body='First amendment.')
    store.write(earlier)
    assert thread_context.select(store, 'worker').incoming == [root, earlier, later]
