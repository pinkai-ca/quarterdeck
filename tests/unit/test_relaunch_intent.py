"""A relaunch the service makes is a section a stop waits out; its intent outlives a service killed inside it."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import quarterdeck
from quarterdeck import relaunch_intent, servicectl, shutdown, watch

DEAD = dict(version=1, pid=999999999, start='synthetic-start-zzzz', token='synthetic-owner-zzzz', device=1, inode=1)


def service(runtime, seats=('worker-1',)):
    logs = []
    ship = SimpleNamespace(runtime=runtime, seats={name: None for name in seats})

    def seat(name):
        pytest.fail(f'{name}: a refused intent reached a seat')

    return SimpleNamespace(runtime=runtime, ship=ship, logs=logs, seat=seat,
                           log=lambda event, **data: logs.append((event, data)))


class Seat:
    """A stand-in whose relaunch is its down and its up, with a stop arriving between them when asked."""

    def __init__(self, runtime, stop_between=False):
        self.name, self.runtime, self.stop_between = 'worker-1', runtime, stop_between
        self.ship = SimpleNamespace(runtime=runtime)
        self.events = []

    def launched(self):
        return SimpleNamespace(at='2000-01-01T00:00:00+00:00')

    def fresh(self):
        self.events.append('down')
        self.held = relaunch_intent.held_by(self.runtime, getattr(shutdown._controller, 'owner', None))
        if self.stop_between:
            os.kill(os.getpid(), signal.SIGTERM)  # the installed handler, as a sync's stop reaches the service
        self.events.append('up')
        return 'worker-1 up'


def intent(runtime, seat='worker-1', name=None, **extra):
    folder = relaunch_intent.folder(runtime)
    folder.mkdir(parents=True, exist_ok=True)
    data = dict(version=1, seat=seat, kind='after-order', resume=False, at=datetime.now(timezone.utc).isoformat(),
                phase=relaunch_intent.RELAUNCHING, owner=DEAD, before=None, **extra)
    path = folder / (name or f'{seat}.json')
    path.write_text(json.dumps(data) + '\n')
    return path


def test_a_stop_between_down_and_up_leaves_the_seat_up(tmp_path):
    runtime = tmp_path / 'runtime'
    svc, seat = service(runtime), Seat(runtime, stop_between=True)
    with shutdown.Stop(runtime).installed():
        with pytest.raises(SystemExit) as exit:
            with relaunch_intent.section(svc, seat, 'after-order', resume=False):
                seat.fresh()
        assert exit.value.code == 0
    assert seat.events == ['down', 'up'], 'the stop waited for the up'
    assert seat.held == ['worker-1'], 'the stop saw the open relaunch and waits for it'
    assert not relaunch_intent.path(runtime, 'worker-1').exists(), 'an up that happened leaves no intent'


def test_both_relaunch_lines_are_logged_around_the_down_and_the_up(tmp_path):
    runtime = tmp_path / 'runtime'
    svc, seat = service(runtime), Seat(runtime)
    order = []
    svc.log = lambda event, **data: order.append((event, data.get('phase')))
    original = seat.fresh
    seat.fresh = lambda: (order.append(('fresh', None)), original())[1]
    with shutdown.Stop(runtime).installed():
        with relaunch_intent.section(svc, seat, 'after-order', resume=False):
            seat.fresh()
    assert order == [('relaunch', 'begin'), ('fresh', None), ('relaunch', 'done')]


def test_without_a_service_the_lines_go_to_the_service_log_and_resume_is_read_once(tmp_path):
    runtime = tmp_path / 'runtime'
    seat, asked = Seat(runtime), []
    with shutdown.Stop(runtime).installed():
        with relaunch_intent.section(None, seat, 'compaction', resume=lambda: asked.append(1) or True):
            data = json.loads(relaunch_intent.path(runtime, 'worker-1').read_text())
            seat.fresh()
    assert data['resume'] is True and asked == [1]
    logged = [json.loads(line) for line in (runtime / 'log' / 'service.jsonl').read_text().splitlines()]
    assert [(entry['event'], entry['phase']) for entry in logged] == [('relaunch', 'begin'), ('relaunch', 'done')]


def test_a_stop_already_asked_starts_no_relaunch(tmp_path):
    runtime = tmp_path / 'runtime'
    svc, seat = service(runtime), Seat(runtime)
    with shutdown.Stop(runtime).installed():
        shutdown._controller.requested = True
        with pytest.raises(SystemExit):
            with relaunch_intent.section(svc, seat, 'after-order', resume=False):
                pytest.fail('a relaunch began after the stop was asked')
    assert not relaunch_intent.folder(runtime).exists() and svc.logs == []


def test_outside_a_running_service_the_section_writes_nothing(tmp_path):
    runtime = tmp_path / 'runtime'
    svc, seat = service(runtime), Seat(runtime)
    with relaunch_intent.section(svc, seat, 'relaunch', resume=True):
        seat.fresh()
    assert seat.events == ['down', 'up'] and svc.logs == [] and not relaunch_intent.folder(runtime).exists()


CHILD = r'''
import os, sys, time
from pathlib import Path
from types import SimpleNamespace
from quarterdeck import relaunch_intent, servicectl, shutdown
runtime = Path(sys.argv[1])
svc = SimpleNamespace(runtime=runtime, log=lambda event, **data: None)
seat = SimpleNamespace(name='worker-1', ship=SimpleNamespace(runtime=runtime), launched=lambda: None)
with shutdown.Stop(runtime).installed():
    servicectl.write_heartbeat(runtime)
    with relaunch_intent.section(svc, seat, 'after-order', resume=False):
        (runtime / 'ready').write_text('ready')
        time.sleep(120)  # an up that hangs
'''


def test_the_bound_releases_a_held_stop_leaves_the_intent_and_shows_on_the_line(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    env = dict(os.environ, PYTHONPATH=str(Path(quarterdeck.__file__).resolve().parents[1]))
    child = subprocess.Popen([sys.executable, '-c', CHILD, str(runtime)], env=env, start_new_session=True)
    try:
        deadline = time.monotonic() + 30
        while not (runtime / 'ready').exists():
            assert child.poll() is None, 'the synthetic service ended before its relaunch'
            assert time.monotonic() < deadline
            time.sleep(0.05)
        owner = shutdown.owner(runtime)
        assert relaunch_intent.held_by(runtime, owner) == ['worker-1']
        monkeypatch.setattr(shutdown, 'ORDINARY_GRACE', 1.0)  # the ordinary bound low, so the relaunch's shows past it
        monkeypatch.setattr(relaunch_intent, 'GRACE', shutdown.ORDINARY_GRACE + 2.0)  # a bound past the ordinary
        started = time.monotonic()
        proof = servicectl.stop(runtime)
        waited = time.monotonic() - started
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)
    assert 'service killed' in proof and 'relaunch boundary' in proof
    assert 'relaunch of worker-1 left unfinished for the next service' in proof
    assert relaunch_intent.GRACE <= waited < relaunch_intent.GRACE + 5, 'held for the bound, then released'
    data = json.loads(relaunch_intent.path(runtime, 'worker-1').read_text())
    assert data['phase'] == relaunch_intent.RELEASED and data['owner'] == owner, 'the intent stays for the next'
    logged = [json.loads(line) for line in (runtime / 'log' / 'service.jsonl').read_text().splitlines()]
    assert [entry['event'] for entry in logged] == ['relaunch-released'] and logged[0]['seat'] == 'worker-1'
    ship = SimpleNamespace(runtime=runtime, seats={'worker-1': None})
    line = watch.Facts('down', 0, 0, 0, relaunches=relaunch_intent.status(ship)).line()
    assert 'worker-1: relaunch at' in line and 'released by a stop that waited for it' in line
    assert 'the next service finishes its up' in line


def test_a_link_at_the_intents_place_is_refused_and_left(tmp_path):
    runtime = tmp_path / 'runtime'
    real = intent(runtime, name='elsewhere.txt')
    link = relaunch_intent.path(runtime, 'worker-1')
    link.symlink_to(real)
    svc = service(runtime)
    relaunch_intent.finish(svc)
    relaunch_intent.finish(svc)
    assert [event for event, data in svc.logs] == ['relaunch-intent'], 'refused once, never followed'
    assert svc.logs[0][1]['outcome'] == 'refused' and 'link' in svc.logs[0][1]['reason']
    assert link.is_symlink(), 'a link is a hold, left where it is'
    assert any('a link at its place' in line for line in relaunch_intent.status(svc.ship))
    with pytest.raises(Exception):
        relaunch_intent.read(runtime, 'worker-1.json', {'worker-1'})


@pytest.mark.parametrize('file_seat,data_seat', [('intruder', 'intruder'), ('worker-1', 'intruder')])
def test_an_undeclared_seat_in_an_intent_is_refused_and_never_launched(tmp_path, file_seat, data_seat):
    runtime = tmp_path / 'runtime'
    intent(runtime, seat=data_seat, name=f'{file_seat}.json')
    svc = service(runtime)  # its seat() fails the test if any seat is made from the intent
    relaunch_intent.finish(svc)
    assert [data['outcome'] for event, data in svc.logs] == ['refused']
    assert any('refused' in line for line in relaunch_intent.status(svc.ship))


def test_an_intent_whose_service_still_runs_is_left_to_it(tmp_path):
    runtime = tmp_path / 'runtime'
    alive = dict(DEAD, pid=os.getpid(), start=shutdown.process_start(os.getpid()))
    folder = relaunch_intent.folder(runtime)
    path = intent(runtime)
    data = json.loads(path.read_text())
    path.write_text(json.dumps({**data, 'owner': alive}) + '\n')
    svc = service(runtime)
    relaunch_intent.finish(svc)
    assert svc.logs == [] and path.exists() and relaunch_intent.status(svc.ship) == []
    assert folder.is_dir()
