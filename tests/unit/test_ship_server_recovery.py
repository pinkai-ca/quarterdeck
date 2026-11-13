"""The ship's own tmux server proved gone: exactly the formerly-up seats come back, at most twice an hour.

Real sockets prove gone, refused and answering; real processes prove a launch's owner dead or alive; the lifecycle
locks and the durable state are the framework's own. Only Seat.up and is_up are stood in, so nothing launches.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import post, seat as seatmod, server_recovery as recovery, ship as shipmod


def dead_pid() -> int:
    child = subprocess.Popen([sys.executable, '-c', ''])
    child.wait()
    return child.pid


class Server:
    """A stand-in tmux handle naming one explicit socket path."""

    def __init__(self, path: Path):
        self.socket = str(path)
        self.listener = None

    def answer(self):
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(self.socket)
        self.listener.listen(1)

    def refuse(self):  # a socket file with nobody listening, as a killed server leaves
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(self.socket)
        stale.close()

    def close(self):
        if self.listener:
            self.listener.close()


@pytest.fixture
def ship(drydock, tmp_path, monkeypatch):
    loaded = shipmod.load(drydock)
    loaded.seats['spock'] = shipmod.replace(loaded.seat('captain'), name='spock', role='systems', persona=None) \
        if hasattr(shipmod, 'replace') else loaded.seats['captain']
    upped, calls = set(), []

    def up(self, resume=True, **kwargs):
        calls.append((self.name, resume, [a['outcome'] for a in recovery.read(self.ship.runtime)['attempts']]))
        if self.name in fail:
            raise shipmod.ShipError(f'synthetic launch failure for {self.name}')
        if self.name not in silent:
            upped.add(self.name)
        return f'{self.name} up (synthetic)'
    fail, silent = set(), set()
    monkeypatch.setattr(seatmod.Seat, 'up', up)
    monkeypatch.setattr(seatmod.Seat, 'is_up', property(lambda self: self.name in upped))
    server = Server(Path(os.environ.get('TMUX_TMPDIR', str(tmp_path))) / f'w3-{os.getpid()}-{id(loaded)}.sock')
    store = post.Store(loaded.runtime)
    service = SimpleNamespace(ship=loaded, tmux=server, store=store, _up=lambda name: name in upped)
    yield SimpleNamespace(ship=loaded, server=server, service=service, calls=calls, upped=upped, fail=fail,
                          silent=silent, store=store, runtime=loaded.runtime)
    server.close()
    Path(server.socket).unlink(missing_ok=True)


def launch(ship, name, pid=None):
    folder = ship.runtime / 'seats' / name
    folder.mkdir(parents=True, exist_ok=True)
    seatmod.Launched('%9', pid or dead_pid(), datetime.now(timezone.utc).isoformat(), 'fake', None,
                     ['qd-fakeharness'], 'synthetic', harness='fake').write(folder / 'launch.json')


def reports(case):
    return [m.body for m in case.store.all() if m.sender == 'ship' and m.kind == 'report']


def test_a_gone_server_brings_back_exactly_the_formerly_up_seats_after_the_attempt_is_saved(ship):
    launch(ship.ship, 'captain')
    launch(ship.ship, 'worker-1')  # desk was never up, or was taken down: no launch.json, never resurrected
    assert recovery.pass_once(ship.service) == 'recovered'
    assert [(name, resume) for name, resume, _ in ship.calls] == [('captain', True), ('worker-1', True)]
    assert all(outcomes == ['started'] for _, _, outcomes in ship.calls), 'consumed before any effect'
    data = recovery.read(ship.runtime)
    assert [a['outcome'] for a in data['attempts']] == ['recovered'] and data['stopped'] is None
    assert any('recovered 2 seat(s) (captain, worker-1), attempt 1 of 2' in body for body in reports(ship))


def test_a_refusing_socket_is_gone_too_and_an_answering_one_is_left_alone(ship):
    launch(ship.ship, 'captain')
    ship.server.answer()
    assert recovery.pass_once(ship.service) is None and ship.calls == []
    ship.server.close()
    Path(ship.server.socket).unlink()
    ship.server.listener = None
    ship.server.refuse()
    assert recovery.server_gone(ship.server)[0] is True
    assert recovery.pass_once(ship.service) == 'recovered'


@pytest.mark.parametrize('how', ['live old pid', 'not a socket', 'no explicit socket, not answering'])
def test_anything_short_of_proof_is_unknown_starts_nothing_and_uses_no_attempt(ship, how):
    launch(ship.ship, 'captain', pid=os.getpid() if how == 'live old pid' else None)
    if how == 'not a socket':
        Path(ship.server.socket).write_text('not a socket')
    if how.startswith('no explicit'):
        ship.service.tmux = SimpleNamespace(socket=None, running=lambda: False)
    said = recovery.pass_once(ship.service)
    assert said.startswith('unknown: ') and ship.calls == []
    assert recovery.read(ship.runtime)['attempts'] == []
    assert recovery.status(ship.runtime) == [f'ship server unknown: {said[len("unknown: "):]}']


def test_a_default_server_that_answers_is_never_reported_unknown(ship):
    launch(ship.ship, 'captain')
    ship.service.tmux = SimpleNamespace(socket=None, running=lambda: True)
    assert recovery.pass_once(ship.service) is None and recovery.status(ship.runtime) == []


def test_an_undeclared_seat_with_a_launch_is_left_alone_and_named(ship):
    launch(ship.ship, 'captain')
    launch(ship.ship, 'old-seat-yarrow')
    assert recovery.pass_once(ship.service) == 'recovered'
    assert [name for name, _, _ in ship.calls] == ['captain']
    assert any('not declared, left alone: old-seat-yarrow' in body for body in reports(ship))


def test_a_busy_lifecycle_defers_without_using_an_attempt(ship):
    launch(ship.ship, 'captain')
    seat = seatmod.Seat(ship.ship, ship.ship.seat('captain'), ship.server)
    with seat._lifecycle(automatic=True):  # a person's qd act holds it
        said = recovery.pass_once(ship.service)
    assert said.startswith('unknown: captain lifecycle busy') and ship.calls == []
    assert recovery.read(ship.runtime)['attempts'] == []
    assert recovery.pass_once(ship.service) == 'recovered', 'the next pass, with the lock free'


def test_two_attempts_an_hour_then_stopped_until_a_person_runs_qd_up(ship):
    for _ in range(2):
        launch(ship.ship, 'captain')
        ship.upped.clear()
        assert recovery.pass_once(ship.service) == 'recovered'
    launch(ship.ship, 'captain')
    ship.upped.clear()
    calls = len(ship.calls)
    assert recovery.pass_once(ship.service) == 'stopped' and len(ship.calls) == calls, 'no third bring-up'
    assert recovery.status(ship.runtime) == ['ship server gone: recovery stopped (2 attempts in the last hour); '
                                             'needs a human: qd up']
    assert recovery.pass_once(ship.service) is None, 'stopped stays stopped'
    assert any('recovery stopped' in body for body in reports(ship))
    assert recovery.clear_by_person(ship.runtime) == 'ship server recovery cleared by qd up'
    assert recovery.status(ship.runtime) == []
    assert recovery.pass_once(ship.service) == 'recovered', 'a person cleared it: the hour starts again'


def test_attempts_older_than_an_hour_no_longer_count(ship, monkeypatch):
    old = datetime.now(timezone.utc) - timedelta(hours=1, minutes=1)
    recovery.save(ship.runtime, dict(format=1, stopped=None, unknown=None, attempts=[
        dict(at=old.isoformat(), outcome='recovered', seats=['captain'])] * 2))
    launch(ship.ship, 'captain')
    assert recovery.pass_once(ship.service) == 'recovered'


def test_a_clock_that_reads_earlier_than_an_attempt_counts_as_exhausted(ship):
    later = datetime.now(timezone.utc) + timedelta(minutes=10)
    recovery.save(ship.runtime, dict(format=1, stopped=None, unknown=None, attempts=[
        dict(at=later.isoformat(), outcome='recovered', seats=['captain'])]))
    launch(ship.ship, 'captain')
    assert recovery.pass_once(ship.service) == 'stopped' and ship.calls == []
    assert 'clock reads earlier' in recovery.read(ship.runtime)['stopped']


def test_a_crash_after_the_attempt_is_saved_still_counts(ship, monkeypatch):
    launch(ship.ship, 'captain')
    monkeypatch.setattr(shipmod, 'fence', lambda s: (_ for _ in ()).throw(SystemExit('synthetic crash')))
    with pytest.raises(SystemExit):
        recovery.pass_once(ship.service)
    assert [a['outcome'] for a in recovery.read(ship.runtime)['attempts']] == ['started']
    assert recovery.window(recovery.read(ship.runtime), recovery.now()) == (1, False)


@pytest.mark.parametrize('mode', ['raises', 'not up after'])
def test_a_bring_up_that_is_not_whole_fails_once_names_the_seat_and_stops(ship, mode):
    launch(ship.ship, 'captain')
    launch(ship.ship, 'worker-1')
    (ship.fail if mode == 'raises' else ship.silent).add('worker-1')
    assert recovery.pass_once(ship.service) == 'failed'
    assert [name for name, _, _ in ship.calls] == ['captain', 'worker-1'], 'every seat still gets its one try'
    data = recovery.read(ship.runtime)
    assert data['attempts'][-1]['outcome'].startswith('failed: worker-1')
    assert data['stopped'].startswith('recovery failed: worker-1')
    assert recovery.pass_once(ship.service) is None and len(ship.calls) == 2, 'never repeated'


def test_unreadable_state_is_never_read_as_empty(ship):
    recovery.state_path(ship.runtime).parent.mkdir(parents=True, exist_ok=True)
    recovery.state_path(ship.runtime).write_text('{ not json')
    with pytest.raises(shipmod.ShipError):
        recovery.pass_once(ship.service)
    recovery.state_path(ship.runtime).write_text(json.dumps(dict(format=2, attempts=[])))
    with pytest.raises(shipmod.ShipError):
        recovery.pass_once(ship.service)
    assert recovery.status(ship.runtime)[0].startswith('ship server recovery state unreadable')


def test_the_status_line_carries_a_stopped_recovery(ship):
    from quarterdeck import watch
    recovery.save(ship.runtime, dict(format=1, stopped='2 attempts in the last hour', unknown=None, attempts=[]))
    facts = watch.Facts(service='ok', waiting=0, oldest=None, blind=0, server_recovery=recovery.status(ship.runtime))
    assert 'ship server gone: recovery stopped (2 attempts in the last hour); needs a human: qd up' in facts.line()
