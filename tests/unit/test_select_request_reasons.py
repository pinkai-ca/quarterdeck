"""What a person asking for a pool move reads while the move waits: synthetic request files, no service or seat."""

import pytest

from quarterdeck import request_queue, servicectl


@pytest.fixture
def asking(tmp_path, monkeypatch):
    """A client filing a select while a stand-in service records one hold on its first wait, then releases it."""
    monkeypatch.setattr(servicectl, 'alive', lambda runtime: True)
    monkeypatch.setattr(servicectl, 'stale', lambda runtime: None)
    held = {}

    def service_pass(_seconds):
        if held.get('hold') and not held.get('done'):
            path, = (tmp_path / 'requests').glob('*.json')
            meta = request_queue.metadata(path)
            if held['hold'][0] == 'tools-unknown':
                meta.update(select_wait=True)  # the idle proof's own wait, as Service.select_request records it
            request_queue.hold(path, meta, *held['hold'])
            held['done'] = True

    monkeypatch.setattr(servicectl.time, 'sleep', service_pass)
    monkeypatch.setattr(servicectl, 'ANSWER_SECONDS', 0.5)
    return tmp_path, held


@pytest.mark.parametrize('hold', [
    ('tools-unknown', 'move to pool primary: tool state unknown'),
    ('queue-unknown', 'move to pool primary: queue unknown: task notification queue unresolved'),
    ('selection', 'declaration changed since this pass loaded it'),
])
def test_the_first_recorded_hold_reaches_the_asker_whatever_its_kind(asking, hold):
    runtime, held = asking
    held['hold'] = hold
    answer = servicectl.request(runtime, 'select', seat='person', pool='primary', origin='launch')
    assert answer.startswith(f'accepted: person waits: {hold[1]} (request ')
    assert 'qd status and qd watch person show each later reason' in answer
    assert 'cancel: qd restart person --cancel ' in answer


def test_no_hold_recorded_reads_as_no_decision_yet(asking):
    runtime, _ = asking
    answer = servicectl.request(runtime, 'select', seat='person', pool='primary', origin='launch')
    assert answer.startswith('pending: the service has not answered')
    assert 'service has not supplied its decision yet' in answer


def test_a_later_stage_replaces_the_reason_and_cancellation_settles_it(tmp_path):
    path = request_queue.enqueue(tmp_path, 'select', dict(seat='person', pool='primary', origin='launch'))
    assert request_queue.pending(tmp_path, 'person')[0]['reason'] == 'service has not supplied its decision yet'
    stages = [('undecided', 'the service has not yet measured what the request waits for'),
              ('queue-unknown', 'move to pool primary: queue unknown: task notification queue unresolved'),
              ('box', 'move to pool primary: the composer is not empty')]
    for dimension, reason in stages:
        request_queue.hold(path, request_queue.metadata(path), dimension, reason)
        row, = request_queue.pending(tmp_path, 'person')
        assert (row['dimension'], row['reason'], row['kind']) == (dimension, reason, 'select')
        assert f'waiting: {reason}' in request_queue.describe(row)
    assert 'cancelled request(s)' in request_queue.cancel(tmp_path, 'person', path.stem)
    assert request_queue.pending(tmp_path, 'person') == []
