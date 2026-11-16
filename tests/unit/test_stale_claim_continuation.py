"""A model control typed before a service stop is resumed by the next service, never settled unapplied."""

from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from quarterdeck import post, request_queue, servicectl
from quarterdeck.service import Service

HERE = Path(__file__).resolve().parent
SESSION, PRIOR, TARGET = 'synthetic-first-conversation', 'synthetic-prior', 'synthetic-next'


def synthetic_seat(root: Path, when: str, name='synthetic-seat'):
    """A first-launch fake seat, built alike by the stopped service and the next one."""
    from quarterdeck.harness.fake import FakeHarness
    from quarterdeck.seat import Launched

    harness = FakeHarness()
    launched = Launched('synthetic-pane', 123, when, 'synthetic-pool', None, ['fake'], 'fake', harness='fake')
    home = root / 'home'
    frame = '\n'.join(['─' * 40, '> ', '─' * 40, f'fake claude · model {TARGET} · effort low · idle'])
    return SimpleNamespace(name=name, harness=harness, home=home,
                           record=lambda: harness.join(home, launched.when).record,
                           launched=lambda: launched, read_pane=lambda: (object(), frame),
                           spec=SimpleNamespace(settings={'as': 'claude'}, model_mode='named', model=TARGET))


def service(runtime, answer):
    runner = Service.__new__(Service)
    runner.runtime = runtime
    runner.log_path = runtime / 'log' / 'service.jsonl'
    runner.quick_request_scan = runner.quick_request_retry = None
    runner.stale_claims_pending = True
    runner.copy_identity = runner.copy_start_token = None
    runner.heartbeat_lock = threading.Lock()
    runner.watch_credentials = lambda stop: None
    runner.ship = SimpleNamespace(tick=0.01)
    runner.answer = answer
    return runner


class EndPass(Exception):
    pass


CHILD = '''
import signal, sys
from pathlib import Path
sys.path[:0] = [sys.argv[1], sys.argv[2]]
from test_stale_claim_continuation import service, synthetic_seat
from quarterdeck import servicectl
from quarterdeck.service import Service
root, runtime = Path(sys.argv[3]), Path(sys.argv[4])
seat = synthetic_seat(root, sys.argv[5], sys.argv[6])
typed = root / 'typed.jsonl'

def deliver(command, confirm):
    assert confirm is False
    with typed.open('a') as out:
        out.write(command + '\\n')
    return type('Delivery', (), dict(result='typed'))()

door = type('Door', (), dict(deliver=staticmethod(deliver)))()
runner = None

def apply(data):
    result = Service.begin_model_control(runner, seat, seat.harness.control('model'), data['value'], door)
    assert result is None, result  # typed, its record not yet confirmed
    print('typed-before-confirmation', flush=True)
    signal.pause()
    raise AssertionError('the stopped service must never finish this control')

runner = service(runtime, apply)
runner.tick = runner.requests
servicectl.write_heartbeat(runtime)
runner.run()
'''


def typed_then_stopped(root: Path, runtime: Path, seat_name: str):
    """A real service loop types the model control, and is stopped before its record confirms it."""
    when = (post.now() - timedelta(seconds=1)).isoformat()
    path = request_queue.enqueue(runtime, 'control', dict(seat=seat_name, name='model', value=TARGET), name='model')
    child = subprocess.Popen([sys.executable, '-c', CHILD, str(Path(request_queue.__file__).parents[1]), str(HERE),
                              str(root), str(runtime), when, seat_name],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             start_new_session=True)  # the stop binds a service by its own private session
    try:
        with selectors.DefaultSelector() as ready:
            ready.register(child.stdout, selectors.EVENT_READ)
            assert ready.select(20), 'service did not type its control'
        assert child.stdout.readline().strip() == 'typed-before-confirmation', child.stderr.read()
        meta = request_queue.metadata(path)
        assert meta['claim_owner']['pid'] == child.pid
        assert request_queue.control_continuation(path).phase == 'submitted'
        assert 'service stopped' in servicectl.stop(runtime)
        child.communicate(timeout=10)
        assert child.returncode == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=5)
    # The claiming service is proved gone, and its claim carries the typed control's continuation.
    assert request_queue.stale_claim(path)
    typed = (root / 'typed.jsonl').read_text().splitlines()
    assert typed == [f'/model {TARGET}']
    return path, when, typed


def resumed_by_next_service(root, runtime, path, when, typed, seat_name, monkeypatch, *, confirmed):
    started = datetime.fromisoformat(request_queue.control_continuation(path).started)
    # An injected clock: within the confirmation window, or past its deadline. No wall time decides.
    clock = started + timedelta(seconds=1 if confirmed else 31)
    monkeypatch.setattr(post, 'now', lambda: clock)
    if confirmed:
        from quarterdeck.harness.synthetic_record import ClaudeShape
        ClaudeShape(root / 'home', root, SESSION, PRIOR).command(
            typed[0], f'Set model to {TARGET} and saved as your default for new sessions')

    seat = synthetic_seat(root, when, seat_name)
    resumed = []
    runner = service(runtime, None)

    def answer(data):
        assert 'control_confirmation' in runner.request_metadata, 'a continuation-less control would be retyped'
        resumed.append(data['value'])
        return Service.control(runner, seat, data['name'], data['value'])

    runner.answer = answer

    def first_tick():
        runner.requests()
        raise EndPass

    runner.tick = first_tick
    try:
        with pytest.raises(EndPass):
            runner.run()
    finally:
        if hasattr(runner, 'lock'):
            runner.lock.close()

    done = path.with_suffix('.done').read_text().strip()
    assert request_queue.RESTART_SETTLEMENT not in done and 'never applied' not in done
    assert resumed == [TARGET]
    if confirmed:
        assert done.startswith(f'model = {TARGET} · applied by /model {TARGET}') and 'confirmed by' in done
    else:
        assert done.startswith(f'model = {TARGET} · ') and 'unconfirmed' in done
    assert not path.exists() and not path.with_suffix('.claimed').exists()
    assert (root / 'typed.jsonl').read_text().splitlines() == typed  # never typed again
    events = [json.loads(line) for line in runner.log_path.read_text().splitlines()]
    assert not [event for event in events if event['event'] == 'request-settled']


@pytest.mark.parametrize('confirmed', [True, False])
def test_a_typed_model_control_survives_a_service_stop_and_is_resumed_not_settled(tmp_path, monkeypatch, confirmed):
    runtime = tmp_path / 'runtime'
    path, when, typed = typed_then_stopped(tmp_path, runtime, 'synthetic-seat')
    resumed_by_next_service(tmp_path, runtime, path, when, typed, 'synthetic-seat', monkeypatch, confirmed=confirmed)


def test_cancel_refuses_a_typed_model_control_and_the_next_service_confirms_it(drydock, tmp_path, monkeypatch, capsys):
    from quarterdeck import cli
    from quarterdeck.ship import load

    monkeypatch.delenv('QD_TMUX_SOCKET', raising=False)
    runtime = load(drydock).runtime
    path, when, typed = typed_then_stopped(tmp_path, runtime, 'worker-1')
    before = {p: p.read_bytes() for p in (path, path.with_suffix('.claimed'), request_queue.state_path(path))}
    assert cli.main(['--ship', str(drydock), 'restart', 'worker-1', '--cancel', path.stem]) == 2
    err = capsys.readouterr().err
    assert 'its command may already be typed; the service confirms it' in err and 'never applied' not in err
    # Without an id, cancel withdraws only pending relaunches: the control is not one of them.
    assert cli.main(['--ship', str(drydock), 'restart', 'worker-1', '--cancel']) == 0
    assert 'no pending relaunch; nothing cancelled' in capsys.readouterr().out
    assert {p: p.read_bytes() for p in before} == before
    assert not path.with_suffix('.done').exists()
    resumed_by_next_service(tmp_path, runtime, path, when, typed, 'worker-1', monkeypatch, confirmed=True)
