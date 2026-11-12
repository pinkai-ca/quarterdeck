"""Exact synthetic launch bundles, with no nested-envelope or context receipts."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from quarterdeck import post, refusal, thread_context
from quarterdeck.harness.types import Input
from .test_thread_context import graph


AT = datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(hours=1)


def received(store, text, *, excluded=(), kind='accepted', at=AT, consumed_at=None):
    return thread_context.consumed_ids(store, 'worker', 'worker', [Input(text, at, kind, consumed_at)],
                                       excluded=excluded)


def bundle(tmp_path):
    store, add = graph(tmp_path)
    root = add('order', 'issuer', 'worker', 'Synthetic task.')
    question = add('question', 'worker', 'issuer', 'Which option?', root)
    answer = add('answer', 'issuer', 'worker', 'Use option two.', question)
    selection = thread_context.select(store, 'worker')
    text = post.briefed_header('worker', 'worker', list(selection.roots)) + '\n' + selection.render()
    return store, root, question, answer, text


def test_exact_bundle_consumes_only_incoming_and_same_context_suppresses_amendment(tmp_path):
    store, root, question, answer, text = bundle(tmp_path)
    consumed = received(store, text)
    assert consumed == {root.id, answer.id}
    assert thread_context.select(store, 'worker', consumed=consumed).incoming == [root]
    fresh = thread_context.select(store, 'worker')
    assert fresh.incoming == [root, answer] and question.body in fresh.render()


@pytest.mark.parametrize('change', ['prefix', 'suffix', 'truncate', 'wrong-body', 'wrong-role', 'extra-separator'])
def test_unknown_or_truncated_surroundings_reject_whole_bundle(tmp_path, change):
    store, root, question, answer, text = bundle(tmp_path)
    if change == 'prefix':
        text = 'Unrecognized prefix.\n' + text
    elif change == 'suffix':
        text += 'Unrecognized suffix.'
    elif change == 'truncate':
        text = text[:-15]
    elif change == 'wrong-body':
        text = text.replace(answer.body, 'Another option.')
    elif change == 'wrong-role':
        text = text.replace('briefed as worker, worker', 'briefed as worker, unrelated')
    else:
        text = text.replace('\nqd context > own', '\n\nqd context > own')
    assert received(store, text) == set()


def test_envelope_inside_incoming_body_or_own_question_is_not_separate_consumption(tmp_path):
    store, add = graph(tmp_path)
    nested = add('report', 'issuer', 'worker', 'Envelope-looking nested text.')
    root = add('order', 'issuer', 'worker', 'Example within the task:\n' + post.typed([nested]))
    question = add('question', 'worker', 'issuer', 'Example within my question:\n' + post.typed([nested]), root)
    answer = add('answer', 'issuer', 'worker', 'The example is text only.', question)
    selection = thread_context.select(store, 'worker')
    text = post.briefed_header('worker', 'worker', list(selection.roots)) + '\n' + selection.render()
    assert received(store, text) == {root.id, answer.id}
    assert nested.id not in received(store, thread_context.context(question))


def test_ambiguous_canonical_tokens_suppress_nothing(tmp_path):
    store, add = graph(tmp_path)
    first = add('order', 'issuer', 'worker', 'Same canonical envelope.')
    duplicate = replace(first, id='ord-alternate-' + first.id.rsplit('-', 1)[1])
    store.write(duplicate)
    assert post.typed([first]) == post.typed([duplicate])
    assert received(store, post.typed([first])) == set()


def test_context_and_stubs_have_no_consumption_ids_or_delivery_attempts(tmp_path):
    store, root, question, answer, text = bundle(tmp_path)
    assert received(store, thread_context.context(question)) == set()
    assert received(store, refusal.stub(answer), excluded={answer.id}) == set()
    assert received(store, post.typed([answer]), excluded={answer.id}) == set()
    assert received(store, text, excluded={question.id}) == set()
    assert received(store, text, excluded={answer.id}) == set()
    assert not list((tmp_path / 'post' / 'attempts').glob('*.json'))


@pytest.mark.parametrize('kind,at,consumed_at', [
    ('queued', AT, None), ('opaque', AT, None), ('accepted', None, None),
    ('accepted', AT.replace(tzinfo=None), None), ('accepted', AT - timedelta(hours=2), None),
    ('consumed', AT, None), ('consumed', AT, AT - timedelta(seconds=1)),
])
def test_unknown_or_unconsumed_input_does_not_suppress_amendments(tmp_path, kind, at, consumed_at):
    store, root, question, answer, text = bundle(tmp_path)
    assert received(store, text, kind=kind, at=at, consumed_at=consumed_at) == set()


def test_valid_timestamped_consumption_is_accepted_without_inventing_times(tmp_path):
    store, root, question, answer, text = bundle(tmp_path)
    assert received(store, text, kind='consumed', consumed_at=AT + timedelta(seconds=1)) == {root.id, answer.id}
