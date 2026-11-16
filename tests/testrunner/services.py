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

The tmux servers the run's tests left are ended there too, after the services: every server of this user whose own
environment puts TMPDIR in the run's scratch, through its own socket only (see ``end_server``).
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


def inside(ship, scratch: Path) -> bool:
    """Whether a ship's folder lies in the run's own scratch: the one test for what this run may stop."""
    if not isinstance(ship, str) or not ship:
        return False
    where, scratch = Path(os.path.realpath(ship)), Path(os.path.realpath(scratch))
    return where == scratch or scratch in where.parents


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
        start = procs.start_time(pid)
        if start is not None and inside(ship, scratch):
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


# -- the tmux servers a run's tests start ---------------------------------------------------------------------------
#
# A private tmux server forks into a session of its own, so no test's ending and no group stop reaches it; a test
# interrupted before its finally leaves it running for good, a stopped one stopped for good. A server started by
# anything inside the run inherits the run's TMPDIR, which lies in the run's scratch: that, read from the server's own
# environment, is the proof that it is the run's. The scratch alone, never the checkout (the shared tree, under which
# a ship's runtime and its server lie). Whatever the environment says, no socket of a ship's runtime and no default
# socket is ever ended (excluded_socket).

SERVER_SECONDS = 5  # the bound on one tmux command to a server, and the failsafe on waiting for it to end


def excluded_socket(socket: str) -> bool:
    """A socket no run ever ends: one under any ship's runtime (*/ships/*/runtime/tmux/), or this user's default
    socket (tmux-<uid>/default, under any TMUX_TMPDIR or /tmp). Judged on the path as given and as it really lies."""
    for path in {Path(socket), Path(os.path.realpath(socket))}:
        parts = path.parts
        if any(parts[at] == 'ships' and parts[at + 2:at + 4] == ('runtime', 'tmux') for at in range(len(parts) - 3)):
            return True
        if path.name == 'default' and path.parent.name == f'tmux-{os.getuid()}':
            return True
    return False


def _environment(pid: int) -> dict[str, str] | None:
    try:
        raw = Path(f'/proc/{pid}/environ').read_bytes()
    except OSError:
        return None
    return {key.decode(errors='replace'): value.decode(errors='replace')
            for key, _, value in (item.partition(b'=') for item in raw.split(b'\0') if b'=' in item)}


def socket_of(argv: list[str], environment: dict[str, str]) -> str | None:
    """The socket a server listens on: its first client's '-S <path>', which the server keeps as its own argv; else
    TMUX_TMPDIR/tmux-<uid>/<-L name, or 'default'>. None when it cannot be named as an absolute path."""
    def option(flag):
        for at, part in enumerate(argv):
            if part == flag:
                return argv[at + 1] if at + 1 < len(argv) else None
            if part.startswith(flag) and len(part) > len(flag) and not part.startswith('--'):
                return part[len(flag):]
        return None
    socket = option('-S')
    if socket is None:
        folder = environment.get('TMUX_TMPDIR') or '/tmp'
        socket = os.path.join(folder, f'tmux-{os.getuid()}', option('-L') or 'default')
    return socket if os.path.isabs(socket) else None


def servers_in(scratch: Path) -> list[dict]:
    """This user's tmux servers whose own environment puts TMPDIR in the run's scratch: the run's, by that proof."""
    scratch = Path(os.path.realpath(scratch))
    found = []
    for pid in procs._ours():
        try:
            if Path(f'/proc/{pid}/comm').read_text().strip() != 'tmux: server':
                continue
            argv = [part.decode(errors='replace') for part in Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
                    if part]
        except OSError:
            continue
        fields = procs.stat_fields(pid)
        try:  # a server's own child (a pane between its fork and its exec) bears its name too: only the server counts
            if fields and Path(f'/proc/{fields[1]}/comm').read_text().strip() == 'tmux: server':
                continue
        except OSError:
            pass
        environment = _environment(pid)
        if environment is None or not inside(environment.get('TMPDIR', ''), scratch):
            continue
        start = procs.start_time(pid)
        if start is not None:
            found.append({'pid': pid, 'start': start, 'socket': socket_of(argv, environment), 'root': str(scratch),
                          'found_by': "its environment: TMPDIR in the run's scratch"})
    return found


def _tmux(socket: str, *arguments: str):
    env = {key: value for key, value in os.environ.items() if key not in ('TMUX', 'TMUX_PANE', 'QD_TMUX_SOCKET')}
    return subprocess.run(['tmux', '-S', socket, '-N', '-f', '/dev/null', *arguments], env=env, capture_output=True,
                          text=True, timeout=SERVER_SECONDS)


def end_server(server: dict) -> dict:
    """End one of the run's servers through its own socket, and only that one: its pid and start checked before and
    after, a stopped one resumed (SIGCONT, the only signal sent), its socket proved to answer with that very pid, and
    the kill guarded by that pid in tmux itself. One that cannot be named, is excluded, answers for another server or
    none, or still runs after, is held by name; nothing else is signalled."""
    import signal
    import time
    pid, start, socket = server['pid'], server['start'], server.get('socket')
    entry = {**server, 'outcome': 'held'}
    if not procs.alive({pid: start}):
        return {**entry, 'outcome': 'ended'}
    if socket is None:
        return {**entry, 'reason': 'its socket cannot be named'}
    if excluded_socket(socket):
        return {**entry, 'reason': "a ship's or the default socket: never ended by a run"}
    fields = procs.stat_fields(pid)
    if fields and fields[0] == 'T' and procs.start_time(pid) == start:
        os.kill(pid, signal.SIGCONT)
        entry['resumed'] = True
    try:
        answer = _tmux(socket, 'display-message', '-p', '#{pid}')
    except (OSError, subprocess.TimeoutExpired):
        return {**entry, 'reason': 'its socket did not answer'}
    if answer.returncode or answer.stdout.strip() != str(pid) or not procs.alive({pid: start}):
        return {**entry, 'reason': 'its socket answers for another server, or none'}
    try:
        _tmux(socket, 'if-shell', '-F', '#{==:#{pid},%d}' % pid, 'kill-server')
    except (OSError, subprocess.TimeoutExpired):
        pass  # judged below by the process itself
    deadline = time.monotonic() + SERVER_SECONDS
    while procs.alive({pid: start}) and time.monotonic() < deadline:
        time.sleep(0.02)
    if procs.alive({pid: start}):
        return {**entry, 'reason': 'it still runs after kill-server through its own socket'}
    return {**entry, 'outcome': 'ended'}


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
        if not inside(service.get('ship'), scratch):
            # A record names a ship outside this run's scratch: whatever wrote it, that service is not the run's to
            # stop. It is never signalled; it is named, and it holds the cleanup until a person looks.
            held = True
            entries.append({**entry, 'outcome': 'held', 'reason': "its ship lies outside the run's scratch; left running"})
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
    # Then the tmux servers the run's tests left, by the proof of their own environment (see above).
    servers = [end_server(server) for server in servers_in(scratch)]
    held = held or any(server['outcome'] == 'held' for server in servers)
    stopped = any(e['outcome'] == 'stopped' for e in entries) or any(s['outcome'] == 'ended' for s in servers)
    return {'outcome': 'held' if held else ('stopped' if stopped else 'none'), 'entries': entries, 'servers': servers}
