"""The ship's own tmux server proved gone: exactly the formerly-up seats come back, at most twice an hour.

Real sockets prove gone, refused and answering; real processes prove a launch's owner dead or alive; the lifecycle
locks and the durable state are the framework's own. Only Seat.up and is_up are stood in, so nothing launches.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from multiprocessing import get_context
import os
import signal
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from quarterdeck import provenance
from quarterdeck import cli, credential, jail_end, lifecycle, post, relaunch_intent, resting_failover, shutdown, trusted_files, watch
from quarterdeck import seat as seatmod, server_recovery as recovery, ship as shipmod
from quarterdeck.harness.claude import ClaudeHarness
from quarterdeck.harness.codex import CodexHarness
from quarterdeck.harness.limits import Usage, Window
from quarterdeck.harness.synthetic_record import ClaudeShape, CodexShape
from quarterdeck.headlines import WIDTH, columns
from quarterdeck.service import Service
from quarterdeck.tmux import Pane
from .resting_attempts import UNSETTLED, save_attempt


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
    monkeypatch.setattr(seatmod.Seat, '_require_resume', lambda self, *a, **kw: None)
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
        tmux = SimpleNamespace(socket=str(tmp_path / 'absent-server'), pane=panes.get,
                               kill_window=lambda ident, **_: panes.pop(ident), capture=lambda *_args, **_kw: '')
        seat = seatmod.Seat(ship, spec, tmux)
        seat.move_to(saved)
        launched = seatmod.Launched('%1', dead_pid(), (post.now() - timedelta(minutes=1)).isoformat(), saved, None,
                                    [old_program], 'synthetic', harness=old_program, start='original-start',
                                    provider_route=provenance.provider_route(seat.pool))
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
            pane = Pane(f'%{len(starts) + 1}', 'crew', seat.name, 123 + len(starts), 80, 24, False, False, None)
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
                               record=record, saved=saved, panes=panes)

    yield make
    for svc in services:
        svc.ledger.close()


def stub_up_cli(w, monkeypatch):
    monkeypatch.delenv('QD_SEAT', raising=False)  # a person's word, whichever seat runs the tests
    w.svc.tmux.ship = None
    monkeypatch.setattr(cli, 'load_ship', lambda _: w.ship)
    monkeypatch.setattr(cli.Tmux, 'for_ship', lambda _: w.svc.tmux)
    monkeypatch.setattr(cli.install, 'prepare_up', lambda _: ([], False))
    monkeypatch.setattr(cli.install, 'delegate_up', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli.service, 'start', lambda _: 'synthetic service already running')


def write_resting_intent(w):
    runtime = w.ship.runtime
    relaunch_intent._write(runtime, dict(
        version=1, seat=w.seat.name, kind=relaunch_intent.RESTING, resume=True, at=post.now().isoformat(),
        phase=relaunch_intent.FAILED, owner={'pid': 1, 'start': 'synthetic-relaunch-owner'}, before=w.old.at,
        destination={'pool': w.saved, 'harness': 'claude'}))
    return relaunch_intent.path(runtime, w.seat.name)


@pytest.mark.parametrize('phase', ['intent', *UNSETTLED])
def test_whole_ship_up_leaves_resting_holds_for_named_up(choice_world, monkeypatch, phase):
    w = choice_world('no-choice')
    runtime = w.ship.runtime
    name = w.seat.name
    w.ship.seats['neighbor'] = shipmod.replace(w.seat.spec, name='neighbor')
    neighbor = w.svc.seat('neighbor')
    if phase == 'intent':
        held_file = write_resting_intent(w)
    else:
        choice = resting_failover.Candidate(w.saved, ('synthetic-account', 'synthetic-generation'))
        attempt = resting_failover.claim(w.seat, choice, w.old.at)
        if phase != 'reserved':
            assert resting_failover.outcome(runtime, attempt, phase, w.old.at)
        assert resting_failover.hold_state(w.seat) is resting_failover.Hold.HELD
        held_file = runtime / resting_failover.FILE
    saved = held_file.read_bytes()
    needs_desk = phase in resting_failover.DESK_RELEASES
    proof = f'{name} left down: resting move held{"; needs the desk" if needs_desk else ""} (qd up {name})'
    hold_reader = resting_failover.hold_state
    claimed_reads = []

    def read_while_claimed(*args):
        held = hold_reader(*args)
        if held is resting_failover.Hold.HELD:
            with pytest.raises(lifecycle.Busy), lifecycle.claim(runtime, name):
                pytest.fail('the hold was checked outside the lifecycle claim')
            claimed_reads.append(held)
        return held

    with monkeypatch.context() as check:
        check.setattr(resting_failover, 'hold_state', read_while_claimed)
        assert proof in seatmod.bring_up(w.ship, w.svc.tmux)
    assert claimed_reads == [resting_failover.Hold.HELD]
    assert neighbor.is_up and not w.seat.is_up and len(w.starts) == 1
    assert w.seat.launched() == w.old and held_file.read_bytes() == saved
    line = watch.facts(w.ship, w.svc.tmux, walls=[], login=[]).line()
    assert f'qd up {name}' in line and columns(line) <= WIDTH

    stub_up_cli(w, monkeypatch)
    bare = SimpleNamespace(seat=None, fresh=False)
    assert proof in cli.cmd_up(bare)
    assert w.seat.launched() == w.old and held_file.read_bytes() == saved and len(w.starts) == 1
    assert any(f'{name} up in' in row for row in cli.cmd_up(SimpleNamespace(seat=name, fresh=False)))
    assert w.seat.is_up and neighbor.is_up and len(w.starts) == 2
    launch = w.seat.launched()
    assert launch.pool == w.saved and launch.at != w.old.at
    if phase == 'intent':
        assert not held_file.exists()
    else:
        settled = resting_failover.attempt_for(w.seat)
        assert (settled.phase, settled.destination) == ('released' if needs_desk else 'launched', launch.at)
        assert resting_failover.spent(runtime, choice)
    unproved = phase in ('reserved', 'launched', 'releasing')
    assert resting_failover.hold_state(w.seat) is (
        resting_failover.Hold.HELD if unproved else resting_failover.Hold.CLEAR)
    assert any(f'{name} already up in' in row for row in cli.cmd_up(bare))
    assert w.seat.launched() == launch and len(w.starts) == 2


@pytest.mark.parametrize(('initial', 'matching'), [
    ('reserved', 'new-launch'), ('reserved', 'neither-launch'), ('ready', 'old-launch'), ('released', 'old-launch'),
])
def test_named_up_settles_only_an_unsettled_attempt_matching_either_launch(choice_world, monkeypatch, initial, matching):
    w = choice_world('earlier')
    runtime = w.ship.runtime
    pool = 'first' if matching == 'neither-launch' else w.saved
    choice = resting_failover.Candidate(pool, ('synthetic-other-account', 'synthetic-other-generation'))
    old_source = w.old.at if matching == 'old-launch' else 'synthetic earlier launch'
    attempt = resting_failover.claim(w.seat, choice, old_source)
    if initial != 'reserved':
        assert resting_failover.outcome(runtime, attempt, initial, w.old.at)
    if matching == 'new-launch':
        replace_launch = shipmod.replace(w.old, pool='first')
        replace_launch.write(w.seat.trusted / trusted_files.LAUNCH)
        assert not attempt.matches(w.seat.launched())
    original = resting_failover.attempt_for(w.seat)
    assert resting_failover.hold_state(w.seat) is resting_failover.Hold.CLEAR
    stub_up_cli(w, monkeypatch)
    cli.cmd_up(SimpleNamespace(seat=w.seat.name, fresh=False, pool=w.saved))
    launch = w.seat.launched()
    assert launch.pool == w.saved and w.seat.is_up and len(w.starts) == 1
    if matching == 'new-launch':
        assert original.matches(launch)
        assert resting_failover.attempt_for(w.seat) == shipmod.replace(original, phase='launched', destination=launch.at)
        assert resting_failover.hold_state(w.seat) is resting_failover.Hold.HELD
    elif initial == 'reserved':
        assert resting_failover.attempt_for(w.seat) == shipmod.replace(original, phase='released', destination=launch.at)
        assert resting_failover.hold_state(w.seat) is resting_failover.Hold.CLEAR
    else:
        assert resting_failover.attempt_for(w.seat) == original
        assert resting_failover.hold_state(w.seat) is resting_failover.Hold.CLEAR
    assert resting_failover.spent(runtime, choice)


def test_direct_seat_up_and_refused_named_up_leave_the_resting_hold(choice_world, monkeypatch):
    w = choice_world('no-choice')
    path = write_resting_intent(w)
    saved = path.read_bytes()
    choice = resting_failover.Candidate(w.saved, ('synthetic-account', 'synthetic-generation'))
    attempt = resting_failover.claim(w.seat, choice, w.old.at)
    stub_up_cli(w, monkeypatch)
    with monkeypatch.context() as refused:
        refused.setattr(seatmod.Seat, '_open_window', Mock(side_effect=shipmod.ShipError('synthetic launch refused')))
        with pytest.raises(shipmod.ShipError, match='synthetic launch refused'):
            cli.cmd_up(SimpleNamespace(seat=w.seat.name, fresh=False))
        assert not w.seat.is_up and not w.starts
        assert path.read_bytes() == saved and resting_failover.attempt_for(w.seat) == attempt
    with pytest.raises(resting_failover.Held, match=f'qd up {w.seat.name}'):
        w.seat.up()
    assert not w.seat.is_up and not w.starts
    assert path.read_bytes() == saved and resting_failover.attempt_for(w.seat) == attempt


@pytest.mark.parametrize('boundary', ['before-scan', 'after-scan'])
def test_corrupt_resting_attempt_db_defers_only_the_intent_seat_until_qd_up(choice_world, monkeypatch, boundary):
    w = choice_world('no-choice')
    w.ship.seats['neighbor'] = shipmod.replace(w.seat.spec, name='neighbor')
    neighbor = w.svc.seat('neighbor')
    neighbor.move_to(w.saved)
    shipmod.replace(w.old, pane='%9').write(neighbor.trusted / trusted_files.LAUNCH)
    record = ClaudeShape(neighbor.home, w.ship.workspace, 'synthetic-neighbor-resume', 'synthetic-model')
    record.begin('Synthetic neighbor input', 'synthetic-model', 'high')
    record.end('Synthetic neighbor output')
    (neighbor.trusted / trusted_files.RECORD).write_text(str(record.path) + '\n')
    runtime = w.ship.runtime
    path = relaunch_intent.path(runtime, w.seat.name)
    data = dict(version=1, seat=w.seat.name, kind=relaunch_intent.RESTING, resume=True,
                at=(post.now() - timedelta(minutes=2)).isoformat(), phase=relaunch_intent.RELAUNCHING,
                owner={'pid': 1, 'start': 'synthetic-relaunch-owner'}, before=None,
                destination={'pool': w.saved, 'harness': 'claude'})
    (runtime / resting_failover.FILE).write_bytes(b'synthetic broken SQLite database')

    def write_intent():
        relaunch_intent._write(runtime, data)

    if boundary == 'before-scan':
        write_intent()
    else:
        monkeypatch.setattr(shipmod, 'fence', lambda _: write_intent())
    monkeypatch.setattr(relaunch_intent, '_gone', lambda *_: True)
    expected = resting_failover.Hold.HELD if boundary == 'before-scan' else resting_failover.Hold.CLEAR
    assert resting_failover.hold_state(w.seat) is expected
    assert resting_failover.hold_state(neighbor) is resting_failover.Hold.CLEAR
    assert recovery.pass_once(w.svc) == 'recovered'
    assert neighbor.is_up and not w.seat.is_up
    assert w.seat.launched() == w.old and len(w.starts) == 1
    assert relaunch_intent.clear_all(w.ship, 'synthetic server stop', keep_server_choices=True) == []
    assert json.loads(path.read_text()) == data
    relaunch_intent.finish(w.svc)
    assert json.loads(path.read_text())['phase'] == relaunch_intent.FAILED
    assert recovery.pass_once(w.svc) is None and len(w.starts) == 1
    line = watch.facts(w.ship, w.svc.tmux, walls=[], login=[]).line()
    assert 'qd up builder' in line and resting_failover.FILE in line

    stub_up_cli(w, monkeypatch)
    cli.cmd_up(SimpleNamespace(seat=w.seat.name, fresh=False, pool=None))
    assert w.seat.is_up and neighbor.is_up and not path.exists()
    assert w.seat.launched().resume == 'synthetic-resume-target'
    assert len(w.starts) == 2
    relaunch_intent.finish(w.svc)
    assert recovery.pass_once(w.svc) is None and len(w.starts) == 2


@pytest.mark.parametrize('method', ['manual', 'server'])
@pytest.mark.parametrize(('case', 'target', 'resumes'), [
    ('next', 'second', False), ('earlier', 'first', True), ('no-choice', 'second', True),
])
def test_server_recovery_and_qd_up_use_the_same_pool_choice(choice_world, case, target, resumes, method):
    w = choice_world(case)
    if method == 'server':
        assert recovery.pass_once(w.svc) == 'recovered'
        owner_reports = [m.body for m in w.svc.store.for_seat('builder') if m.kind == 'report']
        assert any('started a new conversation' in body for body in owner_reports) is (not resumes)
    else:
        proof = w.seat.up()
        assert ('started a new conversation' in proof) is (not resumes)
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


@pytest.mark.parametrize('human', [False, True], ids=['crew', 'human'])
def test_server_recovery_of_unproved_resume_respects_session_ownership(choice_world, human):
    w = choice_world('no-choice')
    w.ship.sessions['crew'] = shipmod.replace(w.ship.sessions['crew'], human=human)
    w.record.path.unlink()
    outcome = recovery.pass_once(w.svc)
    seat = w.svc.seat('builder')
    if human:
        assert outcome == 'failed' and not w.starts and seat.launched() == w.old
        assert 'Use qd fresh builder' in recovery.read(w.ship.runtime)['stopped']
    else:
        assert outcome == 'recovered' and len(w.starts) == 1 and seat.launched().resume is None
        assert any('started a new conversation' in m.body for m in w.svc.store.for_seat('builder'))


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


@pytest.mark.parametrize(('case', 'boundary'), [
    ('earlier', 'before-choice'), ('earlier', 'before-down'),
    ('earlier', 'before-move'), ('next', 'before-move'),
])
def test_interrupted_server_recovery_does_not_choose_again(choice_world, monkeypatch, case, boundary):
    w = choice_world(case)
    target = 'first' if case == 'earlier' else 'second'
    path = relaunch_intent.path(w.ship.runtime, w.seat.name)

    def interrupt(*args, **kwargs):
        assert json.loads(path.read_bytes())['needs_destination'] is True
        raise SystemExit('synthetic interruption')

    with monkeypatch.context() as cut:
        if boundary == 'before-choice':
            cut.setattr(seatmod.Seat, 'launch_choice', interrupt)
        elif boundary == 'before-down':
            cut.setattr(seatmod.Seat, '_relaunch_down', interrupt)
        else:
            move = seatmod.Seat.move_to

            def interrupt_new_selection(seat, pool, **kwargs):
                if pool == target:
                    assert seat.launched() is None
                    interrupt()
                move(seat, pool, **kwargs)

            cut.setattr(seatmod.Seat, 'move_to', interrupt_new_selection)
        cut.setattr(relaunch_intent, '_remove', lambda *_: None)
        with shutdown.Stop(w.ship.runtime).installed(), pytest.raises(SystemExit, match='synthetic interruption'):
            recovery.pass_once(w.svc)

    data = json.loads(path.read_bytes())
    assert data['kind'] == 'server-recovery' and data['before'] == w.old.at
    assert w.seat.launched() == (None if boundary == 'before-move' else w.old)
    monkeypatch.setattr(relaunch_intent, '_gone', lambda owner: owner == data['owner'])
    monkeypatch.setattr(seatmod.Seat, 'launch_choice', lambda _: pytest.fail('interrupted recovery re-routed'))
    assert recovery.pass_once(w.svc) is None  # this tick runs before relaunch intent recovery
    relaunch_intent.finish(w.svc)
    if boundary == 'before-choice':
        assert 'destination' not in data
        assert json.loads(path.read_bytes())['phase'] == relaunch_intent.FAILED
        assert relaunch_intent.status(w.ship) == [
            'builder: relaunch not finished from its intent (its destination pool was not recorded); needs a human: qd up builder']
        assert not w.starts
    else:
        assert data['destination'] == {'pool': target, 'harness': 'claude'}
        assert data['resume'] is (case == 'earlier')
        [(pool, program, argv)] = w.starts
        assert (pool, program) == (target, 'claude')
        assert ('--resume' in argv) is (case == 'earlier')
        assert not path.exists()
    assert recovery.pass_once(w.svc) is None
    assert [a['outcome'] for a in recovery.read(w.ship.runtime)['attempts']] == ['started']


@pytest.mark.parametrize('phase', UNSETTLED)
def test_server_recovery_waits_for_an_intentless_resting_move(choice_world, phase):
    w = choice_world('no-choice')
    _, attempt = save_attempt(w.seat, phase)
    assert recovery.pass_once(w.svc) is None
    assert not w.seat.is_up and not w.starts and w.seat.launched() == w.old
    assert resting_failover.attempt_for(w.seat) == attempt
    assert recovery.read(w.ship.runtime)['attempts'] == []


def test_server_recovery_reads_busy_attempts_once_and_recovers_an_unaffected_seat(choice_world, monkeypatch, tmp_path):
    w = choice_world('no-choice')
    runtime = w.ship.runtime
    ship = w.ship
    second = ship.pools[w.saved]
    ship.pools[w.saved] = shipmod.replace(second, config_dir=tmp_path / 'synthetic-subscription')
    ship.pools['unaffected'] = shipmod.replace(second, name='unaffected')
    spec = shipmod.replace(w.seat.spec, name='neighbor', pools=('unaffected',))
    ship.seats[spec.name] = spec
    neighbor = w.svc.seat(spec.name)
    shipmod.replace(w.old, pane='%9', pool='unaffected').write(neighbor.trusted / trusted_files.LAUNCH)
    record = ClaudeShape(neighbor.home, ship.workspace, 'synthetic-unaffected', 'synthetic-model')
    record.begin('Synthetic neighbor input', 'synthetic-model', 'high')
    record.end('Synthetic neighbor output')
    (neighbor.trusted / trusted_files.RECORD).write_text(str(record.path) + '\n')
    save_attempt(w.seat, 'reserved')
    reads = Mock(wraps=resting_failover.attempts)
    monkeypatch.setattr(resting_failover, 'attempts', reads)
    db = sqlite3.connect(runtime / resting_failover.FILE)
    try:
        db.execute('BEGIN EXCLUSIVE')
        with resting_failover.reading_pass(), shutdown.Stop(runtime).installed():
            assert recovery.pass_once(w.svc) == 'recovered'
            assert resting_failover.hold_state(w.svc.seat(w.seat.name)) is resting_failover.Hold.UNKNOWN
            assert resting_failover.hold_state(neighbor) is resting_failover.Hold.CLEAR
            assert resting_failover.status(ship, {}) == [f'{resting_failover.FILE} busy; waiting']
        reads.assert_called_once_with(runtime)
    finally:
        db.close()
    assert len(w.starts) == 1 and neighbor.is_up and not w.seat.is_up
    assert w.seat.launched() == w.old and recovery.read(runtime)['attempts'][0]['seats'] == ['neighbor']


@pytest.mark.parametrize(('begin', 'phase'), [
    ('BEGIN EXCLUSIVE', 'reserved'), ('BEGIN IMMEDIATE', 'reserved'), ('BEGIN IMMEDIATE', 'releasing'),
])
def test_named_up_waits_for_sqlite_before_launch_then_settles_on_retry(choice_world, monkeypatch, begin, phase):
    w = choice_world('no-choice')
    runtime = w.ship.runtime
    choice, attempt = save_attempt(w.seat, phase)
    intent = write_resting_intent(w)
    saved = intent.read_bytes()
    stub_up_cli(w, monkeypatch)
    args = SimpleNamespace(seat=w.seat.name, fresh=False)
    db = sqlite3.connect(runtime / resting_failover.FILE)
    try:
        db.execute(begin)
        if begin == 'BEGIN IMMEDIATE':
            assert resting_failover.attempt_for(w.seat) == attempt  # SELECT succeeds; the release could not
        reads = Mock(wraps=resting_failover.attempts)
        monkeypatch.setattr(resting_failover, 'attempts', reads)
        with pytest.raises(resting_failover.Held, match=f'busy.*qd up {w.seat.name}'):
            cli.cmd_up(args)
        reads.assert_called_once_with(runtime)
        assert not w.starts and not w.seat.is_up and w.seat.launched() == w.old and intent.read_bytes() == saved
    finally:
        db.close()
    assert any(f'{w.seat.name} up in' in proof for proof in cli.cmd_up(args))
    assert w.seat.is_up and len(w.starts) == 1 and not intent.exists()
    settled = resting_failover.attempt_for(w.seat)
    assert (settled.phase, settled.destination) == ('launched', w.seat.launched().at)
    assert resting_failover.spent(runtime, choice)


def test_whole_ship_up_reads_busy_attempts_once_then_launches(choice_world, monkeypatch, tmp_path):
    w = choice_world('no-choice')
    runtime = w.ship.runtime
    pool = w.ship.pool(w.saved)
    w.ship.pools[w.saved] = shipmod.replace(pool, config_dir=tmp_path / 'synthetic-subscription')
    w.ship.pools['unaffected'] = shipmod.replace(pool, name='unaffected')
    spec = shipmod.replace(w.seat.spec, name='neighbor', pools=('unaffected',))
    w.ship.seats[spec.name] = spec
    neighbor = w.svc.seat(spec.name)
    _, attempt = save_attempt(w.seat, 'ready')
    reads = Mock(wraps=resting_failover.attempts)
    monkeypatch.setattr(resting_failover, 'attempts', reads)
    db = sqlite3.connect(runtime / resting_failover.FILE)
    try:
        db.execute('BEGIN EXCLUSIVE')
        proofs = seatmod.bring_up(w.ship, w.svc.tmux)
        assert any(f'{w.seat.name} left down:' in proof and 'busy' in proof and f'qd up {w.seat.name}' in proof
                   for proof in proofs)
        reads.assert_called_once_with(runtime)
        assert not w.seat.is_up and neighbor.is_up and len(w.starts) == 1 and w.seat.launched() == w.old
    finally:
        db.close()

    reads.reset_mock()
    proofs = seatmod.bring_up(w.ship, w.svc.tmux)
    assert any(f'{w.seat.name} up in' in proof for proof in proofs)
    reads.assert_called_once_with(runtime)
    assert w.seat.is_up and neighbor.is_up and len(w.starts) == 2
    assert resting_failover.attempt_for(w.seat) == attempt



def test_busy_attempts_hold_a_down_seat_set_to_another_declared_program(choice_world, tmp_path):
    w = choice_world('next')
    ship, seat = w.ship, w.seat
    second = ship.pool('second')
    ship.pools['second'] = shipmod.replace(second, config_dir=tmp_path / 'synthetic-subscription')
    choice = resting_failover.Candidate('second', ('synthetic subscription', 'previous expiry'))
    attempt = resting_failover.claim(seat, choice, 'earlier Claude launch')
    (seat.trusted / trusted_files.LAUNCH).unlink()
    assert seat.pool.name == 'first' and seat.spec.harness == 'codex' and not seat.is_up
    assert not resting_failover.eligible_source(seat)
    db = sqlite3.connect(ship.runtime / resting_failover.FILE)
    try:
        db.execute('BEGIN EXCLUSIVE')
        with resting_failover.reading_pass():
            assert resting_failover.hold_state(seat) is resting_failover.Hold.UNKNOWN
            proofs = seatmod.bring_up(ship, w.svc.tmux)
            assert any(f'{seat.name} left down:' in proof and 'busy' in proof for proof in proofs)
            assert not w.starts and seat.launched() is None
            removed = ship.pools.pop('second')
            try:
                assert resting_failover.hold_state(seat) is resting_failover.Hold.CLEAR
            finally:
                ship.pools['second'] = removed
    finally:
        db.close()
    assert resting_failover.hold_state(seat) is resting_failover.Hold.HELD
    assert resting_failover.attempt_for(seat) == attempt


def test_named_up_leaves_sqlite_readable_and_writable_during_launch(choice_world, monkeypatch):
    w = choice_world('no-choice')
    runtime = w.ship.runtime
    name = w.seat.name
    _, attempt = save_attempt(w.seat, 'failed')
    neighbor = shipmod.replace(w.seat.spec, name='neighbor')
    w.ship.seats[neighbor.name] = neighbor
    member = w.svc.seat(neighbor.name)
    other = resting_failover.claim(member, resting_failover.Candidate(w.saved, ('neighbor account', 'generation')),
                                   'neighbor source')
    stub_up_cli(w, monkeypatch)
    original = seatmod.Seat._open_window

    def open_window(seat, *args):
        rows = resting_failover.attempts(runtime)
        assert rows[name] == shipmod.replace(attempt, phase='releasing', origin='failed')
        assert rows[neighbor.name] == other
        assert resting_failover.outcome(runtime, other, 'launched', 'neighbor destination')
        return original(seat, *args)

    monkeypatch.setattr(seatmod.Seat, '_open_window', open_window)
    cli.cmd_up(SimpleNamespace(seat=name, fresh=False))
    rows = resting_failover.attempts(runtime)
    assert (rows[name].phase, rows[name].destination) == ('released', w.seat.launched().at)
    assert rows[neighbor.name] == shipmod.replace(other, phase='launched', destination='neighbor destination')
    assert len(w.starts) == 1


def test_busy_finish_leaves_live_release_for_retry(choice_world, monkeypatch):
    w = choice_world('no-choice')
    runtime = w.ship.runtime
    name = w.seat.name
    _, attempt = save_attempt(w.seat, 'failed')
    stub_up_cli(w, monkeypatch)
    args = SimpleNamespace(seat=name, fresh=False)
    original = seatmod.Seat._open_window
    db = sqlite3.connect(runtime / resting_failover.FILE)

    def open_window(seat, *args):
        db.execute('BEGIN IMMEDIATE')
        return original(seat, *args)

    try:
        with monkeypatch.context() as launch:
            launch.setattr(seatmod.Seat, '_open_window', open_window)
            with pytest.raises(resting_failover.Held, match=f'busy.*qd up {name}'):
                cli.cmd_up(args)
        assert w.seat.is_up and len(w.starts) == 1
        assert resting_failover.attempt_for(w.seat) == shipmod.replace(attempt, phase='releasing', origin='failed')
    finally:
        db.close()
    launch = w.seat.launched()
    assert cli.cmd_up(args)[0] == f'{name} already up in {launch.pane}'
    assert w.seat.launched() == launch and len(w.starts) == 1
    assert resting_failover.attempt_for(w.seat).phase == 'released'


def test_named_up_finishes_a_killed_release_without_launching_twice(choice_world, monkeypatch):
    w = choice_world('no-choice')
    runtime = w.ship.runtime
    name = w.seat.name
    choice, attempt = save_attempt(w.seat, 'failed')
    intent = write_resting_intent(w)
    stub_up_cli(w, monkeypatch)
    args = SimpleNamespace(seat=name, fresh=False)
    outcome = resting_failover.outcome

    def pause_before_finish(runtime, attempt, phase, destination):
        if phase == 'released':
            launch = w.seat.launched()
            assert w.seat.is_up and launch.at == destination and len(w.starts) == 1
            send.send(w.panes[launch.pane])
            signal.pause()
        return outcome(runtime, attempt, phase, destination)

    def named_up():
        recv.close()
        resting_failover.outcome = pause_before_finish
        cli.cmd_up(args)

    fork = get_context('fork')
    recv, send = fork.Pipe(duplex=False)
    child = fork.Process(target=named_up)
    child.start()
    send.close()
    try:
        assert recv.poll(10), 'child did not reach the witnessed second write'
        pane = recv.recv()
        w.panes[pane.id] = pane  # the synthetic tmux view sees the pane that outlives the launching process
        launch = w.seat.launched()
        assert resting_failover.attempt_for(w.seat) == shipmod.replace(attempt, phase='releasing', origin='failed')
        assert w.seat.is_up and resting_failover.hold_state(w.seat) is resting_failover.Hold.HELD
    finally:
        child.kill()
        child.join()
        recv.close()
    assert child.exitcode == -signal.SIGKILL
    assert intent.exists() and resting_failover.hold_state(w.seat) is resting_failover.Hold.HELD
    assert cli.cmd_up(SimpleNamespace(seat=name, pool=w.saved, fresh=True))[0] == f'{name} already up in {pane.id}'
    assert not w.starts and w.seat.launched() == launch and not intent.exists()
    assert resting_failover.attempt_for(w.seat) == shipmod.replace(attempt, phase='released', destination=launch.at)
    assert resting_failover.spent(runtime, choice)


@pytest.mark.parametrize(('result', 'expected_phase'), [
    ('exception', 'failed'), ('returned down', 'failed'), ('retirement unknown', 'releasing'),
])
def test_named_up_restores_the_prior_attempt_only_when_failure_is_confirmed(
        choice_world, monkeypatch, result, expected_phase):
    w = choice_world('no-choice')
    _, attempt = save_attempt(w.seat, 'failed')
    stub_up_cli(w, monkeypatch)

    def launch(seat, *_args, **_kwargs):
        assert not seat.is_up and resting_failover.attempt_for(seat).phase == 'releasing'
        if result == 'exception':
            raise shipmod.ShipError('synthetic launch failure')
        return 'synthetic launch returned'

    monkeypatch.setattr(seatmod.Seat, '_up_with_pool_choice', launch)
    if result == 'retirement unknown':
        monkeypatch.setattr(seatmod.Seat, 'relaunch_refused', lambda self: 'synthetic retirement unconfirmed')
    with pytest.raises(shipmod.ShipError, match='synthetic launch failure|left the seat down|could not be confirmed'):
        cli.cmd_up(SimpleNamespace(seat=w.seat.name, fresh=False))
    assert not w.seat.is_up and not w.starts
    origin = 'failed' if expected_phase == 'releasing' else None
    assert resting_failover.attempt_for(w.seat) == shipmod.replace(attempt, phase=expected_phase, origin=origin)


def test_named_up_refuses_when_first_write_finds_the_attempt_changed(choice_world, monkeypatch):
    w = choice_world('no-choice')
    _, attempt = save_attempt(w.seat, 'reserved')
    stub_up_cli(w, monkeypatch)
    original = resting_failover.outcome

    def concurrent_outcome(runtime, expected, phase, destination):
        assert expected == attempt and phase == 'releasing'
        assert original(runtime, attempt, 'ready', w.old.at)
        return original(runtime, expected, phase, destination)

    monkeypatch.setattr(resting_failover, 'outcome', concurrent_outcome)
    with pytest.raises(resting_failover.Held, match=f'attempt changed; retry qd up {w.seat.name}'):
        cli.cmd_up(SimpleNamespace(seat=w.seat.name, fresh=False))
    assert not w.starts and w.seat.launched() == w.old and not w.seat.is_up
    assert resting_failover.attempt_for(w.seat) == shipmod.replace(attempt, phase='ready', destination=w.old.at)


def test_live_provisional_up_is_read_only_until_down_then_up(choice_world, monkeypatch):
    w = choice_world('earlier')
    name = w.seat.name
    pane = Pane(w.old.pane, 'crew', name, w.old.pid, 80, 24, False, False, None)
    w.panes[pane.id] = pane
    _, attempt = save_attempt(w.seat, 'launched')
    intent = write_resting_intent(w)
    saved = intent.read_bytes()
    stub_up_cli(w, monkeypatch)
    facts = watch.facts(w.ship, w.svc.tmux, walls=[], login=[])
    expected = f'{name}: on resting subscription {attempt.pool}, not yet proved by a first turn'
    assert not facts.relaunches and f'qd up {name}' not in facts.line()
    detailed = watch.status_reading(w.ship, w.svc.tmux, collected=facts)
    assert detailed['resting_provisional'] == [expected] and expected in watch.render_table(detailed)
    assert resting_failover.hold_state(w.seat) is resting_failover.Hold.HELD

    from quarterdeck import compaction, write_bound
    writes = Mock(side_effect=AssertionError('a live provisional up must be read-only'))
    with monkeypatch.context() as read_only:
        read_only.setattr(resting_failover, 'outcome', writes)
        read_only.setattr(compaction, 'disarm', writes)
        read_only.setattr(write_bound, 'lift', writes)
        read_only.setattr(cli.service, 'start', writes)
        for fresh, pool in [(False, None), (True, None), (False, 'first'), (True, 'first')]:
            assert cli.cmd_up(SimpleNamespace(seat=name, fresh=fresh, pool=pool)) == [f'{name} already up in {pane.id}']
    writes.assert_not_called()
    assert not w.starts and w.seat.launched() == w.old and intent.read_bytes() == saved
    assert resting_failover.attempt_for(w.seat) == attempt

    cli.cmd_down(SimpleNamespace(seat=name))
    assert not w.seat.is_up and not intent.exists()
    line = watch.facts(w.ship, w.svc.tmux, walls=[], login=[]).line()
    assert 'program ended before sign-in was proved' in line and f'qd up {name}' in line
    cli.cmd_up(SimpleNamespace(seat=name, fresh=True, pool=attempt.pool))
    assert w.seat.is_up and len(w.starts) == 1
    provisional = resting_failover.attempt_for(w.seat)
    assert (provisional.phase, provisional.destination) == ('launched', w.seat.launched().at)
    assert resting_failover.hold_state(w.seat) is resting_failover.Hold.HELD


@pytest.mark.parametrize('phase', UNSETTLED)
@pytest.mark.parametrize('recorded', [False, True])
def test_finishing_an_older_intent_waits_for_an_intentless_resting_move(choice_world, monkeypatch, phase, recorded):
    w = choice_world('no-choice')
    runtime = w.ship.runtime
    _, attempt = save_attempt(w.seat, phase)
    data = dict(version=1, seat=w.seat.name, kind='relaunch', resume=True, at=post.now().isoformat(),
                phase=relaunch_intent.RELEASED, owner={'pid': 1, 'start': 'synthetic-relaunch-owner'}, before=w.old.at)
    if recorded:
        data['destination'] = {'pool': w.saved, 'harness': w.seat.spec.harness}
    relaunch_intent._write(runtime, data)
    monkeypatch.setattr(relaunch_intent, '_gone', lambda _: True)
    relaunch_intent.finish(w.svc)
    path = relaunch_intent.path(runtime, w.seat.name)
    assert json.loads(path.read_text()) == data
    assert not w.seat.is_up and not w.starts and w.seat.launched() == w.old
    assert resting_failover.outcome(runtime, attempt, 'released', attempt.destination)
    relaunch_intent.finish(w.svc)
    assert w.seat.is_up and len(w.starts) == 1 and not path.exists()
