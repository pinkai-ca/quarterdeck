"""A recipient's closing report settles its order's answers for future carriage."""

import json
from dataclasses import replace

import pytest

from quarterdeck import post, thread_context
from .test_pending_delivery import setup  # noqa: F401
from .test_reported_order_carriage import active


@pytest.mark.parametrize('result', [None, 'launch', 'queued', 'pending', 'blind', 'withheld', 'unacted', 'delivered', 'watch'])
@pytest.mark.parametrize('destination', ['issuer', post.SHIP])
@pytest.mark.parametrize('reply_to', ['order', 'question', 'report'])
def test_reported_order_answers_never_return_as_fresh_work(setup, result, destination, reply_to):
    svc, seat, live, record = setup
    order = svc.store.new('order', 'issuer', seat.name, 'Synthetic task')
    question = svc.store.new('question', seat.name, 'issuer', 'Synthetic clarification', re=order.id)
    report = svc.store.new('report', seat.name, destination, 'Synthetic done', re=order.id)
    parent = dict(order=order, question=question, report=report)[reply_to]
    answer = svc.store.new('answer', 'issuer', seat.name, 'Synthetic follow-up', re=parent.id)
    if result is not None:
        attempt = svc.attempts.open(answer, seat.name, '%1', 123, seat.launched().at)
        svc.attempts.close(attempt, result)
    history = svc.attempts.all()
    messages = {path: path.read_bytes() for path in svc.store.dir.glob('*.json')}
    for _ in range(2):
        carriage = seat.carriage()
        assert answer not in carriage.carried and answer.id not in active(carriage)
        # An outgoing question can still await its answer; none is incoming work.
        assert all(not row.active for row in carriage.rows)
        opening, carried = seat.first_message()
        assert answer not in carried and answer.body not in (opening or '')
        assert all(not json.loads(line)['active'] for line in (opening or '').splitlines() if line.startswith('{'))
    assert svc.attempts.all() == history
    assert svc.store.read_ids(seat.name) == frozenset(), 'closure is not a fabricated body-read receipt'
    assert {path: path.read_bytes() for path in svc.store.dir.glob('*.json')} == messages
    assert live['pasted'] == live['submitted'] == 0


@pytest.mark.parametrize('after', [False, True])
def test_answer_is_carried_until_its_recipient_reports_the_order(setup, after):
    svc, seat, live, record = setup
    order = svc.store.new('order', 'issuer', seat.name, 'Synthetic task')
    if after:
        svc.store.new('report', seat.name, post.SHIP, 'Synthetic done', re=order.id)
    answer = svc.store.new('answer', 'issuer', seat.name, 'Synthetic instruction', re=order.id)
    if not after:
        carriage = seat.carriage()
        assert carriage.carried == [order, answer] and answer.id in active(carriage)
        svc.store.new('report', seat.name, post.SHIP, 'Synthetic done', re=order.id)
    assert seat.carriage().carried == [] and seat.carriage().rows == ()


@pytest.mark.parametrize('ancestry', ['none', 'question', 'open-order', 'foreign-report', 'nested-open', 'foreign-order'])
def test_answers_without_an_own_closed_order_keep_their_existing_carriage(setup, ancestry):
    svc, seat, live, record = setup
    parent = None
    if ancestry == 'question':
        parent = svc.store.new('question', seat.name, 'issuer', 'Synthetic question')
    elif ancestry in ('open-order', 'foreign-report'):
        parent = svc.store.new('order', 'issuer', seat.name, 'Synthetic open task')
        if ancestry == 'foreign-report':
            svc.store.new('report', 'other', 'issuer', 'Not the addressee', re=parent.id)
    elif ancestry in ('nested-open', 'foreign-order'):
        old = svc.store.new('order', 'issuer', seat.name, 'Synthetic old task')
        svc.store.new('report', seat.name, 'issuer', 'Synthetic done', re=old.id)
        parent = svc.store.new('order', 'issuer', seat.name if ancestry == 'nested-open' else 'other',
                               'Synthetic new task', re=old.id)
        if ancestry == 'foreign-order':
            svc.store.new('report', 'other', 'issuer', 'Synthetic other completion', re=parent.id)
    answer = svc.store.new('answer', 'issuer', seat.name, 'Synthetic information', re=parent.id if parent else None)
    carriage = seat.carriage()
    assert answer in carriage.carried and answer.id in active(carriage)
    assert answer.id not in thread_context.closed_order_answers(svc.store, seat.name)


def test_settled_answer_is_only_history_when_other_work_keeps_the_index_row(setup):
    svc, seat, live, record = setup
    old = svc.store.new('order', 'issuer', seat.name, 'Synthetic old task')
    answer = svc.store.new('answer', 'issuer', seat.name, 'Synthetic follow-up', re=old.id)
    svc.store.new('report', seat.name, 'issuer', 'Synthetic done', re=old.id)
    new = svc.store.new('order', 'issuer', seat.name, 'Synthetic new task', re=old.id)
    carriage = seat.carriage()
    assert carriage.carried == [new] and active(carriage) == [new.id]
    # The settled answer amends the closed old order, not the open new one: the line no longer names it, and the
    # thread, printed whole by qd thread, still holds it.
    assert answer.id not in carriage.rows[0].amendments, 'only what amends the open messages is named'
    assert answer.id not in carriage.rows[0].active and not carriage.rows[0].awaiting
    from quarterdeck import post_threads
    assert answer.id in {m.id for m in post_threads.whole(svc.store, new.id, seat.name).messages}, \
        'immutable thread history remains readable'


@pytest.mark.parametrize('broken', ['missing', 'cycle'])
def test_incomplete_ancestry_does_not_prove_an_answer_settled(setup, monkeypatch, broken):
    svc, seat, live, record = setup
    answer = svc.store.new('answer', 'issuer', seat.name, 'Synthetic information')
    changed = replace(answer, re=answer.id if broken == 'cycle' else 'missing')
    monkeypatch.setattr(svc.store, 'all', lambda: [changed])
    assert thread_context.closed_order_answers(svc.store, seat.name) == frozenset()
