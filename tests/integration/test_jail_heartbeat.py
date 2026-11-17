"""A jailed seat sees the service's heartbeat as it is.

The service replaces its heartbeat whole every 2 s (a temporary file, then a rename). A single-file bind pins the inode
a jail started with, so a jail would read its launch-time beat forever, and a replace during bwrap's mount of that one
file breaks the launch. The heartbeat lies in a folder of its own, which the jail shows read-only: a replace inside it
never races a mount and is always seen. Real bwrap on the drydock; the service is played by the test's own writes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from quarterdeck import sandbox, servicectl, ship as shipmod

from .test_sandbox import namespaces_allowed

jailed = pytest.mark.skipif(not namespaces_allowed(), reason='bubblewrap cannot make a namespace here')
# Inside the jail, with the code under test: every reading of the heartbeat, as servicectl reads it, while it changes,
# and the jailed check's own answer at the end. argv: the source, the runtime, the seconds to watch.
PROBE = r"""
import json, sys, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from quarterdeck import servicectl
runtime, end = Path(sys.argv[2]), time.monotonic() + float(sys.argv[3])
seen = []
print('ready', flush=True)
while time.monotonic() < end:
    try:
        text = servicectl.heartbeat_path(runtime).read_text()
    except OSError as error:
        text = type(error).__name__
    if not seen or seen[-1] != text:
        seen.append(text)
    time.sleep(0.05)
print(json.dumps(dict(seen=seen, locked=servicectl.locked(runtime))), flush=True)
"""


@pytest.fixture
def ship(drydock: Path) -> shipmod.Ship:
    ship = shipmod.load(drydock)
    shipmod.fence(ship)
    return ship


def jail_of(ship, tmp_path: Path):
    own = ship.runtime / 'seats' / 'worker-1'
    (own / 'home').mkdir(parents=True, exist_ok=True)
    workspace = tmp_path / 'work'
    workspace.mkdir(exist_ok=True)
    return sandbox.plan(ship, 'worker-1', own, own / 'home', workspace, sys.executable, '/usr/bin:/bin',
                        home=Path.home())


def source() -> str:
    import quarterdeck
    return str(Path(quarterdeck.__file__).resolve().parent.parent)


def beat(runtime: Path, at: datetime) -> str:
    """One heartbeat as the service writes it (servicectl.write_heartbeat), at a time of the test's choosing."""
    text = f'{os.getpid()} {at.isoformat()}\n'
    path = servicectl.heartbeat_path(runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(text)
    os.replace(tmp, path)
    return text


def watched(ship, tmp_path: Path, seconds: float, writes) -> dict:
    """Start the probe in a jail, call ``writes`` once it reads, and return what it saw."""
    jail = jail_of(ship, tmp_path)
    probe = subprocess.Popen(sandbox.command(jail, [sys.executable, '-I', '-c', PROBE, source(), str(ship.runtime),
                                                    str(seconds)]),
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert probe.stdout.readline().strip() == 'ready', probe.stderr.read()
        writes()
        out, err = probe.communicate(timeout=seconds + 30)
    finally:
        if probe.poll() is None:
            probe.kill()
            probe.wait()
    assert probe.returncode == 0, err
    return json.loads(out.strip().splitlines()[-1])


@jailed
def test_a_jail_sees_the_heartbeat_advance_across_replaces(ship, tmp_path):
    now = datetime.now(timezone.utc)
    first = beat(ship.runtime, now)
    written = [first]

    def writes():
        for n in range(1, 8):
            time.sleep(0.3)
            written.append(beat(ship.runtime, now + timedelta(seconds=n)))

    seen = watched(ship, tmp_path, 4.0, writes)['seen']
    assert seen[0] == first and seen[-1] == written[-1], f'the jail saw {seen}; the service wrote {written}'
    assert len(seen) >= 4 and set(seen) <= set(written), 'it followed the replaces, and saw nothing torn'


@jailed
def test_the_jailed_check_reads_a_live_service_as_up_after_120_s(ship, tmp_path):
    """The beat the jail launched with is past the jailed bound (as it is 120 s after a launch); the live service
    then ticks. The jail, which cannot see the lock, reads the service as up by the new beat."""
    assert servicectl.JAILED_HEARTBEAT_SECONDS == 120.0
    beat(ship.runtime, datetime.now(timezone.utc) - timedelta(seconds=200))

    def writes():
        time.sleep(0.2)
        beat(ship.runtime, datetime.now(timezone.utc))

    reading = watched(ship, tmp_path, 1.5, writes)
    assert len(reading['seen']) == 2 and reading['locked'] is True, reading


@jailed
def test_a_launch_during_a_burst_of_replaces_never_fails_on_the_mount(ship, tmp_path):
    jail = jail_of(ship, tmp_path)
    beat(ship.runtime, datetime.now(timezone.utc))
    stop, replaced = threading.Event(), [0]

    def burst():
        while not stop.is_set():
            beat(ship.runtime, datetime.now(timezone.utc))
            replaced[0] += 1

    writer = threading.Thread(target=burst, daemon=True)
    writer.start()
    failures = []
    try:
        for _ in range(40):
            done = subprocess.run(sandbox.command(jail, ['/bin/true']), stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=60)
            if done.returncode != 0:
                failures.append(done.stderr.strip())
    finally:
        stop.set()
        writer.join(timeout=10)
    assert replaced[0] > 1000, 'the heartbeat was replaced throughout'
    assert failures == [], failures[:3]


def test_the_heartbeat_has_a_folder_of_its_own_that_the_jail_shows():
    runtime = Path('/synthetic/runtime')
    assert servicectl.heartbeat_path(runtime) == runtime / 'heartbeat' / 'service.heartbeat'
    assert 'heartbeat' in sandbox.RUNTIME_SHOWN and 'service.heartbeat' not in sandbox.RUNTIME_SHOWN


def beat_at(path: Path, pid: int, age: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'{pid} {(datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()}\n')


def test_only_the_folder_is_read_and_the_earlier_place_never_written(tmp_path):
    """The beat lies in its folder alone. A beat the copy that moved it (11ca0ea6) left at runtime/service.heartbeat is
    never read, so a dead pid there names no service, and nothing of this code writes it."""
    from quarterdeck import service as servicemod

    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    new, old = servicectl.heartbeat_path(runtime), runtime / servicectl.EARLIER_HEARTBEAT
    assert servicectl.read_heartbeat(runtime) is None
    beat_at(old, 4107, 0)
    assert servicectl.read_heartbeat(runtime) is None, 'the earlier place is no beat'
    beat_at(new, 4108, 300)
    assert servicectl.read_heartbeat(runtime)[0] == 4108
    new.write_text('torn')
    assert servicectl.read_heartbeat(runtime) is None, 'an unreadable beat is no beat'
    old.unlink()
    svc = servicemod.Service.__new__(servicemod.Service)
    svc.runtime, svc.heartbeat_lock = runtime, threading.Lock()
    svc.beat()
    assert new.read_text().split()[0] == str(os.getpid())
    assert not old.exists() and not old.with_suffix('.tmp').exists(), 'no beat at the earlier place'


def test_a_service_clears_a_beat_it_did_not_write_at_its_start_and_a_stop_clears_both(tmp_path):
    """A service that ended without clearing its beat left a dead pid in runtime/heartbeat/, and the copy that moved
    the beat left one at runtime/service.heartbeat; the next service of this code removes both and beats at once."""
    from quarterdeck import service as servicemod

    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    new, old = servicectl.heartbeat_path(runtime), runtime / servicectl.EARLIER_HEARTBEAT
    beat_at(new, 4108, 900)
    (new.parent / 'service.tmp').write_text('left')
    beat_at(old, 4107, 1)
    svc = servicemod.Service.__new__(servicemod.Service)
    svc.runtime, svc.heartbeat_lock = runtime, threading.Lock()
    svc.first_beat()
    pid, stamp = new.read_text().split()
    assert pid == str(os.getpid()) and datetime.now(timezone.utc) - datetime.fromisoformat(stamp) < timedelta(seconds=30)
    assert not old.exists(), 'the earlier place is cleared, never beaten'
    assert sorted(p.name for p in new.parent.iterdir()) == ['service.heartbeat', 'service.pass'], \
        "nothing but the service's two beats in the jail-shown folder"
    # The lock exists and nobody holds it: no service runs (without it, a fresh beat reads as one, as in a jail).
    servicectl.lock_path(runtime).parent.mkdir(parents=True, exist_ok=True)
    servicectl.lock_path(runtime).touch()
    assert not servicectl.alive(runtime)
    beat_at(old, 4107, 1)
    assert servicectl.stop(runtime) == 'service was not running'
    assert not new.exists() and not old.exists()
