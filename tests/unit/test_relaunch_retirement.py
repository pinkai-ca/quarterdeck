"""Interrupted retirement and relaunch recovery with a simulated program lifetime."""

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import credential, jail_end, relaunch_intent, seat as seatmod, shutdown, trusted_files, watch, write_bound
from quarterdeck.service import Service
from quarterdeck.ship import Pool, SeatSpec, Session, Ship, ShipError


@pytest.fixture
def world(tmp_path, monkeypatch):
    def make(*, choices=False, role='worker'):
        pools = ('primary', 'secondary') if choices else ('primary',)
        spec = SeatSpec('builder', 'crew', role, 'fake', pools,
                        pool_choices={pool: {} for pool in pools} if choices else {})
        ship = Ship(tmp_path/'ship', 'synthetic', 'operator', tmp_path,
                    {pool: Pool(pool, 'synthetic') for pool in pools},
                    {'crew': Session('crew', 'Synthetic')}, {spec.name: spec})
        w = SimpleNamespace(alive=True, ended=[], reseeded=[], starts=[])
        w.pane = SimpleNamespace(id='%1', pid=123, dead=False)
        def kill_window(pane, expected_pid):
            assert (pane, expected_pid) == (w.pane.id, w.pane.pid)
            assert not w.marker.exists(), 'the matching pane is closed before writing the marker'
            w.pane = None
        tmux = SimpleNamespace(pane=lambda _: w.pane, kill_window=kill_window)
        w.seat = seatmod.Seat(ship, spec, tmux)
        w.marker = w.seat.trusted / seatmod.RETIRING
        w.launch_path = w.seat.trusted / trusted_files.LAUNCH
        w.old = seatmod.Launched('%1', 123, '2000-01-01T10:00:00+00:00', 'primary', None, ['fake'],
                                 'synthetic', harness='fake', start='old-start')
        w.old.write(w.launch_path)
        w.binding = dict(seat=w.seat.name, pid=w.old.pid, start=w.old.start,
                         launch=dict(pane=w.old.pane, at=w.old.at))
        w.service = Service(ship, tmux)
        def process_alive(pid, start):
            assert (pid, start) == (w.old.pid, w.old.start)
            return w.alive
        def end(seat, tree):
            assert not w.alive
            w.ended.append((seat.name, tree))
            return {}
        def reseed(ship, seats, **kwargs):
            if seats:
                assert not w.alive and w.ended
                w.reseeded.extend(seat.launched() for seat in seats)
            return []
        monkeypatch.setattr(seatmod, 'process_alive', process_alive)
        monkeypatch.setattr(jail_end, 'snapshot', lambda launch: {})
        monkeypatch.setattr(jail_end, 'end', end)
        monkeypatch.setattr(credential, 'reseed', reseed)
        return w
    return make


class ServiceEnded(Exception):
    """Synthetic interruption in the program wait."""


def leave_mid_wait(w, monkeypatch, kind, resume):
    def wait(pid, start, seconds):
        assert w.pane is None
        assert (pid, start) == (w.old.pid, w.old.start)
        assert json.loads(w.marker.read_text()) == w.binding
        if seconds == seatmod.PROCESS_END_SECONDS:
            return False
        assert seconds == seatmod.RELAUNCH_END_SECONDS - seatmod.PROCESS_END_SECONDS
        raise ServiceEnded

    # Preserve the actual intent bytes as a process exit without unwinding would.
    with monkeypatch.context() as interrupted:
        interrupted.setattr(seatmod, 'wait_for_end', wait)
        interrupted.setattr(relaunch_intent, '_remove', lambda *_: None)
        with shutdown.Stop(w.service.runtime).installed():
            with pytest.raises(ServiceEnded):
                with relaunch_intent.section(w.service, w.seat, kind, resume=resume):
                    w.seat._relaunch_down()
    intent = relaunch_intent.path(w.service.runtime, w.seat.name)
    data = json.loads(intent.read_text())
    monkeypatch.setattr(relaunch_intent, '_gone', lambda owner: owner == data['owner'])
    return intent


def record_starts(w, monkeypatch, *, previous=None):
    def open_window(seat, argv, env, path):
        assert not w.alive and not w.marker.exists() and seat.launched() == previous
        w.starts.append((argv, seat.pool.name))
        w.pane = SimpleNamespace(id='%2', pid=124, dead=False)
        return w.pane

    monkeypatch.setattr(seatmod.Seat, '_open_window', open_window)
    monkeypatch.setattr(seatmod.Seat, '_survives', lambda *_: None)
    monkeypatch.setattr(seatmod, 'process_start', lambda _pid: 'new-start')


@pytest.mark.parametrize(('kind', 'choices', 'resume', 'outcome'), [
    ('compaction', True, False, 'fresh'),
    ('relaunch', False, True, 'resumed'),
    ('relaunch', False, True, 'missing-target'),
    ('relaunch', False, True, 'hand-fresh'),
    ('relaunch', False, True, 'screen-fresh'),
    ('after-order', False, False, 'fresh'),
    ('refusal', False, False, 'fresh'),
    ('select', True, False, 'missing-destination'),
    ('failover', True, True, 'missing-destination'),
    ('relaunch', True, True, 'missing-destination'),
    ('after-order', True, False, 'missing-destination'),
    ('refusal', True, False, 'missing-destination'),
])
def test_recovery_waits_for_tick_retirement_and_only_finishes_proved_intents(world, monkeypatch, kind, choices, resume, outcome):
    w = world(choices=choices, role='hand' if outcome == 'hand-fresh' else 'worker')
    if outcome == 'screen-fresh':
        monkeypatch.setattr(seatmod.Seat, '_screened', lambda _seat: True)
    target = 'synthetic-session' if outcome == 'resumed' else None
    if outcome in ('resumed', 'missing-target'):
        record = w.seat.home / 'projects' / 'synthetic' / 'record.jsonl'
        record.parent.mkdir(parents=True)
        row = {'timestamp': '2000-01-01T10:00:01+00:00'}
        if target:
            row['sessionId'] = target
        record.write_text(json.dumps(row) + '\n')
        assert w.seat.join().record == record

    successor = Service(w.seat.ship, w.seat.tmux)
    try:
        intent = leave_mid_wait(w, monkeypatch, kind, resume)
        data = json.loads(intent.read_text())
        assert data.get('needs_destination', False) == (outcome == 'missing-destination')
        if kind == 'after-order' and choices:
            del data['needs_destination']  # an older intent never recorded its ability to route
            intent.write_text(json.dumps(data))
        saved = intent.read_bytes()
        record_starts(w, monkeypatch)
        successor.relaunch_intents()
        successor.credentials()
        assert not w.starts and not w.ended and not w.reseeded
        assert intent.read_bytes() == saved and w.seat.launched() == w.old
        assert not w.seat.is_up
        assert 'builder: its program is still ending' in watch.facts(w.seat.ship, w.seat.tmux).line()

        w.alive = False
        successor.relaunch_intents()  # the process end alone is not yet a proof of retirement
        assert not w.starts and intent.read_bytes() == saved
        successor.credentials()
        assert not w.starts and intent.read_bytes() == saved
        assert w.ended == [(w.seat.name, {})] and w.reseeded == [w.old]
        assert not w.marker.exists() and w.seat.launched() is None

        if kind == 'relaunch' and choices:
            spec = successor.ship.seats[w.seat.name]
            successor.ship.seats[w.seat.name] = replace(spec, pools=('primary',), pool_choices={})
        successor.relaunch_intents()
        successor.relaunch_intents()
        reason = {'missing-destination': 'its destination pool was not recorded',
                  'missing-target': 'resume target unproved'}.get(outcome)
        if reason:
            assert not w.starts and not w.seat.is_up and w.seat.launched() is None
            failed = json.loads(intent.read_text())
            assert failed['phase'] == relaunch_intent.FAILED and failed['error'] == reason
            status = watch.facts(w.seat.ship, w.seat.tmux)
            assert status.relaunches == [
                f'builder: relaunch not finished from its intent ({reason}); needs a human: qd up builder']
            assert status.relaunches[0] in status.line()
        else:
            [(argv, pool)] = w.starts
            assert pool == 'primary' and not intent.exists() and w.seat.is_up
            assert w.seat.launched().resume == target
            if target:
                assert argv[argv.index('--resume') + 1] == target
                assert w.seat.lookup_record() == record
            else:
                assert '--resume' not in argv
    finally:
        successor.ledger.close()
        w.service.ledger.close()


def test_completed_relaunch_retires_its_marker_and_keeps_the_end_proof(world, monkeypatch):
    w = world()
    try:
        def ends(pid, start, seconds):
            assert w.pane is None and json.loads(w.marker.read_text()) == w.binding
            w.alive = False
            return True
        monkeypatch.setattr(seatmod, 'wait_for_end', ends)
        assert w.seat._relaunch_down().endswith('process ended')
        assert not w.marker.exists() and w.seat.launched() is None
        assert w.ended == [(w.seat.name, {})] and w.reseeded == [w.old]
    finally:
        w.service.ledger.close()


@pytest.mark.parametrize('first', ['retirement', 'launch'])
def test_recovery_after_an_interruption_between_removals(world, monkeypatch, first):
    w = world()
    try:
        intent = leave_mid_wait(w, monkeypatch, 'relaunch', False)
        saved = intent.read_bytes()
        if first == 'retirement':
            unlink = Path.unlink

            def interrupt(path, *args, **kwargs):
                if path == w.launch_path:
                    assert not w.marker.exists() and w.ended and w.reseeded == [w.old]
                    raise ServiceEnded
                return unlink(path, *args, **kwargs)

            w.alive = False
            with monkeypatch.context() as interrupted:
                interrupted.setattr(Path, 'unlink', interrupt)
                with pytest.raises(ServiceEnded):
                    w.service.credentials()
            assert w.seat.launched() == w.old and not w.marker.exists()
            record_starts(w, monkeypatch, previous=w.old)
        else:
            w.launch_path.unlink()  # the canonical launch name is already absent in this older state
            w.service.credentials()
            w.service.relaunch_intents()
            assert w.marker.exists() and not w.starts and not w.ended and not w.reseeded
            w.alive = False
            w.service.credentials()
            assert not w.marker.exists() and not w.ended and not w.reseeded
            record_starts(w, monkeypatch)

        assert intent.read_bytes() == saved
        w.service.relaunch_intents()
        w.service.relaunch_intents()
        assert len(w.starts) == 1 and w.seat.is_up and w.seat.launched().resume is None
        assert not intent.exists() and not w.marker.exists()
    finally:
        w.service.ledger.close()


@pytest.mark.parametrize('path', ['down', 'relaunch', 'tick', 'overlap'])
def test_native_retirement_ends_before_deferred_cleanup_finishes(world, monkeypatch, path):
    w = world()
    pids = {456: 'cleanup-start'}
    try:
        def end(seat, tree):
            assert not w.alive and w.marker.exists()
            w.ended.append((seat.name, tree))
            return pids

        unlink = Path.unlink
        cut = path == 'overlap'

        def check_handoff(file, *args, **kwargs):
            if file == w.marker and file.exists():
                assert jail_end.held(w.seat.trusted)['pids'] == pids
                if cut:
                    raise ServiceEnded
            return unlink(file, *args, **kwargs)

        def ends(pid, start, seconds):
            w.alive = False
            return True

        monkeypatch.setattr(seatmod, 'wait_for_end', ends)
        monkeypatch.setattr(jail_end, 'end', end)
        monkeypatch.setattr(Path, 'unlink', check_handoff)
        retries = []
        monkeypatch.setattr(write_bound, 'kill', retries.append)
        monkeypatch.setattr(write_bound, 'alive', lambda held: held)
        words = jail_end.words(w.seat.name, {'pids': pids})

        if path == 'tick':
            intent = leave_mid_wait(w, monkeypatch, 'relaunch', False)
            w.alive = False
            assert w.seat.finish_retirement() == words
            assert intent.exists() and relaunch_intent.hold(w.seat) == words
        elif path == 'overlap':
            with pytest.raises(ServiceEnded):
                w.seat._down()
            assert w.marker.exists() and jail_end.held(w.seat.trusted)['pids'] == pids
            cut = False
            w.service.credentials()
        elif path == 'relaunch':
            with shutdown.Stop(w.service.runtime).installed():
                with relaunch_intent.section(w.service, w.seat, 'relaunch', resume=False):
                    intent = relaunch_intent.path(w.service.runtime, w.seat.name)
                    assert intent.exists()
                    assert w.seat._relaunch_down().endswith(words.removeprefix('builder: ') + '; relaunch refused')
                    assert not intent.exists()
        else:
            assert w.seat._down().endswith(words.removeprefix('builder: '))

        assert not w.marker.exists() and w.seat.launched() == w.old
        assert w.seat.relaunch_refused() == words and not w.reseeded
        w.service.credentials()
        assert retries and all(held == pids for held in retries)
        assert w.seat.launched() == w.old and not w.reseeded

        monkeypatch.setattr(write_bound, 'alive', lambda held: {})
        w.service.credentials()
        assert jail_end.held(w.seat.trusted) is None and w.seat.launched() is None
        assert not w.marker.exists() and w.reseeded == [w.old]
        assert w.ended == [(w.seat.name, {})]  # only the independent hold retried
        if path == 'relaunch':
            assert w.seat.relaunch_refused().endswith('qd up builder')
    finally:
        w.service.ledger.close()


@pytest.mark.parametrize('case', ['malformed', 'wrong-seat', 'wrong-process', 'newer-launch',
                                  'invalid-pane', 'invalid-time', 'unreadable-launch', 'unproved-absence'])
def test_unproved_binding_retains_marker_and_current_launch(world, case):
    w = world()
    try:
        w.alive = False
        data = dict(w.binding)
        if case == 'wrong-seat':
            data['seat'] = 'other'
        if case == 'wrong-process':
            data['start'] = 'different-start'
        if case == 'newer-launch':
            replace(w.old, at='2000-01-01T10:01:00+00:00').write(w.launch_path)
        if case in ('invalid-pane', 'invalid-time', 'unproved-absence'):
            w.launch_path.unlink()
        if case == 'invalid-pane':
            data['launch'] = {**data['launch'], 'pane': ''}
        if case == 'invalid-time':
            data['launch'] = {**data['launch'], 'at': 'unknown'}
        if case == 'unreadable-launch':
            w.launch_path.write_text('{')
        if case == 'unproved-absence':
            w.launch_path.symlink_to(w.launch_path.with_name('missing'))
        w.marker.write_text('{' if case == 'malformed' else json.dumps(data))
        saved = w.launch_path.read_bytes() if w.launch_path.exists() else None
        link = w.launch_path.readlink() if w.launch_path.is_symlink() else None
        marker = w.marker.read_bytes()
        assert w.seat.retiring() == dict(pid=None, start=None)
        assert w.seat.finish_retirement() is None
        with pytest.raises(ShipError, match='prior retirement not proved'):
            w.seat._down()
        with pytest.raises(ShipError, match='still ending'):
            w.seat._up(resume=False)
        assert not w.ended and not w.reseeded
        assert w.marker.read_bytes() == marker
        assert (w.launch_path.read_bytes() if w.launch_path.exists() else None) == saved
        assert (w.launch_path.readlink() if w.launch_path.is_symlink() else None) == link
    finally:
        w.service.ledger.close()


def test_a_legacy_process_marker_is_still_retired_after_its_process_ends(world):
    w = world()
    try:
        w.pane = None
        legacy = dict(pid=w.old.pid, start=w.old.start)
        w.marker.write_text(json.dumps(legacy))
        assert w.seat.retiring() == legacy
        assert w.seat.finish_retirement() is None and w.seat.launched() == w.old
        w.alive = False
        assert w.seat.finish_retirement()
        assert not w.marker.exists() and w.seat.launched() is None and w.reseeded == [w.old]
    finally:
        w.service.ledger.close()
