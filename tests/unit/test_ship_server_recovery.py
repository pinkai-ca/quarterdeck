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

from quarterdeck import credential, jail_end, lifecycle, post, relaunch_intent, shutdown, trusted_files
from quarterdeck import seat as seatmod, server_recovery as recovery, ship as shipmod
from quarterdeck.harness.claude import ClaudeHarness
from quarterdeck.harness.codex import CodexHarness
from quarterdeck.harness.limits import Usage, Window
from quarterdeck.harness.synthetic_record import ClaudeShape, CodexShape
from quarterdeck.service import Service
from quarterdeck.tmux import Pane


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
    loaded.seats['sentry'] = shipmod.replace(loaded.seat('captain'), name='sentry', role='systems', persona=None) \
        if hasattr(shipmod, 'replace') else loaded.seats['captain']
    upped, calls = set(), []

    def up(self, resume=True, **kwargs):
        try:  # recovery holds this seat's lifecycle lock for the whole effect: a concurrent act is refused
            with self._lifecycle(automatic=True):
                held.append(False)
        except shipmod.ShipError:
            held.append(True)
        calls.append((self.name, resume, [a['outcome'] for a in recovery.read(self.ship.runtime)['attempts']]))
        if self.name in fail:
            raise shipmod.ShipError(f'synthetic launch failure for {self.name}')
        if self.name not in silent:
            upped.add(self.name)
        return f'{self.name} up (synthetic)'
    fail, silent, held = set(), set(), []
    monkeypatch.setattr(seatmod.Seat, '_up', up)
    monkeypatch.setattr(seatmod.Seat, 'is_up', property(lambda self: self.name in upped))
    server = Server(Path(os.environ.get('TMUX_TMPDIR', str(tmp_path))) / f'w3-{os.getpid()}-{id(loaded)}.sock')
    store = post.Store(loaded.runtime)
    service = SimpleNamespace(ship=loaded, tmux=server, store=store, _up=lambda name: name in upped)
    yield SimpleNamespace(ship=loaded, server=server, service=service, calls=calls, upped=upped, fail=fail,
                          silent=silent, store=store, runtime=loaded.runtime, held=held)
    server.close()
    Path(server.socket).unlink(missing_ok=True)


def launch(ship, name, pid=None):
    folder = ship.runtime / 'trusted' / name
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
    outcomes = [a['outcome'] for a in recovery.read(ship.runtime)['attempts']]
    assert outcomes == ['recovered', 'recovered', 'cleared by qd up'], 'the clear keeps the attempts of the hour'
    assert recovery.pass_once(ship.service) == 'stopped' and len(ship.calls) == calls, \
        'the two automatic attempts of the hour still count after a person cleared STOPPED'


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


def test_a_live_formerly_up_seat_proves_the_server_alive_and_nothing_is_probed(ship):
    launch(ship.ship, 'captain')
    launch(ship.ship, 'worker-1')
    ship.upped.add('captain')  # its pane answers: the server lives; worker-1's missing pane is the per-seat path
    ship.service.tmux = SimpleNamespace(socket=None)  # nothing to probe, and nothing is
    assert recovery.pass_once(ship.service) is None and ship.calls == [] and recovery.status(ship.runtime) == []


def test_a_clear_after_the_hour_lets_a_new_attempt_run(ship):
    old = datetime.now(timezone.utc) - timedelta(hours=1, minutes=5)
    recovery.save(ship.runtime, dict(format=1, stopped='2 attempts in the last hour', unknown=None, attempts=[
        dict(at=old.isoformat(), outcome='recovered', seats=['captain'])] * 2))
    recovery.clear_by_person(ship.runtime)
    launch(ship.ship, 'captain')
    assert recovery.pass_once(ship.service) == 'recovered'


@pytest.mark.parametrize('outcome', ['started', 'failed: synthetic'])
def test_started_and_failed_attempts_count_like_recovered_ones(ship, outcome):
    recent = datetime.now(timezone.utc) - timedelta(minutes=5)
    recovery.save(ship.runtime, dict(format=1, stopped=None, unknown=None, attempts=[
        dict(at=recent.isoformat(), outcome=outcome, seats=['captain'])] * 2))
    launch(ship.ship, 'captain')
    assert recovery.pass_once(ship.service) == 'stopped' and ship.calls == []


def test_the_effect_holds_each_seats_lifecycle_lock_and_never_takes_it_twice(ship):
    launch(ship.ship, 'captain')
    assert recovery.pass_once(ship.service) == 'recovered'
    assert ship.held == [True], 'a concurrent qd act is refused while the effect runs; no deadlock'


def test_a_deliberate_down_between_the_proof_and_the_effect_wins(ship, monkeypatch):
    launch(ship.ship, 'captain')
    launch(ship.ship, 'worker-1')
    real_now = recovery.now

    def now():  # read after every proof, before any effect: a qd down of worker-1 lands here
        (ship.runtime / 'trusted' / 'worker-1' / 'launch.json').unlink()
        return real_now()
    monkeypatch.setattr(recovery, 'now', now)
    assert recovery.pass_once(ship.service) == 'recovered'
    assert [name for name, _, _ in ship.calls] == ['captain'], 'worker-1 stays down'
    assert any('taken down meanwhile, left down: worker-1' in body for body in reports(ship))


def test_stopped_and_unknown_show_in_qd_status_with_no_server_and_none_is_started(ship, capsys, monkeypatch, synthetic_disk_room):
    from quarterdeck import cli
    from quarterdeck.tmux import ship_socket
    absent = ship_socket(ship.ship)  # the ship's own server, which qd now names; nothing has started it
    assert not absent.exists()
    monkeypatch.setenv('QD_SHIP', str(ship.ship.folder))
    monkeypatch.delenv('QD_TMUX_SOCKET', raising=False)
    recovery.save(ship.runtime, dict(format=1, stopped='2 attempts in the last hour', unknown=None, attempts=[]))
    assert cli.main(['status', '--line']) == 0
    line = capsys.readouterr().out
    assert 'ship server gone: recovery stopped (2 attempts in the last hour); needs a human: qd up' in line, line
    recovery.save(ship.runtime, dict(format=1, stopped=None, unknown='the ship server socket refuses connections', attempts=[]))
    assert cli.main(['status']) == 0
    assert 'ship server unknown: the ship server socket refuses connections' in capsys.readouterr().out
    assert not absent.exists(), 'no server was started merely to display it'


def test_a_ship_still_on_tmux_default_server_is_never_recovered_there(ship):
    """qd never starts tmux's default server: a ship that ran there waits, said, for the service to follow it."""
    launch(ship.ship, 'captain')
    ship.server.legacy = True
    assert recovery.server_gone(ship.server)[0] is True
    assert recovery.pass_once(ship.service).startswith("unknown: the ship ran on tmux's default server")
    assert ship.calls == [] and recovery.read(ship.runtime)['attempts'] == []
    assert any("tmux's default server" in line for line in recovery.status(ship.runtime))
    ship.server.legacy = False  # the service has followed the ship to its own server
    assert recovery.pass_once(ship.service) == 'recovered'
    assert recovery.status(ship.runtime) == []


@pytest.fixture
def choice_world(tmp_path, monkeypatch):
    """Real seat transitions, retained records and ledger walls, with only the process start simulated."""
    services = []

    def make(case, *, fresh=None):
        cross_program = case == 'next'
        saved = 'first' if cross_program else 'second'
        pools = ('second',) if case == 'no-choice' else ('first', 'second')
        choices = {'first': {'harness': 'codex'}, 'second': {'harness': 'claude'}} if cross_program else {}
        old_program = 'codex' if cross_program else 'claude'
        spec = shipmod.SeatSpec('builder', 'crew', 'worker', old_program, pools, pool_choices=choices, fresh=fresh)
        ship = shipmod.Ship(tmp_path / 'ship', 'synthetic', 'operator', tmp_path,
                            {name: shipmod.Pool(name, 'openai' if cross_program and name == 'first' else 'anthropic')
                             for name in pools}, {'crew': shipmod.Session('crew', 'Synthetic')}, {spec.name: spec})
        panes = {}
        tmux = SimpleNamespace(socket=str(tmp_path / 'absent-server'), pane=panes.get)
        seat = seatmod.Seat(ship, spec, tmux)
        seat.move_to(saved)
        launched = seatmod.Launched('%1', dead_pid(), (post.now() - timedelta(minutes=1)).isoformat(), saved, None,
                                    [old_program], 'synthetic', harness=old_program)
        launched.write(seat.trusted / trusted_files.LAUNCH)
        shape = CodexShape if cross_program else ClaudeShape
        record = shape(seat.home, tmp_path, 'synthetic-resume-target', 'synthetic-model')
        record.begin('Synthetic input', 'synthetic-model', 'high')
        record.end('Synthetic output')
        (seat.trusted / trusted_files.RECORD).write_text(str(record.path) + '\n')
        assert seat.record() == record.path
        svc = Service(ship, tmux)
        services.append(svc)
        at = post.now()
        if cross_program:
            svc.ledger.record_usage('first', Usage((Window('5h', 1.0, at + timedelta(hours=1)),), at))
        for pool in pools:
            if not cross_program or pool == 'second':
                svc.ledger.record_usage(pool, Usage((Window('5h', 0.2, at + timedelta(hours=1)),), at))
        starts, reseeds = [], []

        def open_window(seat, argv, env, path):
            with pytest.raises(lifecycle.Busy):
                with lifecycle.claim(ship.runtime, seat.name):
                    pytest.fail('the launch escaped its original lifecycle claim')
            starts.append((seat.pool.name, seat.harness.name, argv))
            pane = Pane('%2', 'crew', seat.name, 124, 80, 24, False, False, None)
            panes[pane.id] = pane
            return pane

        def reseed(ship, seats, **bounds):
            reseeds.extend((seat.pool.name, seat.launched().pool) for seat in seats if seat.launched())
            return []

        monkeypatch.setattr(seatmod.Seat, '_open_window', open_window)
        monkeypatch.setattr(seatmod.Seat, '_survives', lambda *_: None)
        monkeypatch.setattr(seatmod, 'process_start', lambda _pid: 'synthetic-start')
        monkeypatch.setattr(seatmod.shutil, 'which', lambda *a, **k: '/usr/bin/sh')
        monkeypatch.setattr(jail_end, 'end', lambda *_: {})
        monkeypatch.setattr(credential, 'reseed', reseed)
        for adapter in (CodexHarness, ClaudeHarness):
            monkeypatch.setattr(adapter, 'version', lambda self: 'synthetic')
        return SimpleNamespace(ship=ship, seat=seat, svc=svc, starts=starts, reseeds=reseeds, old=launched,
                               record=record, saved=saved)

    yield make
    for svc in services:
        svc.ledger.close()


@pytest.mark.parametrize('method', ['manual', 'server'])
@pytest.mark.parametrize(('case', 'target', 'resumes'), [
    ('next', 'second', False), ('earlier', 'first', True), ('no-choice', 'second', True),
])
def test_server_recovery_and_qd_up_use_the_same_pool_choice(choice_world, case, target, resumes, method):
    w = choice_world(case)
    if method == 'server':
        assert recovery.pass_once(w.svc) == 'recovered'
    else:
        proof = w.seat.up()
        if case == 'no-choice':
            assert proof.startswith('builder up in')
        else:
            assert proof.startswith(f'builder: launched on {target}')
    [(pool, program, argv)] = w.starts
    assert (pool, program) == (target, 'claude')
    current = w.svc.seat('builder')
    assert current.is_up and current.launched().pool == target
    assert current.launched().resume == ('synthetic-resume-target' if resumes else None)
    assert ('--resume' in argv) is resumes
    if resumes:
        assert current.record() == w.record.path
    assert w.reseeds == [(w.saved, w.saved)]


@pytest.mark.parametrize('manual', [False, True])
def test_only_manual_up_applies_the_reported_order_fresh_rule(choice_world, manual):
    w = choice_world('no-choice', fresh='after-order')
    order = w.svc.store.new('order', 'issuer', w.seat.name, 'Synthetic previous order')
    attempt = w.svc.attempts.open(order, w.seat.name, w.old.pane, w.old.pid, w.old.at)
    w.svc.attempts.close(attempt, 'delivered', 'Synthetic receipt')
    w.svc.store.new('report', w.seat.name, 'issuer', 'Completed', re=order.id)
    if manual:
        assert w.seat.up().endswith('fresh: its last order is reported')
    else:
        assert recovery.pass_once(w.svc) == 'recovered'
    assert w.seat.launched().resume == (None if manual else 'synthetic-resume-target')


@pytest.mark.parametrize('old_launch_remains', [True, False])
def test_interrupted_server_recovery_does_not_choose_again(choice_world, monkeypatch, old_launch_remains):
    w = choice_world('earlier')  # plain list, with an earlier choice that has room
    path = relaunch_intent.path(w.ship.runtime, w.seat.name)

    def interrupt(*args, **kwargs):
        assert json.loads(path.read_bytes())['needs_destination'] is True
        raise SystemExit('synthetic interruption')

    with monkeypatch.context() as cut:
        if old_launch_remains:
            cut.setattr(seatmod.Seat, 'launch_choice', interrupt)
        else:
            move = seatmod.Seat.move_to

            def interrupt_new_selection(seat, pool, **kwargs):
                if pool == 'first':
                    assert seat.launched() is None
                    interrupt()
                move(seat, pool, **kwargs)

            cut.setattr(seatmod.Seat, 'move_to', interrupt_new_selection)
        cut.setattr(relaunch_intent, '_remove', lambda *_: None)
        with shutdown.Stop(w.ship.runtime).installed(), pytest.raises(SystemExit, match='synthetic interruption'):
            recovery.pass_once(w.svc)

    data = json.loads(path.read_bytes())
    assert data['kind'] == 'server-recovery' and data['before'] == w.old.at
    assert w.seat.launched() == (w.old if old_launch_remains else None)
    monkeypatch.setattr(relaunch_intent, '_gone', lambda owner: owner == data['owner'])
    w.ship.seats[w.seat.name] = shipmod.replace(w.ship.seat(w.seat.name), pools=(w.saved,))
    assert recovery.pass_once(w.svc) is None  # this tick runs before relaunch intent recovery
    relaunch_intent.finish(w.svc)
    assert json.loads(path.read_bytes())['phase'] == relaunch_intent.FAILED
    assert relaunch_intent.status(w.ship) == [
        'builder: relaunch not finished from its intent (its destination pool was not recorded); needs a human: qd up builder']
    assert recovery.pass_once(w.svc) is None and not w.starts
    assert [a['outcome'] for a in recovery.read(w.ship.runtime)['attempts']] == ['started']
