"""Thread amendments through synthetic fresh/resume and subscription move lifecycles."""

import pytest

from quarterdeck import pending, post, record_owner, refusal
from quarterdeck.harness.base import RecordError
from quarterdeck.ship import ShipError
from .test_pending_delivery import setup, input_row  # noqa: F401
from .test_cross_program_failover import running, usage  # noqa: F401


def amendment(store, seat):
    root = store.new('order', 'issuer', seat.name, 'Synthetic open task.')
    question = store.new('question', seat.name, 'issuer', 'Which option?', re=root.id)
    answer = store.new('answer', 'issuer', seat.name, 'Use option two.', re=question.id)
    return root, question, answer


def test_fresh_selection_carries_old_confirmed_amendment_once_but_resume_suppresses_it(setup):
    svc, seat, live, record = setup
    root, question, answer = amendment(svc.store, seat)
    input_row(record, post.typed([answer]), post.now())
    svc.attempts.close(svc.attempts.open(answer, seat.name, '%1', 123, seat.launched().at), 'delivered')
    fresh, incoming = seat.first_message()
    assert incoming == [root, answer] and fresh.count(answer.body) == 1
    assert fresh.index(root.body) < fresh.index(question.body) < fresh.index(answer.body)
    resumed, incoming = seat.first_message(resume_id='synthetic')
    assert incoming == [root] and answer.body not in resumed and question.body not in resumed
    assert svc.attempts.latest()[answer.id] == 'delivered', 'selection never relabels record delivery'
    assert svc.attempts.for_message(question.id) == []
    assert seat.first_message(resume_id='different-native-conversation')[1] == [root, answer]


def test_bundled_launch_consumption_dedups_in_actual_resume_record_only(setup):
    svc, seat, live, record = setup
    root, question, answer = amendment(svc.store, seat)
    first, incoming = seat.first_message()
    input_row(record, first, post.now())
    assert seat.first_message(resume_id='synthetic')[1] == [root]
    assert seat.first_message()[1] == [root, answer]
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
    assert root.body not in first and question.body not in first and answer.body in first
    assert refusal.stub(root) in first
    assert svc.store.is_open(root) and svc.attempts.all() == []
    def unreadable(runtime, name):
        raise refusal.JournalError('synthetic unknown exclusion inventory')
    monkeypatch.setattr(refusal, 'excluded', unreadable)
    with pytest.raises(ShipError, match='inspect it before a launch carries anything'):
        seat.first_message()
    assert svc.attempts.all() == []


def test_actual_cross_program_move_carries_old_amendment_with_incoming_only_receipts(running):
    r = running
    root, question, answer = amendment(r.svc.store, r.seat)
    r.source.begin(post.typed([answer]), 'synthetic-source', 'high')
    r.source.end('Synthetic acknowledgment.')
    r.svc.attempts.close(r.svc.attempts.open(answer, 'worker', '%1', 101, r.seat.launched().at), 'delivered')
    r.svc.account()
    r.svc.failover()
    first = r.launches[-1][-1]
    assert r.seat.launched().resume is None
    assert first.count(answer.body) == 1
    assert first.index(root.body) < first.index(question.body) < first.index(answer.body)
    assert [a.result for a in r.svc.attempts.for_message(answer.id)] == ['delivered', 'launch']
    assert r.svc.attempts.for_message(question.id) == []
    r.svc.reconcile()
    assert r.svc.attempts.latest()[answer.id] == 'launch', 'launch carriage is distinct from delivered'


def test_actual_same_record_restart_suppresses_consumed_amendment_and_fresh_restores_it(running):
    r = running
    root, question, answer = amendment(r.svc.store, r.seat)
    r.svc.failover()
    first = r.launches[-1][-1]
    r.created[-1].begin(first, r.seat.spec.model, 'high')
    r.created[-1].end('Synthetic launch reply.')
    r.seat.restart()
    assert r.seat.launched().resume is not None
    assert answer.body not in r.launches[-1][-1]
    assert root.body in r.launches[-1][-1]
    r.seat.fresh()
    assert r.seat.launched().resume is None and answer.body in r.launches[-1][-1]
    assert r.svc.attempts.for_message(question.id) == []


def test_requested_resume_without_actual_resume_id_carries_amendment(running, monkeypatch):
    r = running
    root, question, answer = amendment(r.svc.store, r.seat)
    r.svc.failover()
    r.created[-1].begin(post.typed([answer]), r.seat.spec.model, 'high')
    r.created[-1].end('Synthetic reply.')
    monkeypatch.setattr(r.seat, '_resume_id', lambda: None)
    monkeypatch.setattr(r.seat.harness, 'preflight_resume', lambda *a: None)
    r.seat.restart()
    assert r.seat.launched().resume is None and answer.body in r.launches[-1][-1]


def test_actual_fresh_carriage_retires_old_hold_without_confirming_old_submission(running):
    r = running
    root, question, answer = amendment(r.svc.store, r.seat)
    launched = r.seat.launched()
    attempt = r.svc.attempts.open(root, 'worker', launched.pane, launched.pid, launched.at)
    proof = pending.Proof.begin(r.seat, attempt, post.typed([root]))
    proof.save(phase='submitted', shown=post.typed([root]).rstrip())
    r.svc.attempts.close(attempt, 'pending', 'Synthetic uncertain original submission.')
    r.seat.fresh()
    assert r.launches[-1][-1].count(answer.body) == 1  # law: context-is-cache
    r.svc.reconcile()
    assert pending.active(r.seat, include_terminal=True)[0].data['phase'] == 'carried'
    assert [a.result for a in r.svc.attempts.for_message(root.id)] == ['pending', 'launch']
    assert r.svc.attempts.for_message(question.id) == []
    assert not pending.detached(r.seat)
