"""Ordinary pure option choice, serialized continuation and canonical identity examples."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from quarterdeck import control_confirmation as confirmation
from quarterdeck.model_mismatch import compare


def test_value_choice_uses_option_value_and_text_independently_of_order():
    yes = confirmation.Option('Yes, select Next', 'next-model', ('synthetic-yes',))
    no = confirmation.Option('No, retain Prior', 'prior-model', ('synthetic-no',))
    left = confirmation.value_choice('model', 'next-model', (yes, no), lambda value: value)
    right = confirmation.value_choice('model', 'next-model', (no, yes), lambda value: value)
    assert left == right
    assert left.option == 'Yes, select Next'
    assert left.keys == ('synthetic-yes',)


def test_late_question_continues_the_same_serialized_submission():
    now = datetime(2000, 1, 1, tzinfo=timezone.utc)
    original = confirmation.Continuation.begin('model', 'next-model', {'launch': 'synthetic-launch'}, now)
    state = confirmation.Continuation.from_metadata(confirmation.submitted(original).metadata())
    first = confirmation.advance(state, now + timedelta(seconds=1), confirmed=False)
    assert first.action == 'wait'
    assert first.state.deadline == original.deadline
    answer = confirmation.value_choice(
        'model', 'next-model', (confirmation.Option('Yes, select Next', 'next-model', ('synthetic-yes',)),),
        lambda value: value)
    question = confirmation.advance(first.state, now + timedelta(seconds=2), confirmed=False, answer=answer)
    assert question.action == 'answer'
    resumed = confirmation.Continuation.from_metadata(question.state.metadata())
    complete = confirmation.advance(confirmation.answered(resumed, typed=True), now + timedelta(seconds=3),
                                    confirmed=True)
    assert complete.action == 'confirmed'
    assert confirmation.proof(complete.state) == 'program asked; ship answered yes; record confirmed'


def test_expired_continuation_names_unconfirmed_without_new_transport():
    now = datetime(2000, 1, 1, tzinfo=timezone.utc)
    state = confirmation.Continuation.begin('model', 'next-model', {'launch': 'synthetic-launch'}, now)
    decision = confirmation.advance(state, now + timedelta(seconds=confirmation.CONFIRM_SECONDS), confirmed=False)
    assert decision.action == 'unconfirmed'
    assert 'not retyped' in decision.reason


def test_canonical_model_difference_uses_only_declared_identity_mapping():
    aliases = {'display-next': 'next-model', 'next-model': 'next-model', 'prior-model': 'prior-model'}
    adapter = SimpleNamespace(model_identity=lambda value, version=None: aliases.get(value))
    assert compare(adapter, 'next-model', 'display-next', source='record').status == 'match'
    mismatch = compare(adapter, 'next-model', 'prior-model', source='record')
    assert (mismatch.status, mismatch.declared, mismatch.running) == ('mismatch', 'next-model', 'prior-model')
    assert compare(adapter, 'next-model', 'unattested-display', source='screen').status == 'unknown'
