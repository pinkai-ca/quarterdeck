"""qd service --settle-delivery: the real act, on a throwaway ship. No tmux server is started.

A stand-in reports the server pid. A child that must exist is a real process of this user. The ship's own
service, tmux and runtime are never touched.
"""

from __future__ import annotations

import ctypes
import json
import os
import resource
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import delivery_boundary, human_wake, pending, post, servicectl, shutdown
from quarterdeck.service import Service
from tests.unit.test_barrier_settle import (
    HOLDER, SLEEPER, UNDUMPABLE, comm, ended, log_lines, scope_of, ship_at, started, tmux)
from tests.unit.test_startup_delivery_diagnosis import evidence, old_owner

SENTINEL = 'synthetic-boundary-words-not-for-the-log'
STAMP = '2026-01-01T00:00:00+00:00'


@pytest.fixture(autouse=True)
def dumpable_again():
    """The act leaves its process non-dumpable for the rest of its life. Here that process is the test's."""
    core = resource.getrlimit(resource.RLIMIT_CORE)
    yield
    resource.setrlimit(resource.RLIMIT_CORE, core)
    ctypes.CDLL(None).prctl(delivery_boundary.barrier_settle.PR_SET_DUMPABLE, 1, 0, 0, 0)


def plant_pointer(ship, journal, *, kind='pending', token='boundary-token', owner=None, before=False):
    body = dict(version=1, token=token, kind=kind, before_paste=before, journal=journal)
    if owner is not None:
        body['owner'] = owner
    path = shutdown.pointer_path(ship.runtime)
    path.write_text(json.dumps(body) + '\n')
    return path


def untouched(ship, *paths):
    before = {path: path.read_bytes() if path.exists() else None for path in paths}
    beat = servicectl.heartbeat_path(ship.runtime)
    beat.parent.mkdir(parents=True, exist_ok=True)
    beat.write_text('{"pid": 1, "beat": "synthetic"}\n')
    lock = servicectl.lock_path(ship.runtime).read_bytes()
    return before, beat, lock


def assert_untouched(ship, saved, result):
    before, beat, lock = saved
    for path, raw in before.items():
        assert path.exists() and path.read_bytes() == raw, path
    assert beat.read_bytes().startswith(b'{"pid": 1')
    assert servicectl.lock_path(ship.runtime).read_bytes() == lock
    assert not (ship.runtime / 'evidence' / 'delivery-boundary').exists()
    blob = result + '\n' + json.dumps(log_lines(ship))
    assert SENTINEL not in blob
    assert 'QD_SHIP=' not in blob


def seat_for(ship, name='ops'):
    return SimpleNamespace(name=name, ship=ship, session=SimpleNamespace(human=False), is_up=True,
                           trusted=ship.runtime / 'trusted' / name, record=lambda: None)


def open_attempt(ship, name='ops'):
    store = post.Store(ship.runtime)
    attempts = post.Attempts(ship.runtime)
    message = store.new('order', 'operator', name, 'Synthetic boundary order')
    launch = STAMP
    attempt = attempts.open(message, name, '%9', 4242, launch)
    return store, attempts, message, attempt


def write_journal(ship, attempt, phase, *, prepared=False):
    path = ship.runtime / 'post' / 'pending' / attempt.seat / attempt.path.name
    path.parent.mkdir(parents=True, exist_ok=True)
    data = dict(seat=attempt.seat, phase=phase, payload=SENTINEL, message=attempt.message, shown=None,
                record=None, attempts=[attempt.path.name], at=STAMP, since=STAMP,
                identity=dict(pane='%9', pid=4242, launch=STAMP, harness='fake'))
    if prepared:
        data['paste_boundary'] = shutdown.BEFORE_PASTE
        data['phase'] = 'prepared'
    path.write_text(json.dumps(data) + '\n')
    return path


def service_for(ship, store, attempts):
    svc = object.__new__(Service)
    svc.runtime = ship.runtime
    svc.ship = ship
    svc.store, svc.attempts = store, attempts
    svc.seat = lambda name: seat_for(ship, name)
    svc.resolved_seats = lambda: []
    svc.heartbeat = lambda: None
    svc.log = lambda *args, **kwargs: None
    svc.blind_checked = 10**12
    svc.hold_selection = lambda error: None
    return svc


def test_a_live_scoped_service_refuses(tmp_path):
    ship = ship_at(tmp_path)
    pointer = plant_pointer(ship, 'post/pending/ops/synthetic.json')
    raw = pointer.read_bytes()
    member, _ = started(SLEEPER)
    saved = untouched(ship, pointer)
    try:
        result = delivery_boundary.settle(ship, tmux(), 'ops', cgroup_of=scope_of(ship, {member.pid}))
        assert result.startswith('refused: ') and f'pid {member.pid} ' in result and 'scope' in result, result
        assert pointer.read_bytes() == raw
        assert_untouched(ship, saved, result)
        assert log_lines(ship)[-1]['event'] == 'delivery-boundary-settle-refused'
    finally:
        ended(member)


def test_an_unscoped_service_holding_the_lock_refuses(tmp_path):
    ship = ship_at(tmp_path)
    pointer = plant_pointer(ship, 'post/pending/ops/synthetic.json')
    raw = pointer.read_bytes()
    holder, _ = started(HOLDER, str(servicectl.lock_path(ship.runtime)))
    saved = untouched(ship, pointer)
    try:
        result = delivery_boundary.settle(ship, tmux(), 'ops', cgroup_of=scope_of(ship, set()))
        assert result.startswith('refused: ') and "the service's lock is held" in result, result
        assert str(holder.pid) in result
        assert pointer.read_bytes() == raw
        assert_untouched(ship, saved, result)
    finally:
        ended(holder)


def test_a_holder_of_a_replaced_lock_inode_refuses(tmp_path):
    ship = ship_at(tmp_path)
    lock = servicectl.lock_path(ship.runtime)
    info = lock.stat()
    stamp = dict(version=1, pid=1, start='1', token='replaced', device=info.st_dev, inode=info.st_ino)
    lock.write_text(json.dumps(stamp) + '\n')
    holder, _ = started(HOLDER, str(lock))
    try:
        os.rename(lock, lock.with_name('service.lock.replaced'))
        lock.write_text(json.dumps(stamp) + '\n')  # the old inode's stamp, on a new file nobody flocks
        assert shutdown.owner(ship.runtime) is None, 'a copied stamp does not read as this file'
        pointer = plant_pointer(ship, 'post/pending/ops/synthetic.json')
        raw = pointer.read_bytes()
        result = delivery_boundary.settle(ship, tmux(), 'ops', cgroup_of=scope_of(ship, set()))
        assert result.startswith('refused: ') and f'pid {holder.pid} ' in result and 'lock' in result, result
        assert pointer.read_bytes() == raw
        assert not (ship.runtime / 'evidence' / 'delivery-boundary').exists()
        assert SENTINEL not in result
    finally:
        ended(holder)


def test_a_qd_ship_carrier_refuses(tmp_path):
    ship = ship_at(tmp_path)
    pointer = plant_pointer(ship, 'post/pending/ops/synthetic.json')
    raw = pointer.read_bytes()
    value = 'synthetic-env-value-not-shown'
    carrier, _ = started(SLEEPER, env={**os.environ, 'QD_SHIP': str(ship.folder), 'QD_SYNTHETIC_SECRET': value})
    saved = untouched(ship, pointer)
    try:
        result = delivery_boundary.settle(ship, tmux(), 'ops', cgroup_of=scope_of(ship, set()))
        assert result.startswith('refused: ') and f'pid {carrier.pid} ' in result, result
        assert value not in result and value not in json.dumps(log_lines(ship))
        assert pointer.read_bytes() == raw
        assert_untouched(ship, saved, result)
    finally:
        ended(carrier)


def test_a_pointer_changed_mid_act_refuses(tmp_path):
    ship = ship_at(tmp_path)
    pointer = plant_pointer(ship, 'post/pending/ops/synthetic.json')
    raw = pointer.read_bytes()
    state = {'done': False}

    def reading(pid):
        if not state['done']:
            pointer.write_bytes(pointer.read_bytes() + b' ')  # size and ctime change after the first stamp
            state['done'] = True
        return scope_of(ship, set())(pid)

    result = delivery_boundary.settle(ship, tmux(), 'ops', cgroup_of=reading)
    assert 'changed' in result and result.startswith('refused: '), result
    assert pointer.exists() and pointer.read_bytes() != raw
    assert not (ship.runtime / 'evidence' / 'delivery-boundary').exists()
    assert not delivery_boundary.mark_path(ship.runtime).exists()
    assert not delivery_boundary.held_journal(ship.runtime, 'post/pending/ops/synthetic.json')


def test_a_pointer_finish_resolves_refuses(tmp_path):
    ship = ship_at(tmp_path)
    journal = evidence(ship.runtime, old_owner(ship.runtime), 'resolvable')
    before = journal.read_bytes()
    pointer = shutdown.pointer_path(ship.runtime)
    assert shutdown.finish_would_resolve(ship.runtime)
    result = delivery_boundary.settle(ship, tmux(), 'ops', cgroup_of=scope_of(ship, set()))
    assert result.startswith('refused: ') and 'a start clears it' in result, result
    assert pointer.is_file() and journal.read_bytes() == before and b'service_stop' not in before
    assert not (ship.runtime / 'evidence' / 'delivery-boundary').exists()
    assert SENTINEL not in result and 'synthetic journal words' not in result


def test_a_non_systems_seat_refuses(tmp_path):
    ship = ship_at(tmp_path)
    pointer = plant_pointer(ship, 'post/pending/ops/synthetic.json')
    raw = pointer.read_bytes()
    result = delivery_boundary.settle(ship, tmux(), 'builder', cgroup_of=scope_of(ship, set()))
    assert result == ('refused: builder is a worker seat; only a systems seat or the operator '
                      'settles the delivery boundary'), result
    stranger = delivery_boundary.settle(ship, tmux(), 'stranger')
    assert stranger.startswith('refused: stranger is not a seat of synthetic'), stranger
    assert pointer.read_bytes() == raw and not log_lines(ship)
    from quarterdeck import cli
    assert cli.build_parser().parse_args(['--ship', str(ship.folder), 'service', '--settle-delivery']).settle_delivery
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(['--ship', str(ship.folder), 'service', '--settle-delivery', '--settle-barrier'])


def _bwrap(code, *args):
    """The ship's own jail shape: a fresh proc, so the inner pid 1 is bwrap and NSpid shows no outer pid."""
    return ['bwrap', '--ro-bind', '/', '/', '--dev', '/dev', '--proc', '/proc',
            '--unshare-user', '--unshare-pid', '--die-with-parent',
            sys.executable, '-I', '-c', code, *args]


def test_a_jailed_caller_refuses(tmp_path):
    probe = subprocess.run(
        _bwrap('from pathlib import Path\n'
               'raise SystemExit(0 if Path("/proc/1/comm").read_text() == "bwrap\\n" else 1)\n'),
        capture_output=True, timeout=20)
    if probe.returncode != 0:
        pytest.skip('bubblewrap cannot make a pid namespace here')
    ship = ship_at(tmp_path)
    pointer = plant_pointer(ship, 'post/pending/ops/synthetic.json')
    raw = pointer.read_bytes()
    src = Path(delivery_boundary.__file__).resolve().parents[1]
    code = r'''
import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from types import SimpleNamespace
from quarterdeck import delivery_boundary
from quarterdeck.ship import Pool, SeatSpec, Session, Ship
folder, runtime = map(Path, sys.argv[2:4])
seats = {"ops": SeatSpec("ops", "crew", "systems", "claude", ("local",)),
         "builder": SeatSpec("builder", "crew", "worker", "claude", ("local",))}
ship = Ship(folder, "synthetic", "operator", runtime, {"local": Pool("local", "synthetic-endpoint")},
            {"crew": Session("crew", "crew")}, seats)
result = delivery_boundary.settle(ship, SimpleNamespace(server_pid=lambda: None, socket="/no/such/socket"), "ops")
print(result)
'''
    child = subprocess.run(
        _bwrap(code, str(src), str(ship.folder), str(ship.runtime)),
        capture_output=True, timeout=60, text=True)
    assert child.returncode == 0, child.stderr
    result = child.stdout.strip()
    assert 'jail' in result and result.startswith('refused: '), result
    assert pointer.read_bytes() == raw
    assert not (ship.runtime / 'evidence' / 'delivery-boundary').exists()
    assert not log_lines(ship)


def test_an_unreadable_descriptor_directory_is_named_unless_in_a_service_scope(tmp_path):
    """A process that is not dumpable hides its descriptors. Outside a service scope it is named and is not a
    holder. Inside any qd-service-* scope it refuses, even when its environment reads as empty."""
    ship = ship_at(tmp_path)
    plant_pointer(ship, 'post/pending/ops/synthetic.json')
    hidden, _ = started(UNDUMPABLE)
    try:
        with pytest.raises(PermissionError):
            list((Path('/proc') / str(hidden.pid) / 'fd').iterdir())

        def environ_of(pid, proc=Path('/proc')):
            if pid == hidden.pid:
                return b''
            return delivery_boundary.barrier_settle._environ(pid, proc)

        result = delivery_boundary.settle(
            ship, tmux(), 'ops', cgroup_of=scope_of(ship, set()), environ_of=environ_of)
        assert result.startswith('delivery boundary moved aside unchanged: '), result
        assert f'pid {hidden.pid} {comm(hidden.pid)}' in result.split('descriptors unreadable to this user, named: ', 1)[1]
        plant_pointer(ship, 'post/pending/ops/synthetic.json')
        elsewhere = scope_of(ship, {hidden.pid}, unit='qd-service-another-0123abcd.scope')
        result = delivery_boundary.settle(ship, tmux(), 'ops', cgroup_of=elsewhere, environ_of=environ_of)
        assert result.startswith('refused: ') and "service's scope" in result, result
        assert f'pid {hidden.pid} {comm(hidden.pid)} in qd-service-another-0123abcd.scope' in result
        assert shutdown.pointer_path(ship.runtime).is_file()
        assert len(list((ship.runtime / 'evidence' / 'delivery-boundary').iterdir())) == 1
    finally:
        ended(hidden)


def test_a_symlink_pointer_refuses(tmp_path):
    ship = ship_at(tmp_path)
    target = tmp_path / 'elsewhere.json'
    target.write_text('{"version": 1}\n')
    pointer = shutdown.pointer_path(ship.runtime)
    pointer.symlink_to(target)
    result = delivery_boundary.settle(ship, tmux(), 'ops', cgroup_of=scope_of(ship, set()))
    assert 'symlink' in result and result.startswith('refused: '), result
    assert pointer.is_symlink() and target.read_text() == '{"version": 1}\n'
    assert not (ship.runtime / 'evidence' / 'delivery-boundary').exists()


def test_a_fifo_pointer_refuses(tmp_path):
    ship = ship_at(tmp_path)
    pointer = shutdown.pointer_path(ship.runtime)
    os.mkfifo(pointer)
    result = delivery_boundary.settle(ship, tmux(), 'ops', cgroup_of=scope_of(ship, set()))
    assert 'fifo' in result and result.startswith('refused: '), result
    assert pointer.is_fifo()
    assert not (ship.runtime / 'evidence' / 'delivery-boundary').exists()


def test_an_oversize_pointer_refuses(tmp_path):
    ship = ship_at(tmp_path)
    pointer = shutdown.pointer_path(ship.runtime)
    pointer.write_bytes(b'x' * 4097)
    result = delivery_boundary.settle(ship, tmux(), 'ops', cgroup_of=scope_of(ship, set()))
    assert 'oversize' in result and result.startswith('refused: '), result
    assert pointer.read_bytes() == b'x' * 4097
    assert not (ship.runtime / 'evidence' / 'delivery-boundary').exists()


def _settle_unknown(ship, relative, *, kind='pending', before=False):
    pointer = plant_pointer(ship, relative, kind=kind, before=before)
    raw = pointer.read_bytes()
    assert shutdown.startup_diagnosis(ship.runtime)
    result = delivery_boundary.settle(ship, tmux(), 'ops', cgroup_of=scope_of(ship, set()))
    assert result.startswith('delivery boundary moved aside unchanged: '), result
    assert not pointer.exists()
    assert shutdown.startup_diagnosis(ship.runtime) is None
    [folder] = (ship.runtime / 'evidence' / 'delivery-boundary').iterdir()
    assert (folder / 'service.delivery').read_bytes() == raw
    assert (folder / 'service.lock').read_bytes() == servicectl.lock_path(ship.runtime).read_bytes()
    mark = delivery_boundary.mark_path(ship.runtime).read_text()
    assert SENTINEL not in mark and SENTINEL not in result and SENTINEL not in json.dumps(log_lines(ship))
    rows = [json.loads(line) for line in mark.splitlines()]
    assert [row['outcome'] for row in rows] == ['settling', 'settled']
    assert rows[-1]['journal'] == relative and rows[-1]['problem'] == 'unknown service owner stamp'
    assert rows[-1]['lock_stamp'] == 'unreadable'
    assert delivery_boundary.held_journal(ship.runtime, relative)
    return result


def test_a_settled_boundary_holds_each_phase_past_prepared(tmp_path, monkeypatch):
    def fail_screen(*_args, **_kwargs):
        raise AssertionError('the screen was read')

    screen = SimpleNamespace(box='', unknown=False, interruption=False)
    monkeypatch.setattr('quarterdeck.door.Door.screen', fail_screen)
    for phase in ('pasting', 'submitting', 'submitted', 'queued'):
        root = tmp_path / phase
        root.mkdir()
        ship = ship_at(root)
        store, attempts, message, attempt = open_attempt(ship)
        journal = write_journal(ship, attempt, phase)
        relative = journal.relative_to(ship.runtime).as_posix()
        journal_bytes, attempt_bytes = journal.read_bytes(), attempt.path.read_bytes()
        _settle_unknown(ship, relative)
        assert journal.read_bytes() == journal_bytes and attempt.path.read_bytes() == attempt_bytes
        seat = seat_for(ship)
        svc = service_for(ship, store, attempts)
        assert pending.one_look(svc, seat, attempts.all()) == []
        assert journal.read_bytes() == journal_bytes
        pending.explicit_resend(svc, seat, [message], screen)
        assert journal.read_bytes() == journal_bytes
        pending._reconcile_claimed(svc, seat, {attempt.path.name: attempts.all()[0]})
        assert journal.read_bytes() == journal_bytes
        again = json.loads(attempt.path.read_text())
        assert again['result'] == 'uncertain' and again['detail'] == delivery_boundary.HOLD_DETAIL, phase
        assert b'service_stop' not in journal.read_bytes()


def test_a_settled_boundary_holds_an_attempt(tmp_path):
    ship = ship_at(tmp_path)
    store, attempts, message, attempt = open_attempt(ship)
    relative = attempt.path.relative_to(ship.runtime).as_posix()
    before = attempt.path.read_bytes()
    _settle_unknown(ship, relative, kind='attempt', before=True)
    assert attempt.path.read_bytes() == before
    assert len(list(attempts.dir.glob('*.json'))) == 1
    svc = service_for(ship, store, attempts)
    svc.reconcile()
    again = json.loads(attempt.path.read_text())
    assert again['result'] == 'uncertain' and 'qd post resend' not in again['detail']
    assert again['detail'] == delivery_boundary.HOLD_DETAIL
    assert len(list(attempts.dir.glob('*.json'))) == 1
    assert not (ship.runtime / 'post' / 'pending').exists() or not any(
        (ship.runtime / 'post' / 'pending').rglob('*.json'))


def test_a_settled_boundary_holds_a_human_wake(tmp_path):
    for phase in ('prepared', 'pasting'):
        root = tmp_path / phase
        root.mkdir()
        ship = ship_at(root)
        seat = seat_for(ship, 'ops')
        seat.session = SimpleNamespace(human=True)
        wake = seat.trusted / 'human-wake.json'
        wake.parent.mkdir(parents=True, exist_ok=True)
        wake.write_text(json.dumps(dict(
            format=1, seat='ops', payload=human_wake.LINE, phase=phase, attempts=[], ended=False,
            identity=dict(pane='%9', pid=4242, harness='fake', launch=STAMP), at=STAMP, since=STAMP,
            record=None, shown=None, generation=None)) + '\n')
        relative = wake.relative_to(ship.runtime).as_posix()
        before = wake.read_bytes()
        _settle_unknown(ship, relative, kind='human wake')
        assert wake.read_bytes() == before
        assert human_wake._run(seat) == 'human wake receipt uncertain; never resent; inspect human-wake.json'
        assert wake.read_bytes() == before


def test_a_prepared_journal_is_not_cancelled(tmp_path):
    ship = ship_at(tmp_path)
    store, attempts, _message, attempt = open_attempt(ship)
    journal = write_journal(ship, attempt, 'prepared', prepared=True)
    relative = journal.relative_to(ship.runtime).as_posix()
    before = journal.read_bytes()
    _settle_unknown(ship, relative)
    assert journal.read_bytes() == before
    svc = service_for(ship, store, attempts)
    pending._reconcile_claimed(svc, seat_for(ship), {attempt.path.name: attempts.all()[0]})
    assert journal.read_bytes() == before
    assert json.loads(journal.read_text())['phase'] == 'prepared'
    again = json.loads(attempt.path.read_text())
    assert again['result'] == 'uncertain'
