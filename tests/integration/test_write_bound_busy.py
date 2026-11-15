"""The write-bound alarm on a busy machine: the clock alone never kills, and a jail that loads the machine on purpose
is still stopped.

On 15 Nov the alarm killed a hand whose seat folder held 531 MB of its 4 GB bound: the walk of its home, 47 entries,
passed its 2 s bound on the clock at load 12 to 16, because the alarm thread was starved by the busy service it
shares the interpreter with. These gates make that machine here, in the test's own processes: CPU burners outside the
jail, each ending by itself and killed at the end, and a thread of this interpreter that holds the interpreter's lock
in one long call, as a busy service does, started from inside the walk so that the walk is surely starved while it
runs. No tmux is used.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from quarterdeck import ship as shipmod, write_bound

from .test_write_bound import MiB, SMALL, jail_for, jailed, logger, until

IDLE = 'import time; time.sleep(600)'
# Burns every CPU it is given from inside the jail, ending by itself, while it writes past its bound. argv[1] is the
# folder; a 'sub' folder of many small files is made after the stream, so a walk reaches the stream first.
BURNING = r"""
import os, sys, time
end = time.monotonic() + 120
for _ in range(os.cpu_count() or 4):
    if os.fork() == 0:
        while time.monotonic() < end:
            pass
        os._exit(0)
with open(sys.argv[1] + '/stream', 'wb') as out:
    for _ in range(256):
        out.write(b'x' * (1 << 20))
    out.flush()
os.mkdir(sys.argv[1] + '/sub')
for n in range(200):
    open(f'{sys.argv[1]}/sub/s{n}', 'wb').close()
open(sys.argv[1] + '/ready', 'w').close()
time.sleep(120)
"""
ENTRIES = r"""
import os, sys, time
end = time.monotonic() + 120
for _ in range(os.cpu_count() or 4):
    if os.fork() == 0:
        while time.monotonic() < end:
            pass
        os._exit(0)
for n in range(3000):
    open(f'{sys.argv[1]}/e{n}', 'wb').close()
time.sleep(120)
"""
BURN_OUTSIDE = 'import time\nend = time.monotonic() + 90\nwhile time.monotonic() < end:\n    pass\n'


@pytest.fixture
def ship(drydock: Path) -> shipmod.Ship:
    return shipmod.load(drydock)


def jailed_program(ship, workspace: Path, program: str):
    from quarterdeck import sandbox
    argv = sandbox.command(jail_for(ship, workspace), ['/usr/bin/python3', '-c', program, str(workspace)])
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def ended(jail):
    if jail.poll() is None:
        jail.kill()
    jail.wait(timeout=10)


@contextmanager
def busy_machine():
    """One CPU burner per CPU, outside the jail, in this test's own processes; each ends by itself, all are killed."""
    burners = [subprocess.Popen([sys.executable, '-c', BURN_OUTSIDE], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for _ in range(os.cpu_count() or 4)]
    try:
        yield
    finally:
        for burner in burners:
            burner.kill()
        for burner in burners:
            burner.wait(timeout=10)


def hand_home(ship) -> Path:
    """A seat folder of a hand's shape: 47 entries in its home, a data folder for each launch, 84 MiB in all."""
    home = ship.runtime / 'seats' / 'worker-1' / 'home'
    home.mkdir(parents=True, exist_ok=True)
    for n in range(40):
        (home / f'data-{n:03d}').mkdir()
    for n in range(7):
        with open(home / f'transcript-{n}.jsonl', 'wb') as stream:
            os.posix_fallocate(stream.fileno(), 0, 12 * MiB)
    return home


PATTERN = re.compile(r'(x+x+)+y')


def holding(seconds: float) -> str:
    """A subject the pattern takes at least ``seconds`` to refuse, in one call that never lets go of the lock: the
    first size whose own call took that long, measured on the machine as busy as it is, and one more x, which
    doubles it, for the margin."""
    for size in range(16, 40):
        began = time.monotonic()
        PATTERN.match('x' * size)
        if time.monotonic() - began >= seconds:
            return 'x' * (size + 1)
    raise AssertionError('no subject long enough to hold the lock')


@contextmanager
def starved(monkeypatch, where, seconds: float = 2.5):
    """The first scan of the folder ``where`` names is starved: another thread of this interpreter takes its lock in
    one call of at least ``seconds`` while the walk is in it. Yields what was seen: the hold's length, once run."""
    subject, seen = holding(seconds), {}
    scan = write_bound._scan

    def hog(began):
        began.set()  # from here this thread keeps the lock: the call below never lets go of it
        start = time.monotonic()
        PATTERN.match(subject)
        seen['held'] = time.monotonic() - start

    def starving(fd, device, group, budget):
        if 'held' not in seen and where(os.readlink(f'/proc/self/fd/{fd}')):
            seen['held'] = None
            began = threading.Event()
            thread = threading.Thread(target=hog, args=(began,), daemon=True)
            thread.start()
            began.wait()
            seen['thread'] = thread
        return scan(fd, device, group, budget)

    monkeypatch.setattr(write_bound, '_scan', starving)
    try:
        yield seen
    finally:
        monkeypatch.setattr(write_bound, '_scan', scan)
        if 'thread' in seen:
            seen['thread'].join(timeout=120)


def cpu_and_clock(function):
    began, cpu = time.monotonic(), time.thread_time()
    value = function()
    return value, time.monotonic() - began, time.thread_time() - cpu


# -- a hand-shaped seat folder, on a busy machine ------------------------------------------------------------------


@jailed
def test_a_hand_shaped_seat_folder_on_a_busy_machine_stays_up_unmeasured_then_measured(ship, tmp_path, monkeypatch):
    workspace = tmp_path / 'work'
    workspace.mkdir()
    home = os.path.realpath(hand_home(ship))
    jail = jailed_program(ship, workspace, IDLE)
    logs, log = logger()
    try:
        assert until(lambda: 'worker-1' in write_bound.jails(ship))
        with busy_machine():
            # The fault itself: with the bounds as they stand, the walk takes longer than 2 s on the clock and well
            # under it in CPU. It is measured, and the seat stays up.
            with starved(monkeypatch, lambda path: path == home) as seen:
                stopped, clock, cpu = cpu_and_clock(lambda: write_bound.look(ship, log))
            assert seen['held'] and seen['held'] > write_bound.SECONDS, 'the walk was starved past the old 2 s bound'
            assert clock > write_bound.SECONDS > cpu, f'{clock:.2f} s on the clock, {cpu:.3f} s of CPU'
            assert stopped == [] and write_bound.path(ship.runtime, 'worker-1').exists() is False
            assert write_bound.unmeasured_read(ship.runtime, 'worker-1') is None, 'measured, under its bound'

            # Starved past the clock's own bound (a short one here, 1 s, in place of 10 s): unmeasured, never over.
            with starved(monkeypatch, lambda path: path == home):
                stopped = write_bound.look(ship, log, budget=write_bound.Budget(wall=1.0))
            assert stopped == [], 'the clock alone never stops a seat'
            assert jail.poll() is None and not write_bound.path(ship.runtime, 'worker-1').exists()
            event, data = logs[-1]
            assert event == 'write-bound' and data['outcome'] == 'unmeasured' and data['passes'] == 1
            assert data['why'] == 'the walk passed 1 s on the clock'
            line = ('worker-1: write bound unmeasured, 1 of 3 passes in a row (the walk passed 1 s on the clock); '
                    'retried each pass, stopped at 3')
            assert write_bound.status(ship) == [line]
        # The machine quiet again: the next pass, with the same bounds, measures it, and the count ends.
        assert write_bound.look(ship, log, budget=write_bound.Budget(wall=1.0)) == []
        assert write_bound.unmeasured_read(ship.runtime, 'worker-1') is None and write_bound.status(ship) == []
        assert jail.poll() is None, 'the seat stayed up throughout'
    finally:
        ended(jail)


def test_the_status_line_names_a_seat_left_unmeasured(ship, tmux):
    from quarterdeck import watch

    reading = write_bound.Reading('worker-1', [], None, {}, (write_bound.SEAT, 531 * MiB, 4 * write_bound.GiB,
                                                              'the walk passed 10 s on the clock'))
    assert write_bound.unmeasured(ship.runtime, reading) == 1
    assert write_bound.unmeasured(ship.runtime, reading) == 2
    words = ('worker-1: write bound unmeasured, 2 of 3 passes in a row (the walk passed 10 s on the clock); '
             'retried each pass, stopped at 3')
    facts = watch.facts(ship, tmux)
    assert facts.write_bounds == [words] and words in facts.line()  # law: nothing-waits-silently
    write_bound.measured(ship.runtime, 'worker-1')
    assert watch.facts(ship, tmux).write_bounds == []


# -- a jail that loads the machine itself ---------------------------------------------------------------------------


@pytest.fixture
def small(monkeypatch):
    monkeypatch.setattr(write_bound, 'BOUNDS', dict(SMALL))


@jailed
def test_a_jail_burning_cpu_while_it_writes_past_its_bound_is_stopped_by_the_alarm(ship, tmp_path, small):
    workspace = tmp_path / 'work'
    workspace.mkdir()
    service = type('Service', (), {})()
    service.ship, service.runtime = ship, ship.runtime
    service.logs, service.log = logger()
    jail = jailed_program(ship, workspace, BURNING)
    alarm = write_bound.Alarm(service).start()
    try:
        assert until(lambda: write_bound.path(ship.runtime, 'worker-1').exists(), 30), 'stopped'
        jail.wait(timeout=10)
    finally:
        alarm.close(timeout=5)
        ended(jail)
    assert jail.returncode == -9 and write_bound.jails(ship) == {}
    assert 'stopped at its write bound (workspace' in write_bound.reason(ship.runtime, 'worker-1')


@jailed
def test_a_walk_cut_short_counts_what_it_reached_and_stops_the_jail_at_once(ship, tmp_path, small, monkeypatch):
    workspace = tmp_path / 'work'
    workspace.mkdir()
    jail = jailed_program(ship, workspace, BURNING)
    logs, log = logger()
    try:
        assert until(lambda: (workspace / 'ready').exists(), 60)
        # The walk is starved in the jail's 'sub' folder, after it has counted the stream beside it.
        with starved(monkeypatch, lambda path: path.endswith('/work/sub'), seconds=1.5):
            stopped = write_bound.look(ship, log, budget=write_bound.Budget(wall=1.0))
        jail.wait(timeout=10)
    finally:
        ended(jail)
    assert stopped == ['worker-1'], 'over on its first pass, not unmeasured'
    data = next(data for event, data in logs if event == 'write-bound')
    assert data['group'] == write_bound.WORKSPACE and data['held'] > SMALL[write_bound.WORKSPACE]
    assert data['why'] == 'over its bound before the walk passed 1 s on the clock'
    assert write_bound.unmeasured_read(ship.runtime, 'worker-1') is None


@jailed
def test_three_unmeasured_passes_in_a_row_stop_a_jail_that_burns_cpu_and_writes(ship, tmp_path, small):
    workspace = tmp_path / 'work'
    workspace.mkdir()
    jail = jailed_program(ship, workspace, BURNING)
    logs, log = logger()
    cut = write_bound.Budget(wall=0.0)  # every walk is cut short by the clock before it reaches the stream
    try:
        assert until(lambda: (workspace / 'ready').exists(), 60)
        passes = [write_bound.look(ship, log, budget=cut) for _ in range(write_bound.PASSES)]
        jail.wait(timeout=10)
    finally:
        ended(jail)
    assert write_bound.PASSES == 3 and write_bound.WALL == 10.0, 'about 30 s unmeasured at most'
    assert passes == [[], [], ['worker-1']], 'up for two passes, stopped on the third'
    counted = [data['passes'] for event, data in logs if event == 'write-bound' and data.get('outcome') == 'unmeasured']
    assert counted == [1, 2, 3]
    stop = [data for event, data in logs if event == 'write-bound' and 'killed' in data]
    assert len(stop) == 1 and stop[0]['why'] == 'unmeasured 3 passes in a row, the walk passed 0 s on the clock'
    assert stop[0]['killed'] >= 1 and jail.returncode == -9
    assert 'unmeasured 3 passes in a row' in write_bound.reason(ship.runtime, 'worker-1')
    assert write_bound.unmeasured_read(ship.runtime, 'worker-1') is None, 'the stop ends the count; the hold stands'


@jailed
def test_a_measured_pass_starts_the_count_again_and_a_gone_jail_ends_it(ship, tmp_path, small):
    workspace = tmp_path / 'work'
    workspace.mkdir()
    jail = jailed_program(ship, workspace, IDLE)
    logs, log = logger()
    try:
        assert until(lambda: 'worker-1' in write_bound.jails(ship))
        for _ in range(write_bound.PASSES - 1):
            assert write_bound.look(ship, log, budget=write_bound.Budget(wall=0.0)) == []
        assert write_bound.unmeasured_read(ship.runtime, 'worker-1')['passes'] == write_bound.PASSES - 1
        assert write_bound.look(ship, log) == [], 'a measured pass'
        assert write_bound.unmeasured_read(ship.runtime, 'worker-1') is None
        for _ in range(write_bound.PASSES - 1):
            assert write_bound.look(ship, log, budget=write_bound.Budget(wall=0.0)) == [], 'counted from one again'
        assert jail.poll() is None
        ended(jail)
        assert until(lambda: write_bound.jails(ship) == {})
        assert write_bound.look(ship, log) == []
        assert write_bound.unmeasured_read(ship.runtime, 'worker-1') is None, 'no jail left to measure'
    finally:
        ended(jail)


# -- the hard bounds ----------------------------------------------------------------------------------------------------


@jailed
def test_a_tree_past_the_entry_bound_still_stops_the_jail_at_once_on_a_busy_machine(ship, tmp_path, small):
    workspace = tmp_path / 'work'
    workspace.mkdir()
    jail = jailed_program(ship, workspace, ENTRIES)
    logs, log = logger()
    try:
        assert until(lambda: len(os.listdir(workspace)) >= 3000, 60)
        with busy_machine():
            stopped = write_bound.look(ship, log, budget=write_bound.Budget(entries=1000))
        jail.wait(timeout=10)
    finally:
        ended(jail)
    assert stopped == ['worker-1'] and 'passed 1000 entries' in logs[0][1]['why'], 'on the first pass'
    assert jail.returncode == -9


def test_the_cpu_bound_reads_as_over_and_the_clock_as_unmeasured(tmp_path):
    for n in range(50):
        (tmp_path / f'f{n}').touch()
    group = write_bound.Group('test', [str(tmp_path)], 1 << 40)
    with pytest.raises(write_bound.Over, match='the walk passed 0 s of CPU time'):
        write_bound.walk(str(tmp_path), group, write_bound.Budget(seconds=0).start())
    with pytest.raises(write_bound.Unmeasured, match='the walk passed 0 s on the clock'):
        write_bound.walk(str(tmp_path), group, write_bound.Budget(wall=0).start())
    with pytest.raises(write_bound.Over, match='passed 0 entries'):  # the hard bounds are read before the clock
        write_bound.walk(str(tmp_path), group, write_bound.Budget(entries=0, wall=0).start())
    with pytest.raises(write_bound.Over, match='of CPU time'):
        write_bound.walk(str(tmp_path), group, write_bound.Budget(seconds=0, wall=0).start())


# -- one look over many seats ---------------------------------------------------------------------------------------


def fake_jails(ship, monkeypatch, names):
    """Jails as the alarm finds them, for seats with no process: each binds its own seat folder alone."""
    running = {}
    for name in names:
        folder = write_bound.seat_folder(ship, name)
        folder.mkdir(parents=True, exist_ok=True)
        running[name] = write_bound.Jail(name, 0, [str(folder)], {})
    monkeypatch.setattr(write_bound, 'jails', lambda ship, processes=None: dict(running))
    return running


def roomy(folder):
    return 300 * write_bound.GiB, 1000 * write_bound.GiB


def test_a_look_with_four_slow_seats_reaches_the_last_within_two_looks(ship, monkeypatch):
    assert write_bound.LOOK == 10.0 and write_bound.WALL == 10.0
    names = ['slow-a', 'slow-b', 'slow-c', 'slow-d']
    fake_jails(ship, monkeypatch, names)
    folders = {str(write_bound.seat_folder(ship, name)): name for name in names}
    scan, walked = write_bound._scan, []

    def slow(fd, device, group, budget):
        if (name := folders.get(os.readlink(f'/proc/self/fd/{fd}'))):
            walked.append(name)
            time.sleep(0.4)  # on the clock, not in CPU: each seat's walk takes 0.4 s of a look of 1 s
        return scan(fd, device, group, budget)

    monkeypatch.setattr(write_bound, '_scan', slow)
    state, (logs, log) = write_bound.State(), logger()
    assert write_bound.look(ship, log, state=state, space=roomy, seconds=1.0) == []
    assert walked == ['slow-a', 'slow-b', 'slow-c'] and state.unreached == ['slow-d']
    assert ('write-bound', {'outcome': 'unreached', 'seats': ['slow-d'], 'seconds': logs[-1][1]['seconds']}) in logs
    walked.clear()
    assert write_bound.look(ship, log, state=state, space=roomy, seconds=1.0) == []
    assert walked[0] == 'slow-d', 'the seat the last look did not reach goes first'
    assert walked == ['slow-d', 'slow-a', 'slow-b'] and state.unreached == ['slow-c']
    assert all(write_bound.unmeasured_read(ship.runtime, name) is None for name in names), 'none cut short'


# -- the free-space floor --------------------------------------------------------------------------------------------


def test_the_floor_is_the_smaller_of_fifty_gib_and_five_percent():
    GiB = write_bound.GiB
    assert write_bound.floor(7389 * GiB) == 50 * GiB
    assert write_bound.floor(int(44.9 * GiB)) == int(44.9 * GiB * 0.05)
    assert write_bound.FALL == 20 * GiB


def test_the_floor_stops_only_seats_unmeasured_or_grown_on_its_disk_and_names_why(ship, monkeypatch, tmux):
    from quarterdeck import watch

    GiB = write_bound.GiB
    running = fake_jails(ship, monkeypatch, ['steady', 'grower', 'cut'])
    folder = {name: write_bound.seat_folder(ship, name) for name in ['steady', 'grower', 'cut', 'fresh']}
    for name in running:
        with open(folder[name] / 'data', 'wb') as stream:
            os.posix_fallocate(stream.fileno(), 0, MiB)
    free = {'bytes': 300 * GiB}
    asked = []

    def space(path):
        asked.append(path)
        return free['bytes'], 1000 * GiB

    state, (logs, log) = write_bound.State(), logger()
    assert write_bound.look(ship, log, state=state, space=space) == [], 'a first look is every seat\'s baseline'
    assert len(asked) == 1, 'one statvfs for the one device, not one for each seat'

    # Between the looks 'grower' writes, 'fresh' starts, and 'cut' will be cut short by the clock; 30 GiB go.
    with open(folder['grower'] / 'more', 'wb') as stream:
        os.posix_fallocate(stream.fileno(), 0, MiB)
    folder['fresh'].mkdir(parents=True)
    running['fresh'] = write_bound.Jail('fresh', 0, [str(folder['fresh'])], {})
    free['bytes'] = 270 * GiB
    scan = write_bound._scan

    def cut(fd, device, group, budget):
        if os.readlink(f'/proc/self/fd/{fd}') == str(folder['cut']):
            raise write_bound.Unmeasured('the walk passed 10 s on the clock')
        return scan(fd, device, group, budget)

    monkeypatch.setattr(write_bound, '_scan', cut)
    assert write_bound.look(ship, log, state=state, space=space) == ['cut', 'grower']
    monkeypatch.setattr(write_bound, '_scan', scan)
    fell = f"{folder['cut']}: free space fell 30.00 GiB since the last look, more than 20.00 GiB"
    assert write_bound.reason(ship.runtime, 'cut') == (
        f"stopped by its disk's free-space floor ({fell}; this seat went unmeasured there); held: qd up cut lifts it")
    assert write_bound.reason(ship.runtime, 'grower') == (
        f"stopped by its disk's free-space floor ({fell}; this seat grew 1.0 MiB there since its last measure); "
        f"held: qd up grower lifts it")
    assert write_bound.reason(ship.runtime, 'steady') is None, 'measured whole and not grown: it stays up'
    assert write_bound.reason(ship.runtime, 'fresh') is None, 'its first measure is its baseline'
    assert write_bound.unmeasured_read(ship.runtime, 'cut') is None, 'the stop ends its count'
    stops = [data for event, data in logs if event == 'write-bound' and data.get('floor')]
    assert [data['seat'] for data in stops] == ['cut', 'grower'] and all(data['why'].startswith(fell) for data in stops)
    line = watch.facts(ship, tmux).line()
    assert "cut: stopped by its disk's free-space floor" in line and "grower: stopped by its disk's" in line

    # Under the floor itself, the seats left are judged the same way: unchanged stays up, grown is stopped.
    del running['cut'], running['grower']
    free['bytes'] = 40 * GiB
    assert write_bound.look(ship, log, state=state, space=space) == []
    with open(folder['fresh'] / 'more', 'wb') as stream:
        os.posix_fallocate(stream.fileno(), 0, MiB)
    assert write_bound.look(ship, log, state=state, space=space) == ['fresh']
    assert ('40.00 GiB free, under its floor of 50.00 GiB; this seat grew 1.0 MiB there'
            in write_bound.reason(ship.runtime, 'fresh'))
    assert write_bound.reason(ship.runtime, 'steady') is None
    assert write_bound.lift(ship.runtime, 'fresh', 'the test') and write_bound.reason(ship.runtime, 'fresh') is None


def test_the_floor_judges_a_seat_only_on_the_disk_it_went_unmeasured_on(ship, monkeypatch):
    """An endpoint seat cut short in its seat folder is not stopped for the floor of /tmp, where its relay folder,
    in the small group and walked first, was measured whole and has not grown."""
    import shutil
    from quarterdeck import endpoint_relay

    GiB = write_bound.GiB
    running = fake_jails(ship, monkeypatch, ['worker-1'])
    seat = write_bound.seat_folder(ship, 'worker-1')
    relay = endpoint_relay.folder_for(seat)
    if os.stat(relay.parent).st_dev == os.stat(seat).st_dev:
        pytest.skip('the relay folder and the seat folder share a device here')
    relay.mkdir()
    try:
        running['worker-1'].binds.append(str(relay))
        low = os.stat(relay).st_dev

        def space(path):
            return (1 * GiB, 44 * GiB) if os.stat(path).st_dev == low else (300 * GiB, 1000 * GiB)

        scan = write_bound._scan

        def cut(fd, device, group, budget):
            if os.readlink(f'/proc/self/fd/{fd}') == str(seat):
                raise write_bound.Unmeasured('the walk passed 10 s on the clock')
            return scan(fd, device, group, budget)

        logs, log = logger()
        state = write_bound.State()
        assert write_bound.look(ship, log, state=state, space=space) == [], 'the baseline'
        monkeypatch.setattr(write_bound, '_scan', cut)
        assert write_bound.look(ship, log, state=state, space=space) == [], 'cut on the roomy disk, whole on the low one'
        assert write_bound.unmeasured_read(ship.runtime, 'worker-1')['passes'] == 1
        assert write_bound.reason(ship.runtime, 'worker-1') is None
        (relay / 'grown').write_bytes(b'x' * 65536)
        assert write_bound.look(ship, log, state=state, space=space) == ['worker-1'], 'grown on the low disk'
        assert 'this seat grew' in write_bound.reason(ship.runtime, 'worker-1')
    finally:
        shutil.rmtree(relay)


@jailed
def test_a_jail_that_burns_cpu_and_writes_with_every_walk_cut_is_stopped_by_the_floor(ship, tmp_path, small):
    workspace = tmp_path / 'work'
    workspace.mkdir()
    jail = jailed_program(ship, workspace, BURNING)
    logs, log = logger()
    low = lambda folder: (40 * write_bound.GiB, 1000 * write_bound.GiB)  # noqa: E731
    try:
        assert until(lambda: (workspace / 'ready').exists(), 60)
        stopped = write_bound.look(ship, log, budget=write_bound.Budget(wall=0.0), space=low)
        jail.wait(timeout=10)
    finally:
        ended(jail)
    assert stopped == ['worker-1'], 'on its first look, by the floor, though its walk never measured it'
    assert jail.returncode == -9 and write_bound.jails(ship) == {}
    words = write_bound.reason(ship.runtime, 'worker-1')
    assert words.startswith("stopped by its disk's free-space floor (") and 'this seat went unmeasured there' in words


def test_the_service_tells_the_desk_of_a_floor_hold_and_not_of_a_bound_hold(ship):
    from quarterdeck import alarms, post, service as servicemod

    actor = servicemod.Service.__new__(servicemod.Service)
    actor.runtime, actor.ship, actor.store = ship.runtime, ship, post.Store(ship.runtime)
    actor.logs, actor.log = logger()
    actor._up = lambda name: False
    bound = write_bound.Reading('captain', [], (write_bound.SEAT, 5 * write_bound.GiB, 4 * write_bound.GiB,
                                                'over its bound'), {})
    write_bound.place(ship.runtime, bound)
    why = '/x: 40.00 GiB free, under its floor of 50.00 GiB; this seat went unmeasured there'
    write_bound.place(ship.runtime, write_bound.Reading('worker-1', [], ('free space', 40, 50, why), {}), floor=True)
    actor.write_bound_floor()
    sent = actor.store.all()
    assert [(message.to, message.sender) for message in sent] == [('desk', post.SHIP)]
    assert sent[0].body == f"worker-1: stopped by its disk's free-space floor ({why}); held: qd up worker-1 lifts it"
    actor.write_bound_floor()
    assert len(actor.store.all()) == 1, 'told once while the hold stands'
    write_bound.lift(ship.runtime, 'worker-1', 'the test')
    actor.write_bound_floor()
    assert alarms.load(ship.runtime, 'worker-1', 'write-bound-floor')['key'] is None, 'the lift clears it'
    assert actor.logs == []
