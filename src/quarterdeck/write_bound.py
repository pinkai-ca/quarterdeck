"""The write-bound alarm: a jailed seat whose writable folders pass their bound is stopped, and held down.

A jail's root and the operator's home are memory with a size (sandbox), but the folders it writes lie on the
machine's disk: its own seat folder, its workspace, its drop, its check folder and an endpoint seat's relay folder.
Nothing bounds those but this alarm. It needs no daemon and no root:

- It finds each jail by its process: the outermost ``bwrap`` of this user that binds a seat's own folder writable
  and has a pid namespace of its own. What that process binds writable (``--bind``, ``--bind-try``) is what is
  measured, so a folder the jail writes is never missed, and the jail's launch record, which the jail itself can
  write, is never trusted for a pid or a folder.
- Each folder is walked by descriptors: ``openat`` with ``O_NOFOLLOW`` from the held descriptor of its parent and
  ``fstatat`` without following, so no path is ever longer than one name and no link is followed. It stays on the
  folder's own device and counts each inode once in a group, by its blocks on the disk.
- Files the jail's processes hold open after unlinking them, which no walk can see, are read from
  ``/proc/<pid>/fd`` and counted against the largest bound on their device, where their folder is no longer known.
- A walk is bounded by entries (``ENTRIES``, the open files counted too), by the alarm thread's own CPU time for
  that seat's measure (``SECONDS``, ``time.thread_time``) and by depth (``DEPTH``). A walk that reaches one of these
  reads as over: a jail cannot hide its writes behind a costly tree, and it drives the walk's own CPU only through
  what it builds, which the entry bound caps.
- A walk is bounded on the clock as well, generously (``WALL``). The clock measures how busy the service and the
  machine are, not the jail: the alarm thread shares the interpreter with a busy service, which can starve it. So a
  walk that passes it reads as unmeasured, not over: the seat stays up, the status line names it, and the next pass
  retries with the same bounds. What a walk cut short reached still counts: if that already passes a bound, it is
  over now. A jail can raise the machine's load on purpose, so unmeasured is no free window: ``PASSES`` unmeasured
  passes in a row stop the seat as a measured over does, and a measured pass starts the count again
  (``runtime/post/write-bound/unmeasured/<seat>.json``, which no jail is shown).
- One look over every jail has a budget on the clock (``LOOK``). A seat it did not reach is measured first on the
  next look, so no seat waits behind another's slow walk for more than one look.
- A backstop no walk can be starved out of: at the end of each look, one ``statvfs`` for each device (``disk_space``) a jail writes.
  Under its floor (``floor``), or fallen more than ``FALL`` since the last look, the device stops every jailed seat
  that went unmeasured there this look or whose bytes there grew since their last whole measure. A seat measured
  whole there and not grown stays up; its first measure is its baseline. The hold says why, the status line names
  it, and the service tells the desk (``floor_reason``).
- Over a bound, the hold is placed first, then the jail's whole process tree is stopped with SIGSTOP and killed with
  SIGKILL, an endpoint seat's relay included. The hold (``runtime/post/write-bound/<seat>.json``, which no jail is
  shown) registers in ``relaunch_intent.HOLDS`` and clears the seat's relaunch intent where it is placed, so no
  service brings the seat back. Every launch refuses while it stands (``require_clear``), and the status line names
  it (``status``). Only a person's word for that seat lifts it: ``qd up``, ``qd fresh`` or ``qd restart`` with the
  seat named (``lift``). A whole ship's ``qd up`` leaves it down and says so.

The service runs the alarm in a thread of its own, every ``PERIOD`` seconds, so a pass that hangs never delays it.
Beside it, the jail's launcher sets ``RLIMIT_FSIZE`` (jail_environment.FILE_BYTES), so one file can never grow past it.

law: nothing-waits-silently
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import stat
import threading
import time

from . import post_dir, relaunch_intent
from .ship import ShipError

MiB = 1024 ** 2
GiB = 1024 ** 3
SEAT, WORKSPACE, SMALL = 'seat folder', 'workspace', 'drop, check and relay'
# The captain's sizes (15 Nov). A hand's seat folder held 101 to 642 MiB on that day, transcripts most of it; its
# workspace under 10 MiB. The drop holds messages a few kilobytes long, the check folder one proof, the relay a socket.
BOUNDS = {SEAT: 4 * GiB, WORKSPACE: 2 * GiB, SMALL: 64 * MiB}
ENTRIES = 200_000  # a hand's seat folder held about 30,000 entries; a walk of 200,000 took about 0.4 s (measured)
SECONDS = 2.0  # the alarm thread's own CPU time for one seat's whole measure, its open files included
# On the clock. On 15 Nov a walk of a 47-entry home passed 2 s at load 12 to 16 with the alarm thread starved: the
# clock read the service's load, not the jail's tree. Passing this bound reads as unmeasured, never as over.
# La'an's terms (15 Nov): an unmeasured window of about 30 s at most, three looks, where a writer at full speed puts
# some 0.4 GiB a second on the disk. A hand's walk takes well under a second on a machine that is not starved.
WALL = 10.0
PASSES = 3  # unmeasured passes in a row that stop a seat as a measured over does
LOOK = 10.0  # on the clock, for one look over every jail; a walk once started runs to its own bounds
# The free-space floor of a device a jail writes, judged on its free space as a user's write sees it (f_bavail): the
# smaller of these two (La'an, 15 Nov), 50 GiB on / and about 2.2 GiB on /tmp. The larger was under both disks' free
# space that day: / held 369.3 GiB free of 7389 GiB, 5% being 369.5 GiB, and /tmp is 44.9 GiB in all. On /tmp an
# endpoint seat's relay folder is in the 64 MiB group, so its walk catches a jail there long before any floor; the
# floor is the backstop for the disk.
FLOOR_BYTES = 50 * GiB
FLOOR_SHARE = 0.05  # of the device's size
FALL = 20 * GiB  # a fall of more than this between two looks trips the floor as well
DEPTH = 512  # folders held open at once; real trees are a few dozen deep, and the service's descriptors are finite
# Measured (15 Nov): a writer at full speed against a 2 GiB bound, looked at every 1 s, was stopped 0.08 GiB over;
# every 5 s, 1.97 GiB over. The overshoot is the write rate times the period, so the period is the short one.
PERIOD = 1.0
FOLDER = 'write-bound'
LIMIT = 4096  # a hold is a few hundred bytes; a larger file is not one


class Over(Exception):
    """A walk that reached a bound, or a folder it could not read: either reads as over."""


class Unmeasured(Exception):
    """A walk cut short by the clock alone: it measured the machine's load, not the jail. Never over by itself."""


@dataclass
class Budget:
    """One seat's measure: entries, depth and the thread's own CPU time are hard; the clock only cuts it short."""

    entries: int = ENTRIES
    seconds: float = SECONDS  # CPU time of the thread that walks (time.thread_time)
    depth: int = DEPTH
    wall: float = WALL
    deadline: float = 0.0  # on the thread's CPU clock
    wall_deadline: float = 0.0  # on the monotonic clock
    seen: int = 0

    def start(self) -> 'Budget':
        self.deadline = time.thread_time() + self.seconds
        self.wall_deadline = time.monotonic() + self.wall
        return self

    def tick(self) -> None:
        self.seen += 1
        if self.seen > self.entries:
            raise Over(f'the walk passed {self.entries} entries')
        if time.thread_time() > self.deadline:
            raise Over(f'the walk passed {self.seconds:g} s of CPU time')
        if time.monotonic() > self.wall_deadline:
            raise Unmeasured(f'the walk passed {self.wall:g} s on the clock')


@dataclass
class Group:
    label: str
    folders: list[str]
    bound: int
    held: int = 0
    devices: set = field(default_factory=set)
    inodes: set = field(default_factory=set)
    on: dict = field(default_factory=dict)  # device -> bytes counted on it


@dataclass
class Jail:
    """One seat's jail as its processes show it."""

    seat: str
    root: int  # the outermost bwrap
    binds: list[str]  # its writable folders, as given on its command line
    pids: dict[int, str]  # the root, its descendants and an endpoint seat's relay: pid -> its start


@dataclass
class Reading:
    seat: str
    groups: list[Group]
    over: tuple[str, int, int, str] | None  # (group, held, bound, why) of the first group over, or None
    pids: dict[int, str]
    # (group, held, bound, why) where the clock cut the walk short and nothing it reached was over; else None
    unmeasured: tuple[str, int, int, str] | None = None
    walked: set = field(default_factory=set)  # the folders walked whole
    opened: bool = False  # the processes' open files were read whole
    on: dict = field(default_factory=dict)  # device -> bytes counted on it, open files included


@dataclass
class State:
    """What one look leaves the next: the seats it did not reach, each device's free space, each seat's bytes."""

    unreached: list = field(default_factory=list)
    free: dict = field(default_factory=dict)  # device -> free bytes at the last look
    sizes: dict = field(default_factory=dict)  # seat -> {device: bytes at its last whole measure there}
    unread: dict = field(default_factory=dict)  # device -> (folder, looks running its statvfs failed, the error's kind)
    unreadable: set = field(default_factory=set)  # seats whose unmeasured count could not be read, logged once


# -- processes -------------------------------------------------------------------------


def _processes() -> dict[int, tuple[int, str, list[str]]]:
    """This user's processes: pid -> (parent pid, start, command line). One that ends while read is left out."""
    found = {}
    uid = os.getuid()
    for name in os.listdir('/proc'):
        if not name.isdigit():
            continue
        try:
            if os.stat(f'/proc/{name}').st_uid != uid:
                continue
            with open(f'/proc/{name}/stat', 'rb') as stream:
                fields = stream.read().rpartition(b')')[2].split()
            with open(f'/proc/{name}/cmdline', 'rb') as stream:
                argv = [part.decode(errors='replace') for part in stream.read().split(b'\0')[:-1]]
        except (OSError, IndexError):
            continue
        if len(fields) > 19 and fields[1].isdigit():
            found[int(name)] = (int(fields[1]), fields[19].decode(), argv)  # its start, in ticks since boot
    return found


def _binds(argv: list[str]) -> list[str]:
    """The folders a bwrap command line binds writable, by their outside path, up to its command."""
    binds, at = [], 1
    while at < len(argv) and argv[at] != '--':
        if argv[at] in ('--bind', '--bind-try') and at + 2 < len(argv):
            binds.append(argv[at + 1])
            at += 3
        else:
            at += 1
    return binds


def _is_jail_of(argv: list[str], folders: set[str]) -> bool:
    options = argv[:argv.index('--')] if '--' in argv else argv
    if not argv or os.path.basename(argv[0]) != 'bwrap' or '--unshare-pid' not in options:
        return False
    return any(folder in folders for folder in _binds(argv))


def _descendants(processes, roots) -> list[int]:
    children: dict[int, list[int]] = {}
    for pid, row in processes.items():
        children.setdefault(row[0], []).append(pid)
    found: dict[int, None] = {}
    stack = list(roots)
    while stack:
        pid = stack.pop()
        if pid in found:
            continue
        found[pid] = None
        stack += children.get(pid, [])
    return list(found)


def seat_folder(ship, name: str) -> Path:
    return ship.runtime / 'seats' / name


def jails(ship, processes=None) -> dict[str, Jail]:
    """Every declared seat's running jail, found by its process; a seat with no jail running is absent."""
    processes = _processes() if processes is None else processes
    names = {}
    for name in ship.seats:
        folder = seat_folder(ship, name)
        names[str(folder)] = name
        names[os.path.realpath(folder)] = name
    roots: dict[int, str] = {}
    for pid, (_, _, argv) in processes.items():
        if _is_jail_of(argv, set(names)):
            roots[pid] = next(names[folder] for folder in _binds(argv) if folder in names)
    found = {}
    for pid, seat in sorted(roots.items()):
        parent, ancestors = processes[pid][0], set()
        while parent in processes and parent not in ancestors:
            ancestors.add(parent)
            parent = processes[parent][0]
        if ancestors & set(roots):
            continue  # a bwrap inside a jail: part of that jail's tree, never a measure of its own
        pids = {child: processes[child][1] for child in _descendants(processes, [pid])}
        relay = processes[pid][0]
        if relay in processes and 'quarterdeck.endpoint_relay' in processes[relay][2]:
            pids[relay] = processes[relay][1]  # an endpoint seat's pane runs the relay, which runs the jail
        binds = _binds(processes[pid][2])
        if seat in found:
            found[seat].pids.update(pids)
            found[seat].binds += [b for b in binds if b not in found[seat].binds]
        else:
            found[seat] = Jail(seat, pid, binds, pids)
    return found


# -- the measure ----------------------------------------------------------------------------


def _device(info: os.stat_result) -> int:
    """The device a stat names: the one lookup by which the walk, the open files and the floor group by disk."""
    return info.st_dev


def _flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def walk(folder: str, group: Group, budget: Budget) -> None:
    """Add what ``folder`` holds on its own device to ``group``: by descriptors, no link followed, an inode once."""
    try:
        top = os.open(folder, _flags())
    except FileNotFoundError:
        return  # a bind-try folder that is not there holds nothing
    except OSError as error:
        raise Over(f'{folder} could not be opened ({type(error).__name__})') from None
    stack: list[tuple[int, list[str]]] = []
    try:
        info = os.fstat(top)
        device = _device(info)
        group.devices.add(device)
        _count(group, info)
        stack.append((top, _scan(top, device, group, budget)))
        top = None
        while stack:
            fd, pending = stack[-1]
            if not pending:
                os.close(stack.pop()[0])
                continue
            name = pending.pop()
            try:
                child = os.open(name, _flags(), dir_fd=fd)
            except FileNotFoundError:
                continue  # gone since it was listed
            except OSError as error:
                raise Over(f'a folder in {folder} could not be opened ({type(error).__name__})') from None
            if _device(os.fstat(child)) != device:
                os.close(child)
                continue
            if len(stack) >= budget.depth:
                os.close(child)
                raise Over(f'the walk passed {budget.depth} folders deep')
            stack.append((child, []))
            stack[-1] = (child, _scan(child, device, group, budget))
    finally:
        if top is not None:
            os.close(top)
        for fd, _ in stack:
            os.close(fd)


def _scan(fd: int, device: int, group: Group, budget: Budget) -> list[str]:
    """Count one folder's entries; return the names of its subfolders on the same device."""
    folders = []
    try:
        listing = os.scandir(fd)
    except OSError as error:
        raise Over(f'a folder could not be listed ({type(error).__name__})') from None
    with listing:
        for entry in listing:
            budget.tick()
            try:
                info = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError as error:
                raise Over(f'an entry could not be read ({type(error).__name__})') from None
            if _device(info) != device:
                continue  # a mount point of another device: not this folder's disk
            _count(group, info)
            if stat.S_ISDIR(info.st_mode):
                folders.append(entry.name)
    return folders


def _count(group: Group, info) -> None:
    device = _device(info)
    key = (device, info.st_ino)
    if key not in group.inodes:
        group.inodes.add(key)
        group.held += info.st_blocks * 512
        group.on[device] = group.on.get(device, 0) + info.st_blocks * 512


def open_deleted(pids, budget: Budget) -> dict[tuple[int, int], int]:
    """Regular files the processes hold open after their last name went: (device, inode) -> bytes on the disk."""
    found = {}
    for pid in pids:
        try:
            fds = os.listdir(f'/proc/{pid}/fd')
        except OSError:
            continue  # ended, or not ours to read
        for fd in fds:
            budget.tick()
            path = f'/proc/{pid}/fd/{fd}'
            try:
                if not os.readlink(path).endswith(' (deleted)'):
                    continue
                info = os.stat(path)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                found[(_device(info), info.st_ino)] = info.st_blocks * 512
    return found


def groups(ship, jail: Jail, bounds=None) -> list[Group]:
    """The jail's writable folders in their groups: its seat folder, its small folders, and the rest as workspace."""
    bounds = {**BOUNDS, **(bounds or {})}
    seat = {str(seat_folder(ship, jail.seat)), os.path.realpath(seat_folder(ship, jail.seat))}
    from . import endpoint_relay, sandbox
    small = {str(ship.runtime / 'post' / 'drop' / jail.seat), str(sandbox.check_proof_folder(ship.runtime, jail.seat)),
             str(endpoint_relay.folder_for(seat_folder(ship, jail.seat)))}
    small |= {os.path.realpath(folder) for folder in small}
    found = {label: Group(label, [], bounds[label]) for label in (SEAT, WORKSPACE, SMALL)}
    for folder in jail.binds:
        label = SEAT if folder in seat else SMALL if folder in small else WORKSPACE
        found[label].folders.append(folder)
    return list(found.values())


def measure(ship, jail: Jail, *, bounds=None, budget: Budget | None = None) -> Reading:
    """What the jail holds in each group, and the first group over its bound, if any.

    The processes' open files are read first and the small folders walked first, so a walk the clock cuts short has
    most often read whole every device but the one it was cut on: the free-space floor judges a seat by device.
    """
    budget = (budget or Budget()).start()
    found = groups(ship, jail, bounds)
    current = max(found, key=lambda group: group.bound)
    deleted: dict = {}
    reading = Reading(jail.seat, found, None, jail.pids)
    try:
        deleted = open_deleted(jail.pids, budget)
        reading.opened = True
        for current in sorted(found, key=lambda group: group.label != SMALL):
            for folder in current.folders:
                walk(folder, current, budget)
                reading.walked.add(folder)
    except Over as why:
        reading.over = (current.label, current.held, current.bound, str(why))
        return reading
    except Unmeasured as why:
        _deleted(found, deleted, reading)
        for group in found:  # what the walk reached still counts: over already is over now
            if group.held > group.bound:
                reading.over = (group.label, group.held, group.bound, f'over its bound before {why}')
                return reading
        reading.unmeasured = (current.label, current.held, current.bound, str(why))
        return reading
    _deleted(found, deleted, reading)
    for group in found:
        if group.held > group.bound:
            reading.over = (group.label, group.held, group.bound, 'over its bound')
            return reading
    return reading


def _deleted(found: list[Group], deleted: dict, reading: Reading) -> None:
    """Add the open files whose names are gone; then the bytes on each device, those files included."""
    for key, size in deleted.items():
        # Its folder is unknown once its name is gone: the largest bound on its device takes it.
        places = [group for group in found if key[0] in group.devices]
        if places:
            place = max(places, key=lambda group: group.bound)
            if key not in place.inodes:
                place.inodes.add(key)
                place.held += size
                place.on[key[0]] = place.on.get(key[0], 0) + size
        elif all(key not in group.inodes for group in found):
            reading.on[key[0]] = reading.on.get(key[0], 0) + size  # its device's folders were not reached
    for group in found:
        for device, count in group.on.items():
            reading.on[device] = reading.on.get(device, 0) + count


# -- the stop and the hold ------------------------------------------------------------------


def folder(runtime) -> Path:
    return Path(runtime) / 'post' / FOLDER


def path(runtime, seat: str) -> Path:
    return folder(runtime) / f'{seat}.json'


def size(count: int) -> str:
    return f'{count / GiB:.2f} GiB' if count >= GiB else f'{count / MiB:.1f} MiB'


def place(runtime, reading: Reading, who: str = 'the write-bound alarm', floor: bool = False) -> dict:
    """Write the hold, then clear the seat's relaunch intent: from here nothing brings the seat back by itself."""
    label, held, bound, why = reading.over
    data = dict(version=1, seat=reading.seat, at=datetime.now(timezone.utc).isoformat(), group=label,
                held=held, bound=bound, why=why, by=who)
    if floor:
        data['floor'] = True  # held, bound: the device's free space and its floor
    post_dir.ensure_directory(folder(runtime))
    post_dir.replace_bytes(path(runtime, reading.seat), (json.dumps(data) + '\n').encode())
    relaunch_intent.clear(runtime, reading.seat, f'its write-bound hold, placed by {who}')
    return data


def kill(pids: dict[int, str], rounds: int = 10, *, observe=None) -> list[int]:
    """SIGSTOP the tree so nothing in it forks on, then SIGKILL every process of it; the pids signalled.

    A process is known by its pid and its start, so a pid the system gave to another process since is never
    signalled; a child is taken into the tree only while its parent is still the known process.
    ``observe``, when supplied, records every identity seen before that round's signals.
    """
    known = dict(pids)
    signalled: list[int] = []
    for _ in range(rounds):
        processes = _processes()
        live = [pid for pid, start in known.items() if pid in processes and processes[pid][1] == start]
        for child in _descendants(processes, live):
            known.setdefault(child, processes[child][1])
        if observe is not None:
            observe(dict(known))
        live = [pid for pid, start in known.items() if pid in processes and processes[pid][1] == start
                and _state(pid) not in ('Z', 'X')]
        if not live:
            break
        for sig in (signal.SIGSTOP, signal.SIGKILL):
            for pid in live:
                try:
                    os.kill(pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass
        signalled += [pid for pid in live if pid not in signalled]
        time.sleep(0.05)
    return signalled


def tree(pid: int, start: str | None, processes=None) -> dict[int, str]:
    """A process and its descendants now, each by pid and start; empty when that process is not the one named.

    Read-only: what kill() is then given, so the stop of a seat ends the same tree the write-bound alarm would.
    """
    processes = _processes() if processes is None else processes
    if pid not in processes or start is None or processes[pid][1] != start:
        return {}
    return {child: processes[child][1] for child in _descendants(processes, [pid])}


def alive(pids: dict[int, str]) -> dict[int, str]:
    """Which of these processes still run, by pid and start; a zombie has ended and is not counted."""
    processes = _processes()
    return {pid: start for pid, start in pids.items()
            if pid in processes and processes[pid][1] == start and _state(pid) not in ('Z', 'X')}


def _state(pid: int) -> str:
    try:
        with open(f'/proc/{pid}/stat', 'rb') as stream:
            return stream.read().rpartition(b')')[2].split()[0].decode()
    except (OSError, IndexError):
        return 'X'


def stop(ship, reading: Reading, log, who: str = 'the write-bound alarm', floor: bool = False) -> dict:
    data = place(ship.runtime, reading, who, floor)
    killed = kill(reading.pids)
    log('write-bound', seat=reading.seat, group=data['group'], held=data['held'], bound=data['bound'],
        why=data['why'], killed=len(killed), **({'floor': True} if floor else {}))
    return data


def read(runtime, seat: str) -> dict | None:
    """The seat's hold, or None; a hold that cannot be read is still a hold (ShipError says why)."""
    target = path(runtime, seat)
    try:
        mode = post_dir.mode(target)
    except FileNotFoundError:
        return None
    except (OSError, ShipError) as error:
        raise ShipError(f'its write-bound hold is unreadable ({type(error).__name__})') from None
    if mode is None:
        return None
    if not stat.S_ISREG(mode):
        raise ShipError('its write-bound hold is not a plain file')
    try:
        data = json.loads(post_dir.read_bytes(target, limit=LIMIT))
    except (OSError, ShipError, ValueError) as error:
        raise ShipError(f'its write-bound hold is unreadable ({type(error).__name__})') from None
    if not isinstance(data, dict) or data.get('seat') != seat:
        raise ShipError('its write-bound hold is not one the alarm wrote')
    return data


def reason(runtime, seat: str) -> str | None:
    """Why the seat stays down for its write bound, in the status line's words; None when nothing holds it."""
    try:
        data = read(runtime, seat)
    except ShipError as error:
        return f'{error}; held: qd up {seat} lifts it'
    if data is None:
        return None
    if data.get('floor') is True and isinstance(data.get('why'), str):
        return f"stopped by its disk's free-space floor ({data['why']}); held: qd up {seat} lifts it"
    try:
        what = f"{data['group']} {size(int(data['held']))} of {size(int(data['bound']))}"
    except (KeyError, TypeError, ValueError):
        what = 'its write bound'
    why = data.get('why') if isinstance(data.get('why'), str) else ''
    detail = f'{what}' if why in ('', 'over its bound') else f'{what}, {why}'
    return f'stopped at its write bound ({detail}); held: qd up {seat} lifts it'


def floor_reason(runtime, seat: str) -> str | None:
    """The status line's words for a seat held by its disk's free-space floor, for the desk; None otherwise."""
    try:
        data = read(runtime, seat)
    except ShipError:
        return None
    return reason(runtime, seat) if data is not None and data.get('floor') is True else None


def require_clear(seat) -> None:
    """Every launch refuses while a write-bound hold stands."""
    if words := reason(seat.ship.runtime, seat.name):
        raise ShipError(f'{seat.name}: {words}')


def lift(runtime, seat: str, who: str) -> bool:
    """A person's word for this seat: its hold goes, logged with who lifted it. A link is left where it is."""
    target = path(runtime, seat)
    try:
        mode = post_dir.mode(target)
    except (OSError, ShipError):
        return False
    if mode is None or not stat.S_ISREG(mode):
        return False
    try:
        post_dir.remove(target)
    except FileNotFoundError:
        return False
    relaunch_intent._log(runtime, 'write-bound', seat=seat, outcome='lifted', reason=f'lifted by {who}')
    return True


def status(ship) -> list[str]:
    """One line for each seat held at its write bound, and for each left unmeasured: law: nothing-waits-silently."""
    try:
        names = sorted(name[:-5] for name in os.listdir(folder(ship.runtime)) if name.endswith('.json'))
    except FileNotFoundError:
        return floor_unread_status(ship.runtime)  # no hold was ever placed and no seat left unmeasured
    except OSError as error:
        return [f'write-bound holds unreadable ({type(error).__name__}); qd status reads them']
    lines = []
    for name in names:
        if words := reason(ship.runtime, name):
            lines.append(f'{name}: {words}')  # law: nothing-waits-silently
    try:
        left = sorted(name[:-5] for name in os.listdir(unmeasured_folder(ship.runtime)) if name.endswith('.json'))
    except FileNotFoundError:
        left = []
    except OSError as error:
        left, lines = [], [*lines, f'write-bound unmeasured passes unreadable ({type(error).__name__})']
    for name in left:
        if name in names:
            continue  # held already: the hold's line says more
        data = unmeasured_read(ship.runtime, name)
        passes = data.get('passes') if data else None
        why = data.get('why') if data and isinstance(data.get('why'), str) else 'the walk was cut short'
        count = f'{passes} of {PASSES} passes in a row' if isinstance(passes, int) else 'passes unreadable'
        lines.append(f'{name}: write bound unmeasured, {count} ({why}); '
                     f'retried each pass, stopped at {PASSES}')  # law: nothing-waits-silently
    return lines + floor_unread_status(ship.runtime)


# -- a seat left unmeasured ----------------------------------------------------------------------


def unmeasured_folder(runtime) -> Path:
    return folder(runtime) / 'unmeasured'


def unmeasured_path(runtime, seat: str) -> Path:
    return unmeasured_folder(runtime) / f'{seat}.json'


def _unmeasured_file(runtime, seat: str) -> tuple[dict | None, str | None]:
    """The seat's run of unmeasured passes, or None; and why it could not be read, when it is there but unreadable."""
    target = unmeasured_path(runtime, seat)
    try:
        mode = post_dir.mode(target)
        if mode is None:
            return None, None
        if not stat.S_ISREG(mode):
            return None, 'not a regular file'
        data = json.loads(post_dir.read_bytes(target, limit=LIMIT))
    except FileNotFoundError:
        return None, None
    except (OSError, ShipError, ValueError) as error:
        return None, type(error).__name__
    if not isinstance(data, dict) or data.get('seat') != seat:
        return None, 'not this seat\'s count'
    return data, None


def unmeasured_read(runtime, seat: str) -> dict | None:
    """The seat's run of unmeasured passes, or None. One that cannot be read reads as none: no jail can write it."""
    return _unmeasured_file(runtime, seat)[0]


def unmeasured(runtime, reading: Reading, log=None, state: State | None = None) -> int:
    """Count one more unmeasured pass in a row for the seat, kept where no jail is shown; the passes so far. A count
    that is there but cannot be read starts again at one, and says so in one log line, once while it stays so."""
    label, held, bound, why = reading.unmeasured
    before, problem = _unmeasured_file(runtime, reading.seat)
    before = before or {}
    if problem is None:
        if state is not None:
            state.unreadable.discard(reading.seat)
    elif log is not None and (state is None or reading.seat not in state.unreadable):
        log('error', step='write-bound', seat=reading.seat,
            error=f'unmeasured count unreadable ({problem}); counted again from one')
        if state is not None:
            state.unreadable.add(reading.seat)
    passes = before.get('passes') if isinstance(before.get('passes'), int) and before.get('passes') > 0 else 0
    now = datetime.now(timezone.utc).isoformat()
    data = dict(version=1, seat=reading.seat, passes=passes + 1, first_at=before.get('first_at') or now, at=now,
                group=label, held=held, bound=bound, why=why)
    post_dir.ensure_directory(unmeasured_folder(runtime))
    post_dir.replace_bytes(unmeasured_path(runtime, reading.seat), (json.dumps(data) + '\n').encode())
    return passes + 1


def measured(runtime, seat: str) -> None:
    """A measured pass, a stop, or no jail left to measure: the run of unmeasured passes ends."""
    target = unmeasured_path(runtime, seat)
    try:
        if post_dir.mode(target) is not None:
            post_dir.remove(target)
    except FileNotFoundError:
        pass


def _held(seat) -> str | None:
    return reason(seat.ship.runtime, seat.name)


if _held not in relaunch_intent.HOLDS:
    relaunch_intent.HOLDS.append(_held)


# -- the service's thread -------------------------------------------------------------------


def look(ship, log, *, bounds=None, budget: Budget | None = None, state: State | None = None, space=None,
         seconds: float | None = None) -> list[str]:
    """One look over every running jail, within ``LOOK`` on the clock; the seats stopped.

    A seat whose walk the clock cut short is counted unmeasured and retried on the next look with the same bounds;
    ``PASSES`` such looks in a row stop it as a measured over does. A seat with no jail running has nothing left to
    measure, and its count ends with it. The seats the look did not reach go first on the next (``state``), and the
    look ends with the free-space floor of every device a jail writes (``space``, the seam a test simulates).
    """
    state = State() if state is None else state
    seconds = LOOK if seconds is None else seconds
    began = time.monotonic()
    stopped, readings, unreached = [], {}, []
    running = jails(ship)
    order = [name for name in state.unreached if name in running]
    order += [name for name in sorted(running) if name not in order]
    for name in order:
        if readings and time.monotonic() - began >= seconds:
            unreached.append(name)  # first on the next look
            continue
        jail = running[name]
        readings[name] = None
        try:
            fresh = replace(budget, deadline=0.0, wall_deadline=0.0, seen=0) if budget else None
            reading = measure(ship, jail, bounds=bounds, budget=fresh)
            readings[name] = reading
            if reading.over is None and reading.unmeasured is not None:
                passes = unmeasured(ship.runtime, reading, log, state)
                label, held, bound, why = reading.unmeasured
                log('write-bound', seat=name, outcome='unmeasured', passes=passes, group=label, held=held,
                    bound=bound, why=why)
                if passes >= PASSES:
                    reading.over = (label, held, bound, f'unmeasured {passes} passes in a row, {why}')
            if reading.over is not None:
                stop(ship, reading, log)
                stopped.append(name)
                measured(ship.runtime, name)
            elif reading.unmeasured is None:
                measured(ship.runtime, name)
        except Exception as error:  # one seat's failure never keeps the alarm from the others
            log('error', step='write-bound', seat=name, error=repr(error))
    state.unreached = unreached
    if unreached:
        log('write-bound', outcome='unreached', seats=unreached, seconds=round(time.monotonic() - began, 3))
    try:
        stopped += floor_look(ship, log, state, {name: jail for name, jail in running.items() if name not in stopped},
                              readings, space)
    except Exception as error:  # the floor never stops the look; the next look reads it again
        log('error', step='write-bound-floor', error=repr(error))
    try:
        left = [entry[:-5] for entry in os.listdir(unmeasured_folder(ship.runtime)) if entry.endswith('.json')]
    except OSError:
        left = []
    for name in left:
        if name not in running:
            try:
                measured(ship.runtime, name)
            except Exception as error:
                log('error', step='write-bound', seat=name, error=repr(error))
    return stopped


# -- the free-space floor --------------------------------------------------------------------------


def disk_space(folder: str) -> tuple[int, int]:
    """The free bytes a user's write can still take on the folder's device, and the device's size."""
    info = os.statvfs(folder)
    return info.f_bavail * info.f_frsize, info.f_blocks * info.f_frsize


def floor(total: int) -> int:
    return min(FLOOR_BYTES, int(total * FLOOR_SHARE))


def _devices(jail: Jail) -> dict[str, int | None]:
    """Each writable folder's device, by one stat; None where it cannot be read. A folder not there is left out."""
    found = {}
    for folder in jail.binds:
        try:
            found[folder] = _device(os.stat(folder))
        except FileNotFoundError:
            continue  # a bind-try folder that is not there holds nothing
        except OSError:
            found[folder] = None
    return found


UNREAD_LOOKS = 2  # a device whose statvfs fails this many looks running is named on the status line


def floor_unread_path(runtime) -> Path:
    return folder(runtime) / 'floor' / 'unread.json'


def floor_unread_write(runtime, unread: dict, log) -> None:
    """Keep, where no jail is shown, each device whose free space went unread ``UNREAD_LOOKS`` looks running: its floor
    is not watched there while the walks still stand, and the status line, in another process, says so."""
    named = [dict(folder=where, looks=looks, error=kind) for where, looks, kind in sorted(unread.values())
             if looks >= UNREAD_LOOKS]
    target = floor_unread_path(runtime)
    try:
        if named:
            content = (json.dumps(dict(version=1, devices=named)) + '\n').encode()
            try:
                if post_dir.read_bytes(target, limit=LIMIT) == content:
                    return  # the same devices, the same looks: written only when it changes, not at every look
            except (FileNotFoundError, ShipError):
                pass
            post_dir.ensure_directory(target.parent)
            post_dir.replace_bytes(target, content)
        elif post_dir.mode(target) is not None:
            post_dir.remove(target)
    except (OSError, ShipError) as error:
        log('error', step='write-bound-floor', error=repr(error))


def floor_unread_status(runtime) -> list[str]:  # law: nothing-waits-silently
    target = floor_unread_path(runtime)
    try:
        mode = post_dir.mode(target)
        if mode is None:
            return []
        if not stat.S_ISREG(mode):
            raise ValueError('not a regular file')
        data = json.loads(post_dir.read_bytes(target, limit=LIMIT))
        devices = data['devices']
        return [f"write bound: free space unread on {item['folder']}, {item['looks']} looks running "
                f"({item['error']}); its floor is not watched there" for item in devices]
    except FileNotFoundError:
        return []
    except (OSError, ShipError, ValueError, KeyError, TypeError) as error:
        return [f'write bound: free-space readings unreadable ({type(error).__name__})']


def floor_look(ship, log, state: State, running: dict[str, Jail], readings: dict, space=None) -> list[str]:
    """Read each device the jails write once; where one is under its floor or fell past ``FALL``, stop the seats
    that went unmeasured there this look or grew there since their last whole measure. The seats stopped."""
    space = disk_space if space is None else space
    devices = {name: _devices(jail) for name, jail in running.items()}
    where: dict[int, str] = {}
    for name in sorted(devices):
        for folder, device in devices[name].items():
            if device is not None:
                where.setdefault(device, folder)
    free, floors, tripped = {}, {}, {}
    for device, folder in where.items():
        try:
            free[device], total = space(folder)
        except OSError as error:
            log('error', step='write-bound-floor', folder=folder, error=repr(error))
            looks = state.unread.get(device, (folder, 0, ''))[1] + 1
            state.unread[device] = (folder, looks, type(error).__name__)
            continue
        state.unread.pop(device, None)
        before, floors[device] = state.free.get(device), floor(total)
        if free[device] < floors[device]:
            tripped[device] = f'{folder}: {size(free[device])} free, under its floor of {size(floors[device])}'
        elif before is not None and before - free[device] > FALL:
            tripped[device] = (f'{folder}: free space fell {size(before - free[device])} since the last look, '
                               f'more than {size(FALL)}')
    state.free = free
    state.unread = {device: value for device, value in state.unread.items() if device in where}
    floor_unread_write(ship.runtime, state.unread, log)
    stopped = []
    for name, jail in sorted(running.items()):
        reading, last = readings.get(name), state.sizes.get(name, {})
        # A folder whose device cannot be read is judged on no device: its own walk reads as over.
        mine = {device for device in devices[name].values() if device is not None}
        whole = set()
        if reading is not None and reading.opened:
            whole = mine - {devices[name][folder] for folder in devices[name] if folder not in reading.walked}
        why = None
        for device in sorted(mine & set(tripped)):
            if device not in whole:
                why = f'{tripped[device]}; this seat went unmeasured there'
            elif device in last and reading.on.get(device, 0) > last[device]:
                why = (f'{tripped[device]}; this seat grew {size(reading.on[device] - last[device])} there since its '
                       f'last measure')
            if why:
                break
        if why is None:
            state.sizes[name] = {**last, **{device: reading.on.get(device, 0) for device in whole}}
            continue
        try:
            stop(ship, Reading(name, [], ('free space', free[device], floors[device], why), jail.pids), log, floor=True)
        except Exception as error:
            log('error', step='write-bound-floor', seat=name, error=repr(error))
            continue
        stopped.append(name)
        measured(ship.runtime, name)
    for name in list(state.sizes):
        if name not in running or name in stopped:
            del state.sizes[name]
    return stopped


class Alarm:
    """The alarm's own thread, with its own period: a service pass that hangs never delays it."""

    def __init__(self, service, period: float = PERIOD):
        self.service = service
        self.period = period
        self.stopping = threading.Event()
        self.state = State()  # what one look leaves the next
        self.swept = None  # when the last sweep of stray sandboxes ran (jail_end), on the monotonic clock
        self.thread = threading.Thread(target=self.run, name='service-write-bound', daemon=True)

    def start(self) -> 'Alarm':
        self.thread.start()
        return self

    def close(self, timeout: float = 0.2) -> None:
        self.stopping.set()
        self.thread.join(timeout=timeout)

    def run(self) -> None:
        while not self.stopping.wait(self.period):
            try:
                look(self.service.ship, self.service.log, state=self.state)
            except Exception as error:  # the alarm never stops the service; the next look tries again
                self.service.log('error', step='write-bound', error=repr(error))
            self.sweep()

    def sweep(self, clock=time.monotonic) -> None:
        """At most once a minute, after a look: the stray sandboxes no stop saw (jail_end). Its own time is bounded,
        so a slow sweep never delays the next look; its kills run in this thread, never beside the alarm's own."""
        from . import jail_end
        if self.swept is not None and clock() - self.swept < jail_end.SWEEP_SECONDS:
            return
        self.swept = clock()
        try:
            jail_end.sweep(self.service)
        except Exception as error:  # a sweep never stops the alarm; the next minute tries again
            self.service.log('error', step='jail-sweep', error=repr(error))
