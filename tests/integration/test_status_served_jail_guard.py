"""A status call from inside a jail is served only the cold reading.

'qd --json status' writes its demand stamp in runtime/status and serves the service's reading only from there.
Inside a jail the runtime is a fenced, read-only folder that shows no runtime/status: the stamp cannot be written, so
the call takes the cold reader, labelled cold, and never tries the served file. A jailed seat cannot plant a reading
there either. Real bubblewrap, with the code under test's own qd inside the seat's jail.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from quarterdeck import presentation, sandbox, servicectl, ship as shipmod, status_snapshot

from .test_jail_runtime_claude_gates import jailed_qd, namespaces_allowed

MARKER = 'synthetic-served-reading-never-inside'

PROBE = r'''
import json, os, sys
root, served, demand = sys.argv[1:4]
seen = {}
try:
    seen['root_listing'] = sorted(os.listdir(root))
except OSError as error:
    seen['root_listing'] = type(error).__name__
for name, path in (('served', served), ('demand', demand)):
    try:
        with open(path, 'rb') as stream:
            seen[name] = 'read ' + str(len(stream.read()))
    except OSError as error:
        seen[name] = type(error).__name__
print(json.dumps(seen))
'''


def files_now(root: Path) -> dict:
    return {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}


@pytest.mark.skipif(not namespaces_allowed(), reason='bubblewrap is not installed, or cannot make a namespace here')
def test_a_jailed_status_call_gets_the_cold_reading_and_never_the_served_one(drydock: Path, tmp_path):
    ship = shipmod.load(drydock)
    shipmod.fence(ship)
    root = status_snapshot.path(ship.runtime).parent  # runtime/status
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    served, demand = status_snapshot.path(ship.runtime), status_snapshot.path(ship.runtime, status_snapshot.DEMAND)
    # A reading in the real served place, fresh by its clocks, carrying a marker no cold reading holds.
    served.write_text(json.dumps(dict(format=1, wall=time.time_ns(), monotonic=time.monotonic_ns(), pid=os.getpid(),
                                      reading=dict(service='ok', marker=MARKER))) + '\n')
    demand.unlink(missing_ok=True)
    before = files_now(root)

    jail, run = jailed_qd(ship, 'worker-1', tmp_path)
    done = run('--json', 'status')
    assert done.returncode == 0, (done.returncode, done.stderr[-800:])
    cold = json.loads(done.stdout)
    assert cold['reading']['mode'] == 'cold', cold.get('reading')
    assert cold['reading']['label'].startswith('cold: independent reading at '), cold['reading']
    assert MARKER not in done.stdout and MARKER not in done.stderr, 'nothing of the served file reached the call'
    assert not demand.exists(), 'the jailed call wrote no demand stamp'
    assert files_now(root) == before

    # Nothing of runtime/status is there inside the jail: no folder, no served file, no stamp.
    probe = subprocess.run(sandbox.command(jail, [sys.executable, '-I', '-c', PROBE, str(root), str(served), str(demand)]),
                           capture_output=True, text=True, timeout=60)
    assert probe.returncode == 0, probe.stderr[-800:]
    seen = json.loads(probe.stdout)
    assert seen['root_listing'] in ([], 'FileNotFoundError'), seen
    assert seen['served'] == 'FileNotFoundError' and seen['demand'] == 'FileNotFoundError', seen


@pytest.mark.skipif(not namespaces_allowed(), reason='bubblewrap is not installed, or cannot make a namespace here')
def test_a_jail_cannot_plant_a_reading_where_status_would_serve_it(drydock: Path, tmp_path):
    """A reading that is fresh, complete and names a live heartbeat, this ship and this framework: the jail cannot put
    it in runtime/status, which its read-only runtime does not show, and its status call stays cold. The same
    reading, put in the real place while the lock is held, is served outside the jail: what keeps it from the
    jailed call is the jail's view, not the reading."""
    ship = shipmod.load(drydock)
    shipmod.fence(ship)
    root = status_snapshot.path(ship.runtime).parent  # runtime/status
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    jail, run = jailed_qd(ship, 'worker-1', tmp_path)
    shape = json.loads(run('--json', 'status').stdout)  # a complete reading's shape, from a cold reading
    value = {key: item for key, item in shape.items() if key != 'reading'}
    value['service'] = 'ok'
    value['facts']['service'] = 'ok'
    value['marker'] = MARKER
    assert status_snapshot._complete(value), 'the planted reading is complete: only the service checks can refuse it'
    beat = servicectl.heartbeat_path(ship.runtime)
    beat.parent.mkdir(parents=True, exist_ok=True)
    beat.write_text(f'{os.getpid()} {datetime.now(timezone.utc).isoformat()}\n')  # a live pid, a fresh beat
    forged = tmp_path / 'worker-1-work' / 'forged.json'
    forged.write_text(json.dumps(dict(format=1, wall=time.time_ns(), monotonic=time.monotonic_ns(), pid=os.getpid(),
                                      process_start=servicectl.process_start(os.getpid()),
                                      ship=presentation.fingerprint(ship.file),
                                      framework=presentation.framework_stamp(), reading=value)) + '\n')
    served = status_snapshot.path(ship.runtime)
    served.unlink(missing_ok=True)
    assert servicectl.read_heartbeat(ship.runtime)[0] == os.getpid()
    import quarterdeck
    source = Path(quarterdeck.__file__).resolve().parent.parent
    entry = 'import sys; sys.path.insert(0, sys.argv.pop(1)); from quarterdeck.cli import main; sys.exit(main(sys.argv[1:]))'
    plant_then_ask = (f'if mkdir -p {root} 2>/dev/null && cp {forged} {served} 2>/dev/null; then echo planted >&2; '
                      f'else echo refused >&2; fi; exec "$@"')
    env = {key: item for key, item in os.environ.items() if not key.startswith(('QD_', 'TMUX'))}
    env.update(QD_SHIP=str(ship.folder), QD_SEAT='worker-1', PATH='/usr/bin:/bin')
    done = subprocess.run(sandbox.command(jail, ['/bin/sh', '-c', plant_then_ask, 'sh', sys.executable, '-I', '-c',
                                                 entry, str(source), '--json', 'status']),
                          capture_output=True, text=True, env=env, timeout=90)
    assert done.returncode == 0, (done.returncode, done.stderr[-800:])
    assert done.stderr.splitlines()[0] == 'refused', 'the jail cannot write runtime/status'
    reading = json.loads(done.stdout)
    assert reading['reading']['mode'] == 'cold', reading.get('reading')
    assert MARKER not in done.stdout, 'no planted reading was served'
    assert not served.exists(), 'nothing reached the real place'

    # The same reading, put in the real place while this process holds the lock, is served outside the jail: what
    # keeps it from the jailed call is the jail's view, not the reading. Its beat and times are stamped afresh here,
    # so the proof never depends on how long the jailed run took.
    held = servicectl.acquire_lock(ship.runtime)
    try:
        beat.write_text(f'{os.getpid()} {datetime.now(timezone.utc).isoformat()}\n')
        data = json.loads(forged.read_text())
        data.update(wall=time.time_ns(), monotonic=time.monotonic_ns())
        served.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        served.write_text(json.dumps(data) + '\n')
        outside = status_snapshot.served(ship)
        assert outside is not None and outside['reading']['mode'] == 'served' and outside['marker'] == MARKER
    finally:
        held.close()
        served.unlink(missing_ok=True)
