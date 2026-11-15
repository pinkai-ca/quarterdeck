"""The ship services a run's tests start, stopped before anything of the run is deleted.

A ship's service runs in a systemd scope of its own and a session of its own (servicectl.start), so nothing a test
leaves running is in the run's process group or its tmux sessions. A test that ends without stopping its service,
whether it fails, is interrupted, or its teardown signals without waiting, leaves it running; once the run deleted
the ship's folder, such a service went on writing, made the folders again and spun on errors for hours.

So the run gives every test the folder ``results/<run>/services``, and servicectl records there each service it
starts: its ship, its runtime, its pid and start, its scope, and the interpreter and package that started it. At the
run's end, and at any retirement, every service recorded there, and every process of this user's whose command line
runs a ship in the run's own scratch (``--ship <folder> service``, for one a test started with that variable cleared),
is stopped before anything is deleted: first by the ship's own code while the ship exists (servicectl.stop, through
the interpreter and package that started it), then by pid and start, with its whole tree. A service that still runs
then holds the cleanup: nothing is removed and the outcome is never ``complete``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

from . import procs

RECORD_ENV = 'TESTRUN_SERVICE_RECORDS'
STOP_SECONDS = 60  # the ship's own stop: its ordinary grace, then its kill, as servicectl.stop waits
OWN_STOP = ('import sys\nfrom pathlib import Path\nfrom quarterdeck import servicectl\n'
            'print(servicectl.stop(Path(sys.argv[1])))\n')


def records(folder: Path) -> list[dict]:
    """The services the run's tests recorded; a record that cannot be read is named, never skipped silently."""
    found = []
    try:
        names = sorted(os.listdir(folder))
    except FileNotFoundError:
        return []
    for name in names:
        if not name.endswith('.json'):
            continue
        try:
            data = json.loads((folder / name).read_text())
        except (OSError, ValueError):
            found.append({'record': name, 'unreadable': True})
            continue
        if isinstance(data, dict):
            found.append({**data, 'record': name})
    return found


def _ship_of(argv: list[str]) -> str | None:
    """The ship a qd service command line runs: ``... --ship <folder> service``."""
    if 'service' not in argv or '--ship' not in argv:
        return None
    at = argv.index('--ship')
    if at + 1 >= len(argv) or 'service' not in argv[at + 2:]:
        return None
    return argv[at + 1]


def running_in(scratch: Path) -> list[dict]:
    """This user's processes running a ship service whose ship lies in ``scratch``: the run's own, by its path."""
    scratch = Path(os.path.realpath(scratch))
    found = []
    for pid in procs._ours():
        if pid == os.getpid():
            continue
        try:
            raw = Path(f'/proc/{pid}/cmdline').read_bytes()
        except OSError:
            continue
        ship = _ship_of([part.decode(errors='replace') for part in raw.split(b'\0')[:-1]])
        if ship is None:
            continue
        where = Path(os.path.realpath(ship))
        start = procs.start_time(pid)
        if start is not None and (where == scratch or scratch in where.parents):
            found.append({'pid': pid, 'start': start, 'ship': ship, 'found_by': 'its command line'})
    return found


def _own_stop(service: dict) -> str | None:
    """The ship's own stop, by the interpreter and package that started the service, while the ship still exists."""
    runtime, python, package = service.get('runtime'), service.get('python'), service.get('package')
    if not (runtime and python and package) or not Path(service.get('ship', '')).exists():
        return None
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': os.environ.get('HOME', '/'),
           'PYTHONPATH': package, 'QD_SHIP': service.get('ship', '')}
    try:
        done = subprocess.run([python, '-c', OWN_STOP, runtime], env=env, capture_output=True, text=True,
                              timeout=STOP_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as error:
        return f'its own stop did not finish: {type(error).__name__}'
    return (done.stdout.strip() or done.stderr.strip()[-200:] or f'exit {done.returncode}')[:200]


def stop(folder: Path | None, scratch: Path) -> dict:
    """Stop every service of the run, recorded or found by its ship's path, and say what still runs."""
    seen, services = set(), []
    for service in [*(records(folder) if folder is not None else []), *running_in(scratch)]:
        if service.get('unreadable'):
            services.append(service)
            continue
        key = (service.get('pid'), service.get('start'))
        if key in seen:
            continue
        seen.add(key)
        services.append(service)
    entries, held = [], False
    for service in services:
        if service.get('unreadable'):
            entries.append({'record': service['record'], 'outcome': 'held', 'reason': 'service record unreadable'})
            held = True
            continue
        pid, start = service.get('pid'), service.get('start')
        entry = {key: service.get(key) for key in ('pid', 'start', 'ship', 'unit', 'record', 'found_by')
                 if service.get(key) is not None}
        if not isinstance(pid, int) or not isinstance(start, str):
            entries.append({**entry, 'outcome': 'ended', 'reason': 'recorded without a live identity'})
            continue
        if not procs.alive({pid: start}):
            entries.append({**entry, 'outcome': 'ended'})
            continue
        entry['own_stop'] = _own_stop(service)
        left = procs.alive({pid: start})
        if left:
            left = procs.end_tree(pid, start)
            entry['killed'] = True
        if left:
            held = True
            entries.append({**entry, 'outcome': 'held', 'left': sorted(left)})
        else:
            entries.append({**entry, 'outcome': 'stopped'})
    return {'outcome': 'held' if held else ('stopped' if any(e['outcome'] == 'stopped' for e in entries) else 'none'),
            'entries': entries}
