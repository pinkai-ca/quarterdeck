"""A measured enqueue without content is timed activity, with no identifiable input text.

Synthetic rows only; the queue row deliberately has just the four native envelope keys.
"""

from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from quarterdeck import harness, human_wake, input_receipt, person_quiet
from tests.compaction_shapes import AT
from .test_person_quiet import desk


def stamp(seconds):
    return (AT + timedelta(seconds=seconds)).isoformat()


def enqueue(second=60, **fields):
    return dict(type='queue-operation', operation='enqueue', sessionId='synthetic-conversation',
                timestamp=stamp(second), **fields)


def write(path, *rows):
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))


def user(text, second):
    return dict(type='user', timestamp=stamp(second), version='2.1.317',
                message=dict(role='user', content=text))


@pytest.mark.parametrize('following', ['none', 'dequeue', 'excluded-input'])
def test_quiet_clock_counts_the_opaque_enqueue_at_its_own_time(desk, following):
    w = desk()
    opaque = enqueue()
    w.native.rows.append(opaque)
    if following == 'dequeue':
        w.native.rows.append(dict(type='queue-operation', operation='dequeue',
                                 sessionId='synthetic-conversation', timestamp=stamp(70)))
    elif following == 'excluded-input':
        w.native.rows.append(user(human_wake.LINE, 70))
    w.native.flush()
    fact = person_quiet.read(w.seat, AT + timedelta(seconds=120), excluded=(human_wake.LINE, ''))
    assert fact['at'] == stamp(60) and fact['reason'] is None
    assert person_quiet.quiet_for(fact, AT + timedelta(seconds=120)) == timedelta(seconds=60)
    assert set(opaque) == {'type', 'operation', 'sessionId', 'timestamp'}


@pytest.mark.parametrize('content', [None, False, 7, [], {}])
def test_present_unrecognized_content_still_makes_quiet_time_unknown(desk, content):
    w = desk()
    w.native.rows.append(enqueue(content=content))
    w.native.flush()
    fact = person_quiet.read(w.seat, AT + timedelta(seconds=120))
    assert fact['at'] is None and fact['reason'] == 'a queued input is malformed'


def test_an_opaque_enqueue_without_a_time_is_unknown(desk):
    w = desk()
    row = enqueue()
    row.pop('timestamp')
    w.native.rows.append(row)
    w.native.flush()
    assert person_quiet.read(w.seat, AT + timedelta(seconds=120))['reason'] == 'an input row has no time'


def test_an_opaque_enqueue_in_the_future_does_not_prove_quiet(desk):
    w = desk()
    w.native.rows.append(enqueue(300))
    w.native.flush()
    assert person_quiet.read(w.seat, AT + timedelta(seconds=120))['reason'] == 'a recorded time is in the future'


@pytest.mark.parametrize('wanted', ['', human_wake.LINE, 'Synthetic payload'])
def test_opaque_enqueue_never_confirms_a_receipt_or_a_typed_message(tmp_path, wanted):
    record = tmp_path / 'synthetic.jsonl'
    write(record, enqueue(), dict(type='queue-operation', operation='dequeue', timestamp=stamp(70)))
    assert input_receipt.confirmed('claude', record, wanted, AT) is None
    evidence = harness.get('claude').inputs(record)
    assert evidence == []
    assert evidence.skipped == [dict(line=1, kind='queue-operation', reason='queued input text not recorded')]


def test_receipts_and_ledger_input_projection_keep_readable_inputs_beside_the_opaque_row(tmp_path):
    record = tmp_path / 'synthetic.jsonl'
    write(record, user('Synthetic before', 1), enqueue(), user('Synthetic after', 80))
    reader = harness.get('claude')
    assert input_receipt.confirmed(reader, record, 'Synthetic before', AT) == 'delivered'
    assert input_receipt.confirmed(reader, record, 'Synthetic after', AT) == 'delivered'
    projected = reader.attribution_inputs(record)
    assert [(item.text, item.at.isoformat()) for item in projected] == [
        ('Synthetic before', stamp(1)), ('Synthetic after', stamp(80))]
    assert input_receipt.confirmed(reader, record, '', AT) is None


def test_human_wake_keeps_prior_receipt_uncertain_when_text_is_not_recorded(tmp_path, monkeypatch):
    record = tmp_path / 'synthetic.jsonl'
    write(record, enqueue())
    seat = SimpleNamespace(harness=harness.get('claude'), record=lambda: record)
    monkeypatch.setattr(human_wake, 'candidate_hold', lambda *args: None)
    assert human_wake.prior_receipt(seat, record, AT) == 'human wake prior receipt parse incomplete; no waking input'
    assert input_receipt.confirmed(seat.harness, record, human_wake.LINE, AT) is None
