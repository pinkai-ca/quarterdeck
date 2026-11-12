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


def test_exclusions_remove_incoming_and_context_bodies_without_closing_roots(tmp_path):
    store, add = graph(tmp_path)
    root = add('order', 'issuer', 'worker', 'Excluded root body.')
    question = add('question', 'worker', 'issuer', 'Excluded own question body.', root)
    answer = add('answer', 'issuer', 'worker', 'Visible amendment.', question)
    other = add('answer', 'issuer', 'worker', 'Excluded answer body.', root)
    selection = thread_context.select(store, 'worker', [root, other],
                                      excluded={root.id, question.id, other.id})
    assert selection.roots == (root,) and store.is_open(root)
    assert selection.incoming == [answer]
    assert selection.entries == (thread_context.Entry(answer),)
    assert selection.render() == post.typed([answer])
    assert all(message.body not in selection.render() for message in (root, question, other))
    assert not list((tmp_path / 'post' / 'attempts').glob('*.json'))


def test_excluded_only_answer_does_not_pull_in_its_own_question_bridge(tmp_path):
    store, add = graph(tmp_path)
    root = add('order', 'issuer', 'worker', 'Visible task.')
    question = add('question', 'worker', 'issuer', 'Unneeded question body.', root)
    answer = add('answer', 'issuer', 'worker', 'Excluded amendment body.', question)
    selection = thread_context.select(store, 'worker', excluded={answer.id})
    assert selection.entries == (thread_context.Entry(root),)


def suborder_thread(tmp_path):
    """One open order whose thread holds every edge the narrow rule decides."""
    store, add = graph(tmp_path)
    m = {}
    m['root'] = add('order', 'issuer', 'worker', 'Open task.')
    m['sub'] = add('order', 'worker', 'helper', 'Sub-order to a helper.', m['root'])
    m['sub_report'] = add('report', 'helper', 'worker', 'Helper closes the sub-order.', m['sub'])
    m['sub_answer'] = add('answer', 'helper', 'worker', 'Helper progress on the sub-order.', m['sub'])
    m['helper_q'] = add('question', 'helper', 'worker', 'Helper asked, worker answered.', m['sub'])
    own_reply = add('answer', 'worker', 'helper', 'Worker answers the helper.', m['helper_q'])
    m['helper_on_answer'] = add('answer', 'helper', 'worker', 'Helper replies to that answer.', own_reply)
    m['up'] = add('order', 'worker', 'issuer', 'Sub-order up to the issuer.', m['root'])
    m['up_report'] = add('report', 'issuer', 'worker', 'Issuer closes the worker sub-order.', m['up'])
    m['up_answer'] = add('answer', 'issuer', 'worker', 'Issuer answers the worker sub-order.', m['up'])
    m['amend'] = add('answer', 'issuer', 'worker', 'Requester amends the open task.', m['root'])
    m['correction'] = add('answer', 'issuer', 'worker', 'Requester correction under the history.', m['sub_report'])
    m['report_amend'] = add('report', 'issuer', 'worker', 'Requester report in the thread.', m['root'])
    m['own_q'] = add('question', 'worker', 'helper', 'Own question to the helper.', m['sub'])
    m['own_answer'] = add('answer', 'helper', 'worker', 'Helper answers the own question.', m['own_q'])
    m['third'] = add('answer', 'bystander', 'worker', 'Bystander note on the open task.', m['root'])
    m['open_q'] = add('question', 'helper', 'worker', 'Helper question still open.', m['sub'])
    m['open_q_amend'] = add('report', 'helper', 'worker', 'Helper amends its open question.', m['open_q'])
    add('ack', 'issuer', 'worker', 'Acknowledgment only.', m['root'])
    return store, m


HISTORY = ('sub_report', 'sub_answer', 'helper_q', 'helper_on_answer', 'up_report', 'up_answer', 'third')


def test_fresh_start_carries_amendments_own_answers_and_open_roots_not_suborder_history(tmp_path):
    store, m = suborder_thread(tmp_path)
    selection = thread_context.select(store, 'worker')
    assert selection.roots == (m['root'], m['open_q'])
    assert selection.incoming == [m['root'], m['amend'], m['correction'], m['report_amend'], m['own_answer'],
                                  m['open_q'], m['open_q_amend']]
    assert [entry.message for entry in selection.entries if entry.context_only] == [m['own_q']]
    rendered = selection.render()
    assert all(m[name].body not in rendered for name in HISTORY)
    assert all(m[name].body in store.get(m[name].id).body for name in HISTORY), 'history stays in the store'
    assert rendered.index(m['own_q'].body) < rendered.index(m['own_answer'].body)


def test_unread_and_queued_history_still_arrives_once_whatever_its_kind(tmp_path):
    store, m = suborder_thread(tmp_path)
    unread = [m['up_report'], m['sub_report'], m['amend'], m['sub_report'], m['third']]
    selection = thread_context.select(store, 'worker', unread)
    carried = [m['root'], m['amend'], m['correction'], m['report_amend'], m['own_answer'], m['open_q'], m['open_q_amend']]
    assert selection.incoming == carried + [m['sub_report'], m['up_report'], m['third']]
    assert len({entry.message.id for entry in selection.entries}) == len(selection.entries)
    resumed = thread_context.select(store, 'worker', [m['sub_report']],
                                    consumed={m['root'].id, m['amend'].id, m['sub_report'].id})
    assert resumed.incoming == [m['root'], m['correction'], m['report_amend'], m['own_answer'],
                                m['open_q'], m['open_q_amend']]


def test_requester_answer_amending_an_open_order_follows_its_root(tmp_path):
    store, add = graph(tmp_path)
    root = add('order', 'requester', 'worker', 'Do the task.')
    amendment = add('answer', 'requester', 'worker', 'Also cover the second case.', root)
    assert store.is_open(root)
    selection = thread_context.select(store, 'worker')
    assert selection.incoming == [root, amendment]
    assert selection.render().index(root.body) < selection.render().index(amendment.body)


def test_nested_root_amendments_follow_their_own_sender_in_thread_order(tmp_path):
    store, add = graph(tmp_path)
    root = add('order', 'issuer', 'worker', 'Outer task.')
    nested = add('order', 'second', 'worker', 'Nested open order from another seat.', root)
    issuer_note = add('answer', 'issuer', 'worker', 'Outer requester on the nested order.', nested)
    nested_amend = add('answer', 'second', 'worker', 'Nested requester amends.', nested)
    later = add('answer', 'issuer', 'worker', 'Outer amendment, later.', root)
    selection = thread_context.select(store, 'worker')
    assert selection.roots == (root, nested)
    assert selection.incoming == [root, nested, issuer_note, nested_amend, later]


def test_excluded_amendments_answers_and_context_never_supply_a_body(tmp_path):
    store, m = suborder_thread(tmp_path)
    excluded = {m['amend'].id, m['own_answer'].id, m['open_q'].id, m['own_q'].id}
    selection = thread_context.select(store, 'worker', [m['amend']], excluded=excluded)
    assert selection.roots == (m['root'], m['open_q']) and store.is_open(m['open_q'])
    assert selection.incoming == [m['root'], m['correction'], m['report_amend'], m['open_q_amend']]
    rendered = selection.render()
    assert all(m[name].body not in rendered for name in ('amend', 'own_answer', 'open_q', 'own_q', *HISTORY))
    only_answer_excluded = thread_context.select(store, 'worker', excluded={m['own_answer'].id})
    assert m['own_q'] not in [entry.message for entry in only_answer_excluded.entries]
    assert not list((tmp_path / 'post' / 'attempts').glob('*.json'))


def test_requester_question_already_answered_is_history_while_an_open_one_is_a_root(tmp_path):
    store, add = graph(tmp_path)
    root = add('order', 'issuer', 'worker', 'Open task.')
    settled = add('question', 'issuer', 'worker', 'Requester question the seat answered.', root)
    add('answer', 'worker', 'issuer', 'The seat answered it.', settled)
    pending = add('question', 'issuer', 'worker', 'Requester question still open.', root)
    amendment = add('answer', 'issuer', 'worker', 'Requester amendment.', root)
    assert not store.is_open(settled) and store.is_open(pending)
    selection = thread_context.select(store, 'worker')
    assert selection.roots == (root, pending)
    assert selection.incoming == [root, pending, amendment]
    assert settled.body not in selection.render()
    unread = thread_context.select(store, 'worker', [settled])
    assert unread.incoming == [root, pending, amendment, settled], 'unread still arrives whatever its kind'
    assert unread.render().count(settled.body) == 1
