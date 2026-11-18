"""Thread amendments through synthetic fresh/resume and subscription move lifecycles."""

import json

import pytest

from quarterdeck import pending, post, post_threads, record_owner, refusal
from quarterdeck.harness.base import RecordError
from quarterdeck.ship import ShipError
from .test_pending_delivery import setup, input_row  # noqa: F401
from .test_cross_program_failover import running, usage  # noqa: F401


def indexed(text):
    return {row['id']: row for row in (json.loads(line) for line in text.splitlines() if line.startswith('{'))}


def whole(store, root, seat):
    return post_threads.render(post_threads.whole(store, root.id, seat.name))


def amendment(store, seat):
    root = store.new('order', 'issuer', seat.name, 'Synthetic open task.')
    question = store.new('question', seat.name, 'issuer', 'Which option?', re=root.id)
    answer = store.new('answer', 'issuer', seat.name, 'Use option two.', re=question.id)
    return root, question, answer


def test_fresh_and_resume_index_delivered_amendment_without_new_receipts(setup):
    svc, seat, live, record = setup
    root, question, answer = amendment(svc.store, seat)
    input_row(record, post.typed([answer]), post.now())
    svc.attempts.close(svc.attempts.open(answer, seat.name, '%1', 123, seat.launched().at), 'delivered')
    fresh, incoming = seat.first_message()
    assert incoming == [root] and answer.body not in fresh and question.body not in fresh
    assert answer.id in indexed(fresh)[root.id]['amendments']
    printed = whole(svc.store, root, seat)
    assert printed.index(root.body) < printed.index(question.body) < printed.index(answer.body)
    resumed, incoming = seat.first_message(resume_id='synthetic')
    assert incoming == [root] and answer.id in indexed(resumed)[root.id]['amendments']
    assert svc.attempts.latest()[answer.id] == 'delivered', 'indexing never relabels record delivery'
    assert svc.attempts.for_message(question.id) == []
    assert seat.first_message(resume_id='different-native-conversation')[1] == [root]


def test_index_input_is_not_consumption_of_its_referenced_bodies(setup):
    svc, seat, live, record = setup
    root, question, answer = amendment(svc.store, seat)
    first, incoming = seat.first_message()
    input_row(record, first, post.now())
    assert seat.first_message(resume_id='synthetic')[1] == [root, answer], 'an index is not consumption of its referenced bodies'
    assert seat.first_message()[1] == [root, answer]
    assert answer.id in indexed(first)[root.id]['amendments']
    assert answer.body not in first and answer.body in whole(svc.store, root, seat)
    assert all(not svc.attempts.for_message(message.id) for message in (root, question, answer))


def test_current_record_must_prove_consumption_and_have_matching_reader(setup, monkeypatch):
    svc, seat, live, record = setup
    root, question, answer = amendment(svc.store, seat)
    def unreadable(record):
        raise RecordError('synthetic unreadable input history')
    monkeypatch.setattr(seat.harness, 'inputs', unreadable)
    assert seat.first_message(resume_id='synthetic')[1] == [root, answer]
    monkeypatch.setattr(record_owner, 'pinned_reader', lambda *a: None)
    assert seat.first_message(resume_id='synthetic')[1] == [root, answer]


def test_read_only_exclusion_hook_filters_both_incoming_and_own_question_context(setup, monkeypatch):
    svc, seat, live, record = setup
    root, question, answer = amendment(svc.store, seat)
    # A synthetic id-set exercises the generic contract, without refusal records or policy fixtures.
    monkeypatch.setattr(refusal, 'excluded', lambda runtime, name: frozenset({root.id, question.id}))
    first, incoming = seat.first_message()
    assert incoming == [answer]
    assert root.body not in first and question.body not in first and answer.body not in first
    # Withheld by the hook alone, so no event holds a notice; this ship has no file to say whether its sender is a
    # seat, so the stub says only that it was not told.
    assert indexed(first)[root.id]['notice'] == refusal.UNTOLD
    assert set(indexed(first)[root.id]['withheld']) == {root.id, question.id}
    printed = whole(svc.store, root, seat)
    assert root.body not in printed and question.body not in printed and answer.body in printed
    assert svc.store.is_open(root) and svc.attempts.all() == []
    def unreadable(runtime, name):
        raise refusal.JournalError('synthetic unknown exclusion inventory')
    monkeypatch.setattr(refusal, 'excluded', unreadable)
    with pytest.raises(ShipError, match='inspect it before a launch carries anything'):
        seat.first_message()
    assert svc.attempts.all() == []


def test_actual_cross_program_move_indexes_old_amendment_and_keeps_receipts(running):
    r = running
    root, question, answer = amendment(r.svc.store, r.seat)
    r.source.begin(post.typed([answer]), 'synthetic-source', 'high')
    r.source.end('Synthetic acknowledgment.')
    r.svc.attempts.close(r.svc.attempts.open(answer, 'worker', '%1', 101, r.seat.launched().at), 'delivered')
    r.svc.account()
    r.svc.failover()
    first = r.launches[-1][-1]
    assert r.seat.launched().resume is None
    assert answer.body not in first and answer.id in indexed(first)[root.id]['amendments']
    printed = whole(r.svc.store, root, r.seat)
    assert printed.index(root.body) < printed.index(question.body) < printed.index(answer.body)
    assert [a.result for a in r.svc.attempts.for_message(answer.id)] == ['delivered']
    assert r.svc.attempts.for_message(question.id) == []
    r.svc.reconcile()
    assert r.svc.attempts.latest()[answer.id] == 'delivered', 'referencing a delivered amendment does not create a new receipt'


def test_actual_same_record_resume_keeps_index_and_fresh_adds_only_self_handoff(running):
    r = running
    root, question, answer = amendment(r.svc.store, r.seat)
    handoff = r.svc.store.new('order', r.seat.name, r.seat.name, 'Synthetic complete self handoff.')
    r.svc.failover()
    first = r.launches[-1][-1]
    r.created[-1].begin(first, r.seat.spec.model, 'high')
    r.created[-1].end('Synthetic launch reply.')
    r.seat.restart()
    assert r.seat.launched().resume is not None
    assert answer.body not in r.launches[-1][-1]
    assert root.body not in r.launches[-1][-1] and handoff.body not in r.launches[-1][-1]
    assert answer.id in indexed(r.launches[-1][-1])[root.id]['amendments']
    r.seat.fresh()
    assert r.seat.launched().resume is None and handoff.body in r.launches[-1][-1]
    assert answer.body not in r.launches[-1][-1] and answer.body in whole(r.svc.store, root, r.seat)
    assert r.svc.attempts.for_message(question.id) == []


def test_unproved_resume_refuses_and_explicit_fresh_retains_amendment_index(running, monkeypatch):
    r = running
    root, question, answer = amendment(r.svc.store, r.seat)
    r.svc.failover()
    r.created[-1].begin(post.typed([answer]), r.seat.spec.model, 'high')
    r.created[-1].end('Synthetic reply.')
    before = r.seat.launched()
    monkeypatch.setattr(r.seat, '_resume_target', lambda: None)
    with pytest.raises(ShipError, match='Use qd fresh worker'):
        r.seat.restart()
    assert r.seat.launched() == before and r.seat.is_up
    r.seat.fresh()
    assert r.seat.launched().resume is None and answer.body not in r.launches[-1][-1]
    assert answer.id in indexed(r.launches[-1][-1])[root.id]['amendments']
    assert answer.body in whole(r.svc.store, root, r.seat)


def test_actual_fresh_carriage_retires_old_hold_without_confirming_old_submission(running):
    r = running
    root, question, answer = amendment(r.svc.store, r.seat)
    launched = r.seat.launched()
    attempt = r.svc.attempts.open(root, 'worker', launched.pane, launched.pid, launched.at)
    proof = pending.Proof.begin(r.seat, attempt, post.typed([root]))
    proof.save(phase='submitted', shown=post.typed([root]).rstrip())
    r.svc.attempts.close(attempt, 'pending', 'Synthetic uncertain original submission.')
    r.seat.fresh()
    assert answer.body not in r.launches[-1][-1]  # law: context-is-cache
    assert answer.id in indexed(r.launches[-1][-1])[root.id]['amendments']
    assert whole(r.svc.store, root, r.seat).count(answer.body) == 1
    r.svc.reconcile()
    assert pending.active(r.seat, include_terminal=True)[0].data['phase'] == 'carried'
    assert [a.result for a in r.svc.attempts.for_message(root.id)] == ['pending', 'launch']
    assert r.svc.attempts.for_message(question.id) == []
    assert not pending.detached(r.seat)
