"""A jail past its bound is killed first and its hold recorded after: a hold that cannot be written (a full disk) never
leaves the jail running. Stand-in jails only: a throwaway process tree of the test's own, in the test's own runtime."""
from __future__ import annotations

from datetime import datetime, timezone
import errno
import json
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from quarterdeck import post_dir, relaunch_intent, trusted_files, watch, write_bound

SEAT = 'synthetic-jailed'
TREE = 'import subprocess, sys, time; subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"]); time.sleep(120)'


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(write_bound, '_UNRECORDED', {}, raising=False)
    ship = SimpleNamespace(runtime=tmp_path / 'runtime')
    ship.runtime.mkdir()
    logs = []
    started = []

    def jail():
        """A stand-in jail: a process and its child, each known by its pid and start."""
        root = subprocess.Popen([sys.executable, '-c', TREE], stdin=subprocess.DEVNULL)
        started.append(root)
        for _ in range(100):
            processes = write_bound._processes()
            pids = write_bound.tree(root.pid, processes.get(root.pid, (0, None))[1], processes)
            if len(pids) >= 2:
                return pids
            time.sleep(0.05)
        raise AssertionError('the stand-in jail did not start its child')

    yield SimpleNamespace(ship=ship, logs=logs, log=lambda event, **data: logs.append((event, data)), jail=jail)
    for root in started:  # the test ends whatever is left of its own trees
        try:
            for pid in write_bound.alive(write_bound.tree(root.pid, write_bound._processes().get(root.pid, (0, None))[1])):
                write_bound.kill({pid: write_bound._processes()[pid][1]})
        except KeyError:
            pass
        root.kill()
        root.wait()


def leave_intent(runtime):
    folder = relaunch_intent.folder(runtime)
    folder.mkdir(parents=True, exist_ok=True)
    data = dict(version=1, seat=SEAT, kind='after-order', resume=False, at=datetime.now(timezone.utc).isoformat(),
                phase=relaunch_intent.RELAUNCHING, before=None,
                owner=dict(version=1, pid=999999999, start='synthetic-start-zzzz', token='synthetic-owner-zzzz',
                           device=1, inode=1))
    relaunch_intent.path(runtime, SEAT).write_text(json.dumps(data) + '\n')


def over(pids):
    return write_bound.Reading(SEAT, [], (write_bound.WORKSPACE, 3 * write_bound.GiB, 2 * write_bound.GiB,
                                          'over its bound'), pids)


def full_disk(monkeypatch):
    original = post_dir.replace_bytes

    def refuse(target, data, *args, **kwargs):
        if target == write_bound.path(target.parents[2], SEAT) or target.name == f'{SEAT}.json':
            raise OSError(errno.ENOSPC, 'No space left on device')
        return original(target, data, *args, **kwargs)

    monkeypatch.setattr(post_dir, 'replace_bytes', refuse)
    return original


def test_a_hold_that_cannot_be_written_still_ends_the_jail_and_is_named(world, monkeypatch):
    pids = world.jail()
    leave_intent(world.ship.runtime)
    full_disk(monkeypatch)
    write_bound.stop(world.ship, over(pids), world.log)  # never raised out of the stop
    for _ in range(40):
        if not write_bound.alive(pids):
            break
        time.sleep(0.05)
    assert write_bound.alive(pids) == {}, 'the kill stands though the hold could not be written'
    assert write_bound.read(world.ship.runtime, SEAT) is None
    assert relaunch_intent.path(world.ship.runtime, SEAT).exists(), 'the intent kept where it was'
    [line] = write_bound.status(world.ship)
    assert line == (f'{SEAT}: jail stopped at its write bound (workspace 3.00 GiB of 2.00 GiB), its hold unrecorded '
                    '(OSError: No space left on device); written at the next look, the seat kept down meanwhile')
    assert line in watch.Facts('up', 0, 0, 0, write_bounds=write_bound.status(world.ship)).line()
    assert write_bound._held(SimpleNamespace(name=SEAT, ship=world.ship)) == line.split(': ', 1)[1], 'launches refuse'
    assert ('error', dict(step='write-bound', seat=SEAT, error='hold unrecorded: OSError: No space left on device')) \
        in world.logs
    [stopped] = [data for event, data in world.logs if event == 'write-bound']
    assert stopped['recorded'] is False and stopped['killed'] >= 2


def test_a_later_look_records_the_kept_hold_and_kills_nothing_new(world, monkeypatch):
    pids = world.jail()
    leave_intent(world.ship.runtime)
    original = full_disk(monkeypatch)
    write_bound.stop(world.ship, over(pids), world.log)
    monkeypatch.setattr(post_dir, 'replace_bytes', original)  # the disk has room again
    newer = world.jail()  # a new jail of the same seat, which nothing may kill by mistake
    monkeypatch.setattr(write_bound, 'jails', lambda ship: {})
    monkeypatch.setattr(write_bound, 'floor_look', lambda *args, **kwargs: [])
    write_bound.look(world.ship, world.log)
    assert write_bound.alive(newer) == newer, 'nothing new is killed'
    assert write_bound.read(world.ship.runtime, SEAT)['group'] == write_bound.WORKSPACE, 'recorded from the kept hold'
    assert not relaunch_intent.path(world.ship.runtime, SEAT).exists(), 'its intent cleared once the hold is written'
    assert write_bound.unrecorded() == {}
    assert write_bound.status(world.ship) == [
        f'{SEAT}: stopped at its write bound (workspace 3.00 GiB of 2.00 GiB); held: qd up {SEAT} lifts it']
    assert any(data.get('outcome') == 'hold recorded after its stop' for event, data in world.logs)


def test_a_seat_launched_since_its_stop_drops_the_unrecorded_hold(world, monkeypatch):
    pids = world.jail()
    original = full_disk(monkeypatch)
    write_bound.stop(world.ship, over(pids), world.log)
    monkeypatch.setattr(post_dir, 'replace_bytes', original)
    launch = trusted_files.path(world.ship.runtime, SEAT, trusted_files.LAUNCH)
    launch.parent.mkdir(parents=True, exist_ok=True)
    launch.write_text(json.dumps(dict(at=datetime.now(timezone.utc).isoformat())))  # a person's qd up since
    assert write_bound.finish_unrecorded(world.ship, world.log) == []
    assert write_bound.read(world.ship.runtime, SEAT) is None and write_bound.unrecorded() == {}
    assert write_bound.status(world.ship) == []
