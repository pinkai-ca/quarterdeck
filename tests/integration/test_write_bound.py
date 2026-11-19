"""The write-bound alarm: a jailed seat whose writable folders pass their bound is stopped, held, and named.

Fake writers only, each in a real jail made by sandbox.plan and sandbox.command on the drydock, with small bounds;
no writer puts more than 256 MiB on the disk. The walk's own cases run outside any jail, in the test's folder.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import jail_environment, relaunch_intent, sandbox, seat as seatmod, ship as shipmod, write_bound
from quarterdeck.ship import ShipError

from .test_sandbox import namespaces_allowed

MiB = write_bound.MiB
SMALL = {write_bound.SEAT: 64 * MiB, write_bound.WORKSPACE: 64 * MiB, write_bound.SMALL: 8 * MiB}
jailed = pytest.mark.skipif(not namespaces_allowed(), reason='bubblewrap cannot make a namespace here')


@pytest.fixture
def ship(drydock: Path) -> shipmod.Ship:
    return shipmod.load(drydock)


def logger():
    logs = []
    return logs, (lambda event, **data: logs.append((event, data)))


# -- the walk ----------------------------------------------------------------------------------


def walked(folder, budget=None):
    group = write_bound.Group('test', [str(folder)], 1)
    write_bound.walk(str(folder), group, (budget or write_bound.Budget()).start())
    return group.held


def fill(path: Path, size: int) -> None:
    with open(path, 'wb') as stream:
        os.posix_fallocate(stream.fileno(), 0, size)


@pytest.fixture
def deep_place():
    """A folder outside the test run's own: the test tool measures its run by path, and a tree past the path limit
    would stop that measure. Removed by descriptors at the end (shutil.rmtree walks so on Linux)."""
    import tempfile
    place = Path(tempfile.mkdtemp(prefix='qd-write-bound-', dir='/dev/shm' if os.access('/dev/shm', os.W_OK) else None))
    try:
        yield place
    finally:
        shutil.rmtree(place)


def test_the_walk_counts_by_descriptors_below_the_path_limit(deep_place):
    """Data a walk by path could not reach: 30 folders of 200-byte names, 6,000 bytes below the top."""
    tmp_path = deep_place
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    for _ in range(30):
        os.mkdir('d' * 200, dir_fd=fd)
        child = os.open('d' * 200, os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
        os.close(fd)
        fd = child
    big = os.open('big', os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=fd)
    os.posix_fallocate(big, 0, 32 * MiB)
    os.close(big)
    os.close(fd)
    assert walked(tmp_path) >= 32 * MiB


def test_the_walk_counts_an_inode_once_and_follows_no_link(tmp_path):
    inside, outside = tmp_path / 'inside', tmp_path / 'outside'
    inside.mkdir()
    outside.mkdir()
    fill(inside / 'one', 16 * MiB)
    for n in range(100):
        os.link(inside / 'one', inside / f'link{n}')
    fill(outside / 'big', 32 * MiB)
    (inside / 'out').symlink_to(outside)
    (inside / 'file').symlink_to(outside / 'big')
    held = walked(inside)
    assert 16 * MiB <= held < 17 * MiB, 'one inode, a hundred names; and no link followed out'


def test_a_walk_that_reaches_a_bound_reads_as_over(tmp_path):
    for n in range(300):
        (tmp_path / f'f{n}').touch()
    with pytest.raises(write_bound.Over, match='passed 200 entries'):
        walked(tmp_path, write_bound.Budget(entries=200))
    deep = tmp_path / 'deep'
    deep.mkdir()
    place = deep
    for _ in range(12):
        place = place / 'd'
        place.mkdir()
    with pytest.raises(write_bound.Over, match='10 folders deep'):
        walked(deep, write_bound.Budget(depth=10))
    with pytest.raises(write_bound.Over, match='passed 0 s'):
        walked(tmp_path, write_bound.Budget(seconds=0))
    assert walked(tmp_path / 'absent') == 0, 'a bind-try folder that is not there holds nothing'
    (tmp_path / 'link').symlink_to(tmp_path)
    with pytest.raises(write_bound.Over, match='could not be opened'):
        walked(tmp_path / 'link')


# -- the gate: fake writers in real jails ------------------------------------------------------------


WRITERS = {
    'streaming': """
import sys, time
with open(sys.argv[1] + '/stream', 'wb') as out:
    for _ in range(256):
        out.write(b'x' * (1 << 20))
    out.flush()
    time.sleep(600)
""",
    'many': """
import sys, time
for n in range(4096):
    with open(f'{sys.argv[1]}/f{n}', 'wb') as out:
        out.write(b'x' * (64 << 10))
time.sleep(600)
""",
    'unlinked': """
import os, sys, time
out = open(sys.argv[1] + '/gone', 'wb')
os.unlink(sys.argv[1] + '/gone')
for _ in range(256):
    out.write(b'x' * (1 << 20))
out.flush()
time.sleep(600)
""",
    'entries': """
import sys, time
for n in range(3000):
    open(f'{sys.argv[1]}/e{n}', 'wb').close()
time.sleep(600)
""",
    'deep': """
import os, sys, time
fd = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY)
for _ in range(30):
    os.mkdir('d' * 200, dir_fd=fd)
    child = os.open('d' * 200, os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
    os.close(fd)
    fd = child
big = os.open('big', os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=fd)
os.posix_fallocate(big, 0, 128 << 20)
time.sleep(600)
""",
    'linked': """
import os, sys, time
with open(sys.argv[1] + '/one', 'wb') as out:
    os.posix_fallocate(out.fileno(), 0, 40 << 20)
for n in range(100):
    os.link(sys.argv[1] + '/one', f'{sys.argv[1]}/link{n}')
open(sys.argv[1] + '/ready', 'w').close()
time.sleep(600)
""",
}


def jail_for(ship, workspace: Path):
    own = ship.runtime / 'seats' / 'worker-1'
    (own / 'home').mkdir(parents=True, exist_ok=True)
    return sandbox.plan(ship, 'worker-1', own, own / 'home', workspace, '/usr/bin/python3', '/usr/bin', home=Path.home())


def start(ship, workspace: Path, writer: str, target: Path | None = None):
    jail = jail_for(ship, workspace)
    argv = sandbox.command(jail, ['/usr/bin/python3', '-c', WRITERS[writer], str(target or workspace)])
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def until(predicate, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if value := predicate():
            return value
        time.sleep(0.02)
    return None


class Service:
    def __init__(self, ship):
        self.ship, self.runtime = ship, ship.runtime
        self.logs, self.log = logger()


@pytest.fixture
def small(monkeypatch):
    monkeypatch.setattr(write_bound, 'BOUNDS', dict(SMALL))


@jailed
@pytest.mark.parametrize('writer', ['streaming', 'many', 'unlinked', 'deep'])
def test_a_jailed_writer_past_its_bound_is_stopped_in_under_two_seconds(ship, tmp_path, small, writer, request):
    workspace = (request.getfixturevalue('deep_place') if writer == 'deep' else tmp_path) / 'work'
    workspace.mkdir()
    service = Service(ship)
    jail = start(ship, workspace, writer)
    began = time.monotonic()
    alarm = write_bound.Alarm(service).start()
    try:
        held = until(lambda: write_bound.path(ship.runtime, 'worker-1').exists(), 5)
        took = time.monotonic() - began
        jail.wait(timeout=5)
    finally:
        alarm.close(timeout=3)
        if jail.poll() is None:
            jail.kill()
            jail.wait()
    assert held and took < 2.0, f'{writer}: stopped after {took:.2f} s'
    assert jail.returncode == -9, 'the jail was killed'
    assert write_bound.jails(ship) == {}, 'nothing of the jail is left'
    event, data = next(entry for entry in service.logs if entry[0] == 'write-bound')
    assert data['seat'] == 'worker-1' and data['group'] in (write_bound.WORKSPACE, write_bound.SEAT) and data['killed'] >= 1
    assert data['held'] > SMALL[write_bound.WORKSPACE] or data['why'] != 'over its bound'
    reason = write_bound.reason(ship.runtime, 'worker-1')
    assert reason.startswith('stopped at its write bound (') and reason.endswith('held: qd up worker-1 lifts it')


@jailed
def test_a_walk_that_hits_its_entry_bound_stops_the_jail(ship, tmp_path, small):
    workspace = tmp_path / 'work'
    workspace.mkdir()
    jail = start(ship, workspace, 'entries')
    try:
        until(lambda: len(os.listdir(workspace)) >= 3000)
        logs, log = logger()
        stopped = write_bound.look(ship, log, budget=write_bound.Budget(entries=1000))
        jail.wait(timeout=5)
    finally:
        if jail.poll() is None:
            jail.kill()
    assert stopped == ['worker-1'] and 'passed 1000 entries' in logs[0][1]['why']
    assert 'passed 1000 entries' in write_bound.reason(ship.runtime, 'worker-1')


@jailed
def test_hard_links_are_counted_once_and_the_folders_are_the_jails_own(ship, tmp_path, small):
    workspace = tmp_path / 'work'
    workspace.mkdir()
    jail = start(ship, workspace, 'linked')
    try:
        assert until(lambda: (workspace / 'ready').exists())
        found = write_bound.jails(ship)
        assert set(found) == {'worker-1'}
        reading = write_bound.measure(ship, found['worker-1'])
        groups = {group.label: group for group in reading.groups}
        assert groups[write_bound.WORKSPACE].folders == [str(workspace.resolve())]
        assert groups[write_bound.SEAT].folders == [str(ship.runtime / 'seats' / 'worker-1')]
        assert sorted(groups[write_bound.SMALL].folders) == sorted(
            [str(ship.runtime / 'post' / 'drop' / 'worker-1'), str(ship.runtime / 'check' / 'worker-1')])
        assert reading.over is None, 'forty MiB under a hundred names is forty MiB'
        assert 40 * MiB <= groups[write_bound.WORKSPACE].held < 41 * MiB
        assert jail.pid in reading.pids and len(reading.pids) >= 2, 'the jail and the writer inside it'
    finally:
        jail.kill()
        jail.wait(timeout=5)


@jailed
def test_the_drop_has_its_own_small_bound(ship, tmp_path, small):
    workspace = tmp_path / 'work'
    workspace.mkdir()
    jail = start(ship, workspace, 'streaming', target=ship.runtime / 'post' / 'drop' / 'worker-1')
    try:
        stream = ship.runtime / 'post' / 'drop' / 'worker-1' / 'stream'
        assert until(lambda: stream.exists() and stream.stat().st_blocks * 512 > 16 * MiB)
        logs, log = logger()
        assert write_bound.look(ship, log) == ['worker-1']
        jail.wait(timeout=5)
    finally:
        if jail.poll() is None:
            jail.kill()
    assert logs[0][1]['group'] == write_bound.SMALL


@jailed
def test_one_file_is_capped_at_the_launch(ship, tmp_path):
    workspace = tmp_path / 'work'
    workspace.mkdir()
    probe = r"""
import os, resource, signal, sys
print(resource.getrlimit(resource.RLIMIT_FSIZE)[0])
with open(sys.argv[1] + '/big', 'wb') as out:
    try:
        os.truncate(out.fileno(), int(sys.argv[2]) + 1)
        print('grew')
    except OSError as error:
        print(error.errno)
"""
    jail = jail_for(ship, workspace)
    done = subprocess.run(sandbox.command(jail, ['/usr/bin/python3', '-c', probe, str(workspace),
                                                 str(jail_environment.FILE_BYTES)]),
                          capture_output=True, text=True, timeout=60)
    import errno
    assert done.stdout.split() == [str(jail_environment.FILE_BYTES), str(errno.EFBIG)], done.stderr
    # A program that does not ignore SIGXFSZ ends at it, as dd did in the measurement.
    shell = subprocess.run(sandbox.command(jail, ['/bin/sh', '-c', f'truncate -s {jail_environment.FILE_BYTES + 1} '
                                                  f'{workspace}/big2; echo $?']),
                           capture_output=True, text=True, timeout=60)
    assert shell.stdout.strip() != '0' and not (workspace / 'big2').exists() or (workspace / 'big2').stat().st_size == 0


def test_the_kill_never_signals_a_pid_given_to_another_process(tmp_path):
    other = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    try:
        assert write_bound.kill({other.pid: 'not-its-start'}) == []
        assert other.poll() is None
    finally:
        other.kill()
        other.wait()


# -- the hold ----------------------------------------------------------------------------------------


def leave_intent(ship, seat='worker-1'):
    folder = relaunch_intent.folder(ship.runtime)
    folder.mkdir(parents=True, exist_ok=True)
    data = dict(version=1, seat=seat, kind='after-order', resume=False, at=datetime.now(timezone.utc).isoformat(),
                phase=relaunch_intent.RELAUNCHING, before=None,
                owner=dict(version=1, pid=999999999, start='synthetic-start-zzzz', token='synthetic-owner-zzzz',
                           device=1, inode=1))
    relaunch_intent.path(ship.runtime, seat).write_text(json.dumps(data) + '\n')


def reading(seat='worker-1', why='over its bound'):
    return write_bound.Reading(seat, [], (write_bound.WORKSPACE, 3 * write_bound.GiB, 2 * write_bound.GiB, why), {})


def service_log(ship):
    log = ship.runtime / 'log' / 'service.jsonl'
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def test_placing_the_hold_clears_the_intent_and_a_new_service_never_finishes_it(ship, tmux):
    leave_intent(ship)
    write_bound.place(ship.runtime, reading())
    assert not relaunch_intent.path(ship.runtime, 'worker-1').exists(), 'placing the hold cleared the intent'
    assert any(entry.get('outcome') == 'cleared' and 'write-bound hold' in entry['reason'] for entry in service_log(ship))
    worker = lambda name: seatmod.Seat(ship, ship.seat(name), tmux)
    assert write_bound._held in relaunch_intent.HOLDS
    assert relaunch_intent.hold(worker('worker-1')).startswith('stopped at its write bound (workspace 3.00 GiB of 2.00 GiB)')
    # An intent a later service wrote before the hold was read: the next service still never finishes it.
    leave_intent(ship)
    logs, log = logger()
    relaunch_intent.finish(SimpleNamespace(runtime=ship.runtime, ship=ship, logs=logs, seat=worker, log=log))
    assert not worker('worker-1').is_up, 'a held seat is not relaunched by a new service'
    assert relaunch_intent.path(ship.runtime, 'worker-1').exists()
    assert relaunch_intent.status(ship, {'worker-1': worker('worker-1')}) == [
        'worker-1: relaunch intent left as it is while held: stopped at its write bound (workspace 3.00 GiB of '
        '2.00 GiB); held: qd up worker-1 lifts it']
    relaunch_intent.clear(ship.runtime, 'worker-1', 'the test')
    with pytest.raises(ShipError, match='worker-1: stopped at its write bound'):
        worker('worker-1')._up(resume=False)
    assert not worker('worker-1').is_up


def test_a_whole_ship_up_leaves_a_held_seat_down_and_its_own_word_lifts_it(ship, tmux, monkeypatch):
    import argparse
    from quarterdeck import cli

    write_bound.place(ship.runtime, reading())
    proofs = seatmod.bring_up(ship, tmux)
    assert 'worker-1 left down: stopped at its write bound' in '\n'.join(proofs)
    assert not seatmod.Seat(ship, ship.seat('worker-1'), tmux).is_up
    for seat in seatmod.seats(ship, tmux):
        if seat.is_up:
            seat.down()
    monkeypatch.setenv('QD_TMUX_SOCKET', tmux.socket)
    monkeypatch.setenv('QD_SEAT', 'crew-seat')
    monkeypatch.setattr(cli.service, 'start', lambda ship: 'service not started in this test')
    monkeypatch.setattr(cli.install, 'delegate_up', lambda *a, **k: None)
    monkeypatch.setattr(cli.install, 'prepare_up', lambda ship: ([], False))
    cli.cmd_up(argparse.Namespace(ship=str(ship.folder), seat='worker-1', fresh=True, pool=None))
    seat = seatmod.Seat(ship, ship.seat('worker-1'), tmux)
    try:
        assert seat.is_up and write_bound.reason(ship.runtime, 'worker-1') is None
        assert any(entry.get('event') == 'write-bound' and entry['reason'] == 'lifted by qd up by crew-seat'
                   for entry in service_log(ship))
    finally:
        seat.down()


def test_a_hold_that_cannot_be_read_still_holds(ship):
    folder = write_bound.folder(ship.runtime)
    folder.mkdir(parents=True)
    (folder / 'worker-1.json').write_text('not json')
    assert write_bound.reason(ship.runtime, 'worker-1') == \
        'its write-bound hold is unreadable (JSONDecodeError); held: qd up worker-1 lifts it'
    (folder / 'worker-1.json').unlink()
    (folder / 'worker-1.json').symlink_to(ship.folder / 'ship.toml')
    assert 'not a plain file' in write_bound.reason(ship.runtime, 'worker-1')
    assert not write_bound.lift(ship.runtime, 'worker-1', 'the test') and (folder / 'worker-1.json').is_symlink()


# -- the status line ------------------------------------------------------------------------------------


def test_the_status_line_names_a_seat_stopped_at_its_write_bound(ship, tmux):
    from quarterdeck import headlines, watch

    write_bound.place(ship.runtime, reading(why='the walk passed 2 s'))
    facts = watch.facts(ship, tmux)
    words = 'worker-1: stopped at its write bound (workspace 3.00 GiB of 2.00 GiB, the walk passed 2 s); ' \
            'held: qd up worker-1 lifts it'
    assert facts.write_bounds == [words] and words in facts.line()  # law: nothing-waits-silently
    crowded = SimpleNamespace(**{**vars(facts), 'pending': [f'pending line {n} ' + 'x' * 40 for n in range(10)]})
    short = headlines.status_line(crowded, ['service ok', *crowded.pending, *crowded.write_bounds])
    assert '1 at write bound' in short and short.endswith(' · qd status')
    assert words in headlines.details(crowded)


# -- a real seat ---------------------------------------------------------------------------------------


@jailed
def test_a_launched_jailed_seat_is_found_stopped_and_kept_down(drydock, tmux, tmp_path, small):
    """A seat launched by qd in its pane, jailed, writes past its bound when an order tells it to."""
    from quarterdeck import post
    from quarterdeck.door import Door
    from .waiting import wait_for

    workspace = tmp_path / 'work'
    workspace.mkdir()
    text = (drydock / 'ship.toml').read_text()
    text = shipmod.edit(text, 'seats.worker-1', 'sandbox', True)
    text = shipmod.edit(text, 'seats.worker-1', 'cwd', str(workspace))
    (drydock / 'ship.toml').write_text(text + '\n[seats.worker-1.settings]\nstep_seconds = "0.05"\n')
    ship = shipmod.load(drydock)
    seat = seatmod.Seat(ship, ship.seat('worker-1'), tmux)
    try:
        seat.up(resume=False)
        launched = seat.launched()
        found = write_bound.jails(ship)
        assert set(found) == {'worker-1'} and launched.pid in found['worker-1'].pids, 'the pane runs the jail'
        assert str(workspace.resolve()) in found['worker-1'].binds
        assert write_bound.measure(ship, found['worker-1']).over is None, 'a fresh seat is under its bounds'
        wait_for(lambda: Door(seat).reading().open)
        store = post.Store(ship.runtime)
        message = store.new('order', 'desk', 'worker-1', f'Write.\n$ head -c 100000000 /dev/zero > {workspace}/big; sleep 600',
                            subject='Write')
        assert Door(seat).deliver(post.typed([message])).result == 'delivered'
        assert until(lambda: (workspace / 'big').exists() and (workspace / 'big').stat().st_size > 64 * MiB, 20)
        service = Service(ship)
        assert write_bound.look(ship, service.log) == ['worker-1']
        assert until(lambda: not seat.is_up, 10), 'the pane ended with its jail'
        assert write_bound.jails(ship) == {}
        with pytest.raises(ShipError, match='stopped at its write bound'):
            seat.up(resume=True)
        assert not seat.is_up
    finally:
        write_bound.lift(ship.runtime, 'worker-1', 'the test')
        seat.down()
