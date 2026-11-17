"""Every place that recognises a typed message knows the form it was typed in before line ends were taken off.

A message with a line ending in a space was typed with that space before the door took line ends off; a record or a
journal from then holds that form. Each site that compares a record's input or a journal's payload with a message's
typed text matches any of its delivered forms (post.delivered_forms). Synthetic messages, records and journals.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from quarterdeck import drill, post, recovery, structure_outcome, thread_context
from quarterdeck.harness.types import Input
from quarterdeck.post_message import _typed

AT = datetime(2030, 1, 1, tzinfo=timezone.utc)
BODY = 'A synthetic line that ends in a space \nA synthetic last line'


def earlier(message):
    """The message as typed before the change: its line ends kept."""
    form = _typed([message], ends=False)
    assert form != post.typed([message]) and form in post.delivered_forms(message)
    return form


def test_a_resumed_conversation_holding_the_earlier_form_reads_it_consumed(tmp_path):
    store = post.Store(tmp_path / 'runtime')
    order = store.new('order', 'synthetic-issuer', 'worker', BODY)
    for text in (earlier(order), _typed([order], second=(order.id,), ends=False)):
        assert thread_context.consumed_ids(store, 'worker', 'worker', [Input(text, AT + timedelta(days=1))]) \
            == {order.id}


def test_a_recovery_reservation_typed_in_the_earlier_form_is_its_publication(tmp_path):
    runtime = tmp_path / 'runtime'
    store = post.Store(runtime)
    notice = store.new('report', post.SHIP, 'worker', BODY)
    seat = SimpleNamespace(name='worker', ship=SimpleNamespace(runtime=runtime))
    proof = SimpleNamespace(data={'recovery': {'post': notice.id}, 'payload': earlier(notice)})
    assert recovery._publication(seat, proof)
    proof.data['payload'] = earlier(notice).replace('last line', 'other line')
    assert not recovery._publication(seat, proof), 'any other text is still no publication'


def test_a_finished_order_typed_in_the_earlier_form_is_its_structure_target(tmp_path):
    store = post.Store(tmp_path / 'runtime')
    order = store.new('order', 'synthetic-issuer', 'worker', BODY)
    seat = SimpleNamespace(launched=lambda: SimpleNamespace(recovery=None))
    state = SimpleNamespace(last_input=earlier(order), capped=None)
    text, at, due = structure_outcome._target(seat, tmp_path / 'record', state, 'synthetic-conversation', order, [])
    assert text == earlier(order) and at == order.at and due is False, 'the text the record holds'


def test_a_finished_recovery_whose_order_was_typed_in_the_earlier_form_is_found(tmp_path, monkeypatch):
    store = post.Store(tmp_path / 'runtime')
    order = store.new('order', 'synthetic-issuer', 'worker', BODY)
    record = tmp_path / 'record'
    since = AT + timedelta(minutes=5)
    meta = {'mode': 'done', 'source': {'conversation': 'synthetic-conversation', 'work': 'original-work'},
            'error': None, 'recovery_input': 'recovery-work', 'client': 'synthetic-signature'}
    proof = SimpleNamespace(data={'recovery': meta, 'record': str(record), 'phase': 'delivered',
                                  'payload': 'synthetic continuation\n', 'since': since.isoformat()},
                            same_launch=lambda seat: True)

    def accepted_input(path, text, at, conversation):  # the record holds the order only in its earlier form
        return {earlier(order): 'original-work', 'synthetic continuation\n': 'recovery-work'}.get(text)
    client = SimpleNamespace(accepted_input=accepted_input, signature='synthetic-signature')
    monkeypatch.setattr(recovery, '_client', lambda seat: client)
    monkeypatch.setattr(recovery, '_publication', lambda seat, proof: True)
    state = SimpleNamespace(last_input='synthetic continuation\n', work_id='recovery-work', capped=None)
    found = structure_outcome._target(SimpleNamespace(), record, state, 'synthetic-conversation', order, [proof])
    assert found == ('synthetic continuation\n', since, False)


def test_the_drills_crash_catches_a_submission_typed_in_the_earlier_form(tmp_path, monkeypatch):
    store = post.Store(tmp_path / 'runtime')
    order = store.new('order', 'ship', 'captain-seat', BODY)
    attempt = SimpleNamespace(result=None, path=SimpleNamespace(name='synthetic-attempt.json'))
    proof = SimpleNamespace(data={'phase': 'submitted', 'attempts': ['synthetic-attempt.json'],
                                  'payload': earlier(order)},
                            same_launch=lambda seat: True, confirmed=lambda seat: None)
    monkeypatch.setattr(drill, 'journals', lambda seat: ([proof], []))
    run = object.__new__(drill.Drill)
    run.attempts = SimpleNamespace(for_message=lambda identifier: [attempt])
    assert run.crash_attempt(SimpleNamespace(), order, None)
    gate = SimpleNamespace(reached=lambda: {'text': earlier(order).strip(), 'pid': 7})
    assert run.crash_attempt(SimpleNamespace(pane=lambda: SimpleNamespace(pid=7)), order, gate)
