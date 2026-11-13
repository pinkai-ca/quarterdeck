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


def test_first_fake_model_input_confirms_only_in_its_new_launch_record(tmp_path):
    from datetime import timedelta
    from quarterdeck.harness.fake import FakeHarness
    from quarterdeck.harness.synthetic_record import ClaudeShape
    from quarterdeck.seat import Launched

    harness = FakeHarness()
    launched = Launched('synthetic-pane', 123, (post.now() - timedelta(seconds=1)).isoformat(),
                        'synthetic-pool', None, ['fake'], 'fake', harness='fake')
    home = tmp_path / 'home'
    writer = ClaudeShape(home, tmp_path, 'synthetic-first-conversation', 'synthetic-prior')
    assert not writer.path.exists()
    pinned = dict(record=None)

    def record():
        if pinned['record'] is None:
            pinned['record'] = harness.join(home, launched.when).record
        return pinned['record']

    frame = '\n'.join(['─' * 40, '> ', '─' * 40,
                       'fake claude · model synthetic-next · effort low · idle'])
    seat = SimpleNamespace(name='synthetic-seat', harness=harness, home=home, record=record,
                           launched=lambda: launched, read_pane=lambda: (object(), frame),
                           spec=SimpleNamespace(settings={'as': 'claude'}, model_mode='named', model='synthetic-next'))
    folder = tmp_path / 'requests'
    folder.mkdir()
    path = folder / 'first-model.json'
    path.write_text(json.dumps(dict(kind='control', seat=seat.name, name='model', value='synthetic-next')))
    service = SimpleNamespace(runtime=tmp_path, log=lambda *args, **kwargs: None)
    service.clear_request_claim = MethodType(Service.clear_request_claim, service)
    service.failed_request = MethodType(Service.failed_request, service)
    service.continue_model_control = MethodType(Service.continue_model_control, service)
    commands = []

    def deliver(command, confirm):
        assert confirm is False and record() is None
        commands.append(command)
        writer.command(command, 'Set model to synthetic-next and saved as your default for new sessions')
        return SimpleNamespace(result='typed')

    door = SimpleNamespace(deliver=deliver)
    service.answer = lambda data: Service.begin_model_control(
        service, seat, harness.control('model'), data['value'], door)
    Service.requests(service)
    assert commands == ['/model synthetic-next']
    assert record() == writer.path
    assert 'record confirmed' in path.with_suffix('.done').read_text()
    assert not path.exists() and not path.with_suffix('.claimed').exists()

    later = Launched('synthetic-later-pane', 456, (post.now() + timedelta(seconds=1)).isoformat(),
                     'synthetic-pool', None, ['fake'], 'fake', harness='fake')
    old = SimpleNamespace(spec=seat.spec, launched=lambda: later,
                          record=lambda: harness.join(home, later.when).record)
    assert old.record() is None
    origin = harness.model_control_origin(old, None, 'synthetic-next')
    assert origin is not None and origin['path'] is None
    assert harness.model_control_join(old, writer.path, origin) is None
    assert harness.model_control_result(old, writer.path, 'synthetic-next', origin) is None
