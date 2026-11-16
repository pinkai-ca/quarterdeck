"""qd service --settle-barrier: an unreadable service stop barrier moved aside, unchanged, once nothing it could
have recorded runs.

A stop barrier (``stop_barrier``, ``runtime/locks/service-stop.json``) that no reader can trust holds every stop and
every start, and nothing it names can be read to clear it. This act settles it, and only it: the file is moved,
unchanged and never followed, into ``runtime/evidence/stop-barrier/``, and the move is logged, only when all three
of these hold, read under the stop guard's lock so that no start or stop runs between the reading and the move:

(a) the service's lock is free;
(b) no live process of this user sits in a cgroup named for this ship's service (``qd-service-<ship>-*.scope``),
    leaving out a tmux server in it and everything under one, as a stop does;
(c) no live process of this user outside the ship's own tmux server's tree, and outside this act's own process and
    its ancestors, carries ``QD_SHIP`` naming this ship in its environment.

Otherwise it refuses, naming what it saw: the condition, the pids, their process names and their cgroups, never a
value of any environment. Another process's environment is read only to test for ``QD_SHIP`` naming this ship; no
value is printed, logged or kept. A barrier that reads cleanly is not this act's: a start or a stop clears it.

Its limit: a child that cleared its own environment, or made itself unreadable, and left the scope escapes (b) and
(c). A process of this user whose environment this user cannot read (one that is not dumpable) is skipped by (c) and
named, by pid and process name, in the logged line and the output; but one that sits in any service's scope
(``qd-service-*``) is not skipped, and refuses.

Before (c) reads any other process's environment, the act makes its own process non-dumpable
(``prctl(PR_SET_DUMPABLE, 0)``) with no core (``RLIMIT_CORE`` 0, its soft limit), so that nothing of what it read can
be taken from it by another process of this user, through ptrace or ``/proc/<pid>/mem``, or written in a core
dump. It stays so for the rest of its life, which ends with the act: the bytes it read are dropped, but a freed copy
may linger in its memory, so dumpability is not restored after (c). Where it cannot make itself so, (c) reads
nothing and the act refuses.

Only a systems seat may run it, as with qd press and qd type, or the operator's own shell (no ``QD_SEAT``), which
holds the ship's power in any case.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

from . import service_processes, servicectl, ship_locks, stop_barrier
from .procstat import EXITED
from .ship import Ship, ShipError

PROC = Path('/proc')
PR_GET_DUMPABLE, PR_SET_DUMPABLE = 3, 4  # linux/prctl.h
EVIDENCE = 'stop-barrier'
GUARD = 'service-stop-guard.lock'


def authority(ship: Ship, caller: str | None) -> str | None:
    """Why ``caller`` may not settle the barrier, or None: a systems seat, or the operator's own shell."""
    if not caller:
        return None  # the operator's shell: no seat names it
    spec = ship.seats.get(caller)
    if spec is None:
        return f"refused: {caller} is not a seat of {ship.name}; only a systems seat or the operator settles the barrier"
    if spec.role != 'systems':
        return f"refused: {caller} is a {spec.role} seat; only a systems seat or the operator settles the barrier"
    return None


def unit_pattern(ship: Ship) -> re.Pattern:
    """The scope units servicectl.scoped names for this ship's service, and no other ship's."""
    name = re.sub(r'[^A-Za-z0-9_.-]', '_', ship.name)[:64] or 'ship'
    return re.compile(rf'qd-service-{re.escape(name)}-[0-9a-f]{{8}}\.scope')


def _own(pids_root: Path):
    """This user's processes: (pid, process name, state)."""
    uid = os.getuid()
    for entry in pids_root.iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            state = (entry / 'stat').read_text().rsplit(')', 1)[1].split()[0]
            comm = (entry / 'comm').read_text().rstrip('\n')
        except (OSError, IndexError, UnicodeError):
            continue  # ended while read
        if state not in EXITED:
            yield int(entry.name), comm


def _cgroup(pid: int) -> str:
    """The process's cgroup v2 path, as /proc gives it."""
    rows = (PROC / str(pid) / 'cgroup').read_text().splitlines()
    return next(row[3:] for row in rows if row.startswith('0::'))


def in_scope(ship: Ship, *, cgroup_of=_cgroup, proc=PROC) -> dict[int, tuple[str, str]]:
    """(b): this user's live processes in a scope named for this ship's service, less a tmux server there and
    everything under one: pid -> (process name, unit)."""
    unit = unit_pattern(ship)
    found = {}
    for pid, comm in _own(proc):
        try:
            path = cgroup_of(pid)
        except (OSError, StopIteration, UnicodeError):
            continue  # ended while read, or no cgroup v2 line: no scope of any service
        named = next((part for part in Path(path).parts if unit.fullmatch(part)), None)
        if named is not None:
            found[pid] = (comm, named)
    servers = [pid for pid in found if service_processes.server(pid)]
    spared = service_processes.descendants(servers) if servers else set()
    return {pid: seen for pid, seen in found.items() if pid not in spared}


def _prctl(option: int, value: int) -> int:
    import ctypes
    return ctypes.CDLL(None, use_errno=True).prctl(option, value, 0, 0, 0)


def undumpable() -> str | None:
    """Make this act's own process non-dumpable, with no core, before (c) reads another process's environment; it
    stays so for the act's life (see the module's docstring). None once it holds, else why it could not."""
    import resource
    try:
        _, hard = resource.getrlimit(resource.RLIMIT_CORE)
        resource.setrlimit(resource.RLIMIT_CORE, (0, hard))
        if _prctl(PR_SET_DUMPABLE, 0) != 0 or _prctl(PR_GET_DUMPABLE, 0) != 0:
            return "this act could not make itself non-dumpable, so it read no other process's environment"
    except (OSError, ValueError, AttributeError) as error:
        return (f"this act could not make itself non-dumpable ({type(error).__name__}), so it read no other "
                "process's environment")
    return None


def _environ(pid: int, proc: Path = PROC) -> bytes:
    """Another process's environment, read once, for (c)'s test alone."""
    with open(proc / str(pid) / 'environ', 'rb') as stream:
        return stream.read()


def _ancestors(pid: int, proc=PROC) -> set[int]:
    found = set()
    while pid > 1 and pid not in found:
        found.add(pid)
        try:
            pid = int((proc / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()[1])
        except (OSError, IndexError, ValueError, UnicodeError):
            break
    return found


def _names_ship(value: bytes, folder: str, pid: int, proc: Path) -> bool:
    """Whether one QD_SHIP value names this ship's folder, as qd resolves it (ship.choose)."""
    try:
        text = os.fsdecode(value)
        if not text:
            return False
        text = os.path.expanduser(text)
        if not os.path.isabs(text):
            text = os.path.join(os.readlink(proc / str(pid) / 'cwd'), text)
        return os.path.realpath(text) == folder
    except (OSError, ValueError):
        return False


def carriers(ship: Ship, server: int | None, *, cgroup_of=_cgroup, proc=PROC,
             environ_of=_environ) -> tuple[dict[int, str], dict[int, str], dict[int, str]]:
    """(c): this user's live processes outside the ship's tmux server's tree and this act's own line that carry
    QD_SHIP naming this ship; those whose environment this user cannot read, skipped; and those of that kind that sit
    in any service's scope (qd-service-*), never skipped. Each pid -> process name (and its unit, for the last)."""
    folder = os.path.realpath(ship.folder)
    left_out = service_processes.descendants([server]) if server else set()
    left_out |= _ancestors(os.getpid(), proc)
    found, unreadable, scoped = {}, {}, {}
    for pid, comm in _own(proc):
        if pid in left_out:
            continue
        try:
            environment = environ_of(pid, proc)
        except PermissionError:  # not dumpable: its environment is not this user's to read
            try:
                unit = next((part for part in Path(cgroup_of(pid)).parts if service_processes.UNIT.fullmatch(part)),
                            None)
            except (OSError, StopIteration, UnicodeError):
                unit = None
            if unit is not None:
                scoped[pid] = f"{comm} in {unit}"  # in a service's scope: never skipped
            else:
                unreadable[pid] = comm
            continue
        except OSError:
            continue  # ended while read
        carries = any(_names_ship(entry[len(b'QD_SHIP='):], folder, pid, proc)
                      for entry in environment.split(b'\0') if entry.startswith(b'QD_SHIP='))
        del environment  # read only for this test: never kept
        if carries:
            found[pid] = comm
    return found, unreadable, scoped


def lock_free(runtime: Path) -> str | None:
    """(a): None when the service's lock is free, else what was seen."""
    held = servicectl._held(servicectl.lock_path(runtime))
    legacy = servicectl._held(servicectl.legacy_lock_path(runtime))
    if held is True or legacy is True:
        holder = servicectl.holder(runtime)
        return f"the service's lock is held" + (f" (pid {holder})" if holder else '')
    if held is None:
        return f"the service's lock {servicectl.lock_path(runtime)} cannot be read here"
    return None


def ship_server(tmux) -> tuple[int | None, str | None]:
    """The ship's own tmux server's pid, or None where none runs; with why it cannot be told, where it cannot."""
    from . import jail_end
    try:
        server = tmux.server_pid()
    except Exception:
        server = None
    if isinstance(server, int) and server > 1:
        return server, None
    if jail_end.server_state(tmux) == jail_end.GONE:
        return None, None  # no server at its socket: no pane can descend from it
    return None, "the ship's tmux server runs but its pid cannot be read, so its tree cannot be left out"


def settle(ship: Ship, tmux, caller: str | None, *, cgroup_of=_cgroup, proc=PROC, environ_of=_environ) -> str:
    """Move an unreadable stop barrier aside, unchanged, under (a), (b) and (c); else refuse, naming what was seen."""
    if refusal := authority(ship, caller):
        return refusal
    runtime = ship.runtime
    target = stop_barrier.path(runtime)
    who = caller or 'the operator'
    with ship_locks.held(ship_locks.path(runtime, GUARD), 'service stop barrier settling'):
        try:
            if stop_barrier.read(runtime) is None:
                return f"no service stop barrier at {target}: nothing to settle"
            return (f"refused: the service stop barrier {target} reads cleanly; a start or a stop clears it "
                    "itself once every process it recorded is gone (qd status names them)")
        except ShipError:
            pass  # unreadable: this act's
        seen = []
        if why := lock_free(runtime):
            seen.append(f"(a) {why}")
        scope = in_scope(ship, cgroup_of=cgroup_of, proc=proc)
        if scope:
            seen.append("(b) in this ship's service scope: " + ', '.join(
                f"pid {pid} {comm} in {unit}" for pid, (comm, unit) in sorted(scope.items())))
        server, unknown = ship_server(tmux)
        unreadable = {}
        if unknown:
            seen.append(f"(c) {unknown}")
        elif why := undumpable():
            seen.append(f"(c) {why}")
        else:
            found, unreadable, scoped = carriers(ship, server, cgroup_of=cgroup_of, proc=proc, environ_of=environ_of)
            if found:
                seen.append("(c) outside the ship's tmux server, QD_SHIP names this ship in: " + ', '.join(
                    f"pid {pid} {comm}" for pid, comm in sorted(found.items())))
            if scoped:
                seen.append("(c) environment unreadable to this user, in a service's scope: " + ', '.join(
                    f"pid {pid} {where}" for pid, where in sorted(scoped.items())))
        skipped = ', '.join(f"pid {pid} {comm}" for pid, comm in sorted(unreadable.items()))
        if seen:
            servicectl.append_log(runtime, 'stop-barrier-settle-refused', caller=who, barrier=str(target), seen=seen,
                                  environment_unreadable=skipped)
            return (f"refused: the service stop barrier {target} stays: " + '; '.join(seen)
                    + "; nothing was moved")
        evidence = runtime / 'evidence' / EVIDENCE
        evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        destination = evidence / f'service-stop-{stamp}.json'
        if os.path.lexists(destination):
            raise ShipError(f"{destination} exists already; nothing was moved")
        os.rename(target, destination)  # the entry itself, never followed: a link moves as a link, bytes unchanged
        proof = ("the service's lock is free; no process in this ship's service scope; no process outside the "
                 "ship's tmux server carries QD_SHIP for this ship")
        servicectl.append_log(runtime, 'stop-barrier-settled', caller=who, barrier=str(target),
                              moved=str(destination), proof=proof, environment_unreadable=skipped)
        return (f"service stop barrier moved aside unchanged: {destination} · {proof}"
                + (f" · environment unreadable to this user, skipped: {skipped}" if skipped else ''))
