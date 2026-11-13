"""Ordinary queue progress while one submitted model control awaits its record."""
import json
from types import MethodType, SimpleNamespace

from quarterdeck import control_confirmation as confirmation, post, request_queue
from quarterdeck.service import Service


def test_submitted_control_keeps_its_claim_and_later_requests_still_finish(tmp_path):
    folder = tmp_path / 'requests'
    folder.mkdir()
    pending = folder / 'first-model.json'
    healthy = folder / 'later-work.json'
    pending.write_text(json.dumps(dict(kind='control', seat='synthetic-seat', name='model', value='next-model')))
    healthy.write_text(json.dumps(dict(kind='synthetic-operation')))
    service = SimpleNamespace(runtime=tmp_path, log=lambda *args, **kwargs: None)
    service.clear_request_claim = MethodType(Service.clear_request_claim, service)
    service.failed_request = MethodType(Service.failed_request, service)
    observations, commands = [], []

    def answer(data):
        if data['kind'] != 'control':
            return 'healthy request completed'
        observations.append(data['value'])
        if len(observations) == 1:
            commands.append('synthetic command transport')
            origin = dict(
                seat='synthetic-seat', declaration=['synthetic-program', 'named', 'next-model'],
                launch=dict(pane='synthetic-pane', pid=123, at='2000-01-01T00:00:00+00:00', harness='synthetic-program'),
                inputs=dict(record='synthetic-record', device=1, inode=2, input_count=0, inputs_digest='a' * 64),
                record=dict(path='synthetic-record', device=1, inode=2, offset=0, prefix='b' * 64,
                            display=dict(display='Next')))
            state = confirmation.submitted(confirmation.Continuation.begin('model', 'next-model', origin, post.now()))
            service.request_metadata['control_confirmation'] = state.metadata()
            request_queue.save_metadata(service.request_path, service.request_metadata)
            service.request_wait = ('control-confirmation', 'waiting for the synthetic record')
            return None
        assert request_queue.control_continuation(service.request_path).phase == 'submitted'
        return 'record confirmed'

    service.answer = answer
    Service.requests(service)
    assert pending.exists() and pending.with_suffix('.claimed').exists()
    assert not pending.with_suffix('.done').exists()
    assert healthy.with_suffix('.done').read_text() == 'healthy request completed\n'
    assert len(commands) == 1
    Service.requests(service)
    assert pending.with_suffix('.done').read_text() == 'record confirmed\n'
    assert not pending.exists() and not pending.with_suffix('.claimed').exists()
    assert not request_queue.state_path(pending).exists()
    assert observations == ['next-model', 'next-model']
    assert len(commands) == 1
