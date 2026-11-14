"""Current envelopes and complete pre-change records retain the same receipt and attribution."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from quarterdeck import delivery_record, post
from quarterdeck.harness import grok_screen, synthetic_record
from quarterdeck.harness.claude import ClaudeHarness
from quarterdeck.harness.fake import FakeHarness
from quarterdeck.harness.grok import GrokHarness
from quarterdeck.ledger import Ledger
from quarterdeck.order_attribution import Threads

AT = datetime(2032, 1, 1, tzinfo=timezone.utc)
ROOT_ID = '-'.join(('ord', '20320101', '0000', 'captain', '1234'))


def message(kind):
    return post.Message(f'{kind[:3]}-20320101-0000-issuer-abcd', kind, 'issuer', 'worker',
                        AT, 'ordinary synthetic body', re=ROOT_ID)


def legacy(message, second=False):
    # Independent bytes of the formatter before this change, not a projection of typed().
    header = f'qd > {message.kind} from {message.sender} · {message.short} · re ord-1234'
    footer = f'qd > inbox: post/inbox/{message.to}.md'
    if message.kind in {'order', 'question'}:
        verb = 'report' if message.kind == 'order' else 'answer'
        footer = f'qd > reply: qd post {verb} --re {message.short} · {footer[5:]}'
    again = f'qd > second delivery of {message.id}; you may have it already: act on it once\n' if second else ''
    return f'{again}{header}\n{message.body}\n{footer}\n'


@pytest.mark.parametrize('kind', post.KINDS)
@pytest.mark.parametrize('second', [False, True])
def test_only_obligations_have_a_reply_line_and_no_inbox_path(kind, second):
    m = message(kind)
    text = post.typed([m], second=[m.id] if second else [])
    forms = post.delivered_forms(m)
    assert len(forms) == 4
    assert forms[int(second)] == text
    assert forms[2 + int(second)] == legacy(m, second)
    assert 'post/inbox/' not in text
    expected = f'qd > {kind} from issuer · {m.short} · re ord-1234\n{m.body}'
    if m.obligation:
        verb = 'report' if kind == 'order' else 'answer'
        expected += f'\nqd > reply: qd post {verb} --re {m.short}'
    if second:
        expected = f'qd > second delivery of {m.id}; you may have it already: act on it once\n' + expected
    assert text == expected + '\n'
    assert grok_screen.queue_preview(text).endswith(f'(+{text.count(chr(10)) - 1} lines)')
    # Claude's folding count derives from the exact outgoing payload, including its final LF.
    assert ClaudeHarness().box(text).matches(f'[Pasted text #1 +{text.count(chr(10))} lines]')


@pytest.mark.parametrize('kind', post.KINDS)
@pytest.mark.parametrize('second', [False, True])
def test_old_typed_record_is_confirmed_and_attributed_after_the_change(tmp_path, monkeypatch, kind, second):
    store = post.Store(tmp_path / 'runtime')
    root = post.Message(ROOT_ID, 'order', 'captain', 'issuer',
                        AT - timedelta(seconds=1), 'ordinary synthetic change')
    m = message(kind)
    store.write(root)
    store.write(m)
    old = legacy(m, second)
    clock = [AT + timedelta(seconds=10)]
    monkeypatch.setattr(synthetic_record, 'now', lambda: clock[0])
    record = synthetic_record.ClaudeShape(tmp_path / 'home', tmp_path, 'synthetic-old', 'fake-model')
    record.begin(old, 'fake-model', 'low')
    clock[0] += timedelta(seconds=2)
    record.end('ordinary synthetic report')
    reader = FakeHarness()
    assert old != post.typed([m], second=[m.id] if second else [])
    assert delivery_record.receipt(reader, record.path, m, AT) == 'delivered'
    turns = reader.turns(record.path)
    assert len(turns) == 1 and turns[0].input.strip() == old.strip()
    # Exercise the same persisted turn and attribution replay as an already filed record.
    with Ledger(tmp_path / 'ledger.sqlite') as book:
        book.ingest('worker', 'synthetic-pool', record.path, turns, store)
        book.attributions.replay(store, tmp_path, {str(record.path): {'fake'}})
        result, = book.attributions.details()
        assert result['status'] == 'attributed' and result['orders'] == [root.id]
        assert result['evidence'][0]['message'] == m.id


@pytest.mark.parametrize('kind', ['report', 'answer', 'ack'])
def test_old_footer_is_consumed_once_in_a_mixed_bundle(tmp_path, kind):
    store = post.Store(tmp_path)
    first = message(kind)
    second = replace(message('question'), id='-'.join(('que', '20320101', '0000', 'issuer', 'dcba')))
    store.write(first)
    store.write(second)
    received = Threads(store).received(legacy(first) + post.typed([second]),
                                       AT + timedelta(seconds=10), 'worker')
    assert [entry['message'] for entry in received] == [first.id, second.id]


@pytest.mark.parametrize('kind', ['order', 'report'])
@pytest.mark.parametrize('second', [False, True])
@pytest.mark.parametrize('old', [False, True])
def test_current_and_legacy_queue_previews_and_claude_queue_echo(tmp_path, monkeypatch, kind, second, old):
    m = message(kind)
    payload = legacy(m, second) if old else post.typed([m], second=[m.id] if second else [])
    sample = Path(__file__).parents[1] / 'fixtures/grok/1.0.77/queue-native-112.json'
    data = json.loads(sample.read_text())
    rows = data['screens']['1']
    rows = ['     #1 ' + grok_screen.queue_preview(payload) if row.startswith('     #1 ') else row
            for row in rows]
    grok = GrokHarness()
    before = grok.screen('\n'.join(data['draft']) + '\n')
    after = grok.screen('\n'.join(rows) + '\n')
    assert grok.queued_waiting(payload, after)
    assert grok.queued_input(payload, before, after)
    clock = [AT + timedelta(seconds=10)]
    monkeypatch.setattr(synthetic_record, 'now', lambda: clock[0])
    record = synthetic_record.ClaudeShape(tmp_path / 'home', tmp_path, 'synthetic-queue', 'fake-model')
    record.queued(payload)
    assert delivery_record.receipt(FakeHarness(), record.path, m, AT) == 'queued'
    clock[0] += timedelta(seconds=1)
    record.dequeued(payload)
    record.begin(payload, 'fake-model', 'low')
    clock[0] += timedelta(seconds=1)
    record.end('ordinary synthetic report')
    assert delivery_record.receipt(FakeHarness(), record.path, m, AT) == 'delivered'
