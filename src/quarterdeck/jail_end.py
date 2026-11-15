"""A seat's sandbox ends whole: its tree at the stop, a hold where it does not end, and a sweep for a stray one.

A jail is an outer bwrap and, in a pid namespace of its own, an inner bwrap, that namespace's pid 1, which runs the
seat's program. The pane's hangup, or the relay's forwarded signal, ends the outer one. The inner one follows only by
the parent-death signal it arms while starting, and a kill in that moment leaves it running unseen, under whatever
reaps orphans (measured on 15 November: 1 kill in 60 by the pane, 1 in 60 through the relay, under load). So:

- the stop ends the launched program's whole tree, read before its window is killed, and every orphan of the seat,
  through the write bound's own kill, and confirms them gone (write_bound.alive). What still runs at the kill's round
  limit holds the seat: its launch stays, no path launches it again, and the status line names it, until the
  service's tick finds the held processes ended;
- the sweep, in the write bound's own thread at most once a minute, ends any orphan the stop never saw, and says so to
  the systems seats and the status line, with what it killed.

An orphan is a bwrap whose writable binds name a seat folder of this ship (runtime/seats/<seat>) and which descends
from no live seat's launched program and no live pane of the ship's own tmux server. It is judged by its ancestry,
never by its parent's name, and killed by pid and start, both read again just before the kill. A jail still starting
lies under its pane or its relay, so it is never one; the old ship and test runs bind no folder of this ship.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import time

from . import durable, trusted_files, write_bound
from .ship import ShipError

SWEEP_SECONDS = 60.0  # the sweep runs at most this often, after a look of the write bound
SWEEP_BUDGET = 2.0  # a sweep past this on the clock stops, and the next one starts over
DAY = timedelta(days=1)  # how long a sweep stands in the status line, and the window for its second alarm
EVIDENCE = 'jail-sweep'  # runtime/evidence/jail-sweep/: one record per stray sandbox ended


def _now() -> datetime:
    return datetime.now(timezone.utc)


# -- the processes ---------------------------------------------------------------------------------------------------


def _read(path: str) -> bytes | None:
    try:
        with open(path, 'rb') as stream:
            return stream.read()
    except OSError:
        return None


def _stat(pid: int) -> tuple[int, str, str] | None:
    """(parent pid, start in ticks since boot, state) of a process, or None once it has gone."""
    raw = _read(f'/proc/{pid}/stat')
    if raw is None:
        return None
    fields = raw.rpartition(b')')[2].split()
    if len(fields) <= 19 or not fields[1].isdigit():
        return None
    return int(fields[1]), fields[19].decode(), fields[0].decode()


def _seat_of(runtime: Path, argv: list[str]) -> str | None:
    """The seat a jail's command line binds the folder of, in this ship; None for any other command."""
    options = argv[:argv.index('--')] if '--' in argv else argv
    if not argv or os.path.basename(argv[0]) != 'bwrap' or '--unshare-pid' not in options:
        return None
    seats = {str(Path(runtime) / 'seats'), os.path.realpath(Path(runtime) / 'seats')}
    for bind in write_bound._binds(argv):
        parent, name = os.path.split(bind.rstrip('/'))
        if parent in seats and name and not name.startswith('.'):
            return name
    return None


def live_roots(runtime: Path, tmux) -> set[int] | None:
    """The processes no orphan descends from: every seat's live launched program, the ship's own tmux server, and
    every pane of it. Every pane, and so every jail starting in one, descends from the server; a real orphan never
    does, since it goes to a subreaper or to the user's manager. None when the server or its panes cannot be read:
    then nothing is an orphan on this reading."""
    roots: set[int] = set()
    base = trusted_files.root(runtime)
    for folder in sorted(base.iterdir()) if base.is_dir() else []:
        try:
            launch = json.loads((folder / trusted_files.LAUNCH).read_text())
        except (OSError, ValueError):
            continue
        pid, start = launch.get('pid') if isinstance(launch, dict) else None, (launch or {}).get('start')
        if isinstance(pid, int):
            found = _stat(pid)
            if found is not None and (start is None or found[1] == _ticks(start)):
                roots.add(pid)  # a launch with no recorded start is taken as live while its pid runs: never killed
    try:
        server = tmux.server_pid()
        roots.update(pane.pid for pane in tmux.panes())
    except Exception:  # the ship's server unreadable: no reading of ancestry is safe
        return None
    if not isinstance(server, int) or server <= 1:
        return None  # a server whose pid cannot be read proves no pane's ancestry
    roots.add(server)
    return roots


def _ticks(start) -> str | None:
    """A launch's recorded start, as /proc gives it: ticks since boot, as text."""
    return str(start) if start is not None else None


def orphans(runtime: Path, roots: set[int], *, seat: str | None = None, deadline: float | None = None,
            clock=time.monotonic) -> tuple[list[tuple[str, int, str]], bool]:
    """Every orphan's topmost bwrap of this ship's seats (or of one seat): (seat, pid, start). Each process's name
    is read first and only a bwrap's arguments are parsed. The flag is False where the deadline cut the reading."""
    uid = os.getuid()
    candidates: dict[int, tuple[str, str, int]] = {}  # pid -> (seat, start, parent)
    for name in os.listdir('/proc'):
        if deadline is not None and clock() > deadline:
            return [], False
        if not name.isdigit() or _read(f'/proc/{name}/comm') != b'bwrap\n':
            continue
        pid = int(name)
        try:
            if os.stat(f'/proc/{name}').st_uid != uid:
                continue
        except OSError:
            continue
        found, raw = _stat(pid), _read(f'/proc/{name}/cmdline')
        if found is None or raw is None:
            continue
        owner = _seat_of(runtime, [part.decode(errors='replace') for part in raw.split(b'\0')[:-1]])
        if owner is not None and (seat is None or owner == seat):
            candidates[pid] = (owner, found[1], found[0])
    result = []
    for pid, (owner, start, parent) in sorted(candidates.items()):
        if parent in candidates:
            continue  # inside another bwrap of the seat: that one's tree takes it
        if not rooted(pid, roots):
            result.append((owner, pid, start))
    return result, True


def rooted(pid: int, roots: set[int]) -> bool:
    """Whether the process, or any of its ancestors, is a live root. The candidate itself first: a seat's outer
    bwrap is its pane's own process (launch.sh execs it) and its launched program, so its own pid is the root."""
    ancestors, at = set(), pid
    while at > 1 and at not in ancestors:
        if at in roots:
            return True
        ancestors.add(at)
        found = _stat(at)
        if found is None:
            return False
        at = found[0]
    return False


def ticks_now() -> int | None:
    """Now, as a process's start is counted: clock ticks since boot (/proc/uptime), or None where unreadable."""
    raw = _read('/proc/uptime')
    try:
        return int(float(raw.split()[0]) * os.sysconf('SC_CLK_TCK')) if raw else None
    except (ValueError, OSError):
        return None


# -- the stop and its hold -------------------------------------------------------------------------------------------


def snapshot(launched) -> dict[int, str]:
    """The launched program's whole tree, read before its window is killed: by then the inner bwrap is a child."""
    if launched is None or not launched.start:
        return {}
    return write_bound.tree(launched.pid, str(launched.start))


@contextmanager
def stopping(trusted: Path):
    """While a seat's own stop ends its sandbox, the sweep leaves that seat to it."""
    trusted.mkdir(mode=0o700, parents=True, exist_ok=True)
    own = _stat(os.getpid())
    durable.replace_file(trusted / trusted_files.JAIL_STOPPING,
                         json.dumps(dict(pid=os.getpid(), start=own[1] if own else None)) + '\n')
    try:
        yield
    finally:
        (trusted / trusted_files.JAIL_STOPPING).unlink(missing_ok=True)


def stopping_now(trusted: Path) -> bool:
    """A stop of the seat is ending its sandbox now: its marker names a process that still runs."""
    try:
        data = json.loads((trusted / trusted_files.JAIL_STOPPING).read_text())
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True  # unreadable: left alone, never swept on a guess
    found = _stat(data.get('pid')) if isinstance(data, dict) and isinstance(data.get('pid'), int) else None
    return found is not None and found[1] == data.get('start') and found[2] not in ('Z', 'X')


def end(seat, tree: dict[int, str]) -> dict[int, str]:
    """End the seat's old sandbox: the tree read before the window kill, and every orphan of the seat, each with its
    tree. Returns what still runs after the write bound's kill, by pid and start; empty when all is gone."""
    pids = dict(tree)
    roots = live_roots(seat.ship.runtime, seat.tmux)
    if roots is not None:
        found, _ = orphans(seat.ship.runtime, roots, seat=seat.name)
        for _, pid, start in found:
            pids.update(write_bound.tree(pid, start))
    if not pids:
        return {}
    write_bound.kill(pids)
    return write_bound.alive(pids)


def hold(trusted: Path, seat: str, left: dict[int, str]) -> None:
    """Hold the seat on what still runs; written by the stop itself, in the trusted folder no jail sees."""
    trusted.mkdir(mode=0o700, parents=True, exist_ok=True)
    durable.replace_file(trusted / trusted_files.JAIL_HELD, json.dumps(dict(
        seat=seat, at=_now().isoformat(), pids={str(pid): start for pid, start in sorted(left.items())}),
        sort_keys=True) + '\n')


def held(trusted: Path) -> dict | None:
    """The hold, or None. A hold that cannot be read still holds: its processes are then unknown."""
    try:
        data = json.loads((trusted / trusted_files.JAIL_HELD).read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return dict(pids=None)
    pids = data.get('pids') if isinstance(data, dict) else None
    if not isinstance(pids, dict) or not all(key.isdigit() and isinstance(value, str) for key, value in pids.items()):
        return dict(pids=None)
    return dict(data, pids={int(key): value for key, value in pids.items()})


def words(seat: str, data: dict) -> str:
    """The same count of processes for the status line and for a refused launch."""
    pids = data.get('pids')
    if pids is None:
        return f'{seat}: its old sandbox hold is unreadable; held, and nothing is launched beside it'
    count = len(pids)
    return (f"{seat}: its old sandbox did not end ({count} process{'' if count == 1 else 'es'} still running); "
            'held, and nothing is launched beside it until they have ended')


def retry(trusted: Path) -> bool | None:
    """The tick: end the held processes again. True once none runs and the hold is lifted; False while held;
    None when there is no hold. Never lifted by age, nor where its processes are unknown."""
    data = held(trusted)
    if data is None:
        return None
    if data['pids'] is None:
        return False
    write_bound.kill(data['pids'])
    left = write_bound.alive(data['pids'])
    if left:
        if left != data['pids']:
            durable.replace_file(trusted / trusted_files.JAIL_HELD, json.dumps(dict(
                data, pids={str(pid): start for pid, start in sorted(left.items())}), sort_keys=True) + '\n')
        return False
    (trusted / trusted_files.JAIL_HELD).unlink(missing_ok=True)
    return True


# -- the sweep -------------------------------------------------------------------------------------------------------


def _boot() -> float | None:
    for line in (_read('/proc/stat') or b'').splitlines():
        if line.startswith(b'btime '):
            return float(line.split()[1])
    return None


def _ran(start: str) -> float | None:
    boot = _boot()
    try:
        return max(0.0, time.time() - (boot + int(start) / os.sysconf('SC_CLK_TCK'))) if boot is not None else None
    except (ValueError, OSError):
        return None


def sweep(service, *, budget: float = SWEEP_BUDGET, clock=time.monotonic) -> list[dict]:
    """End every orphan of this ship's seats, each confirmed gone, recorded and told. Bounded on the clock: past the
    budget it stops, logs so, and the next sweep starts over."""
    ship = service.ship
    runtime = ship.runtime
    deadline = clock() + budget
    # A seat launched after its roots are read has a pane those roots miss: its jail would read as rooted nowhere.
    # So what started at or after this moment (one second's margin before it) is never a target on this sweep.
    read_at = ticks_now()
    roots = live_roots(runtime, service.tmux)
    if roots is None:
        service.log('jail-sweep', outcome='unknown', reason="the ship's server or its panes could not be read; nothing swept")
        return []
    found, whole = orphans(runtime, roots, deadline=deadline, clock=clock)
    if not whole:
        service.log('jail-sweep', outcome='cut', reason=f'past {budget:g}s on the clock; the next sweep starts over')
        return []
    ended = []
    for seat, pid, start in found:
        if clock() > deadline:
            service.log('jail-sweep', outcome='cut', reason=f'past {budget:g}s on the clock; the next sweep starts over')
            break
        trusted = trusted_files.folder(runtime, seat)
        try:
            if read_at is None or int(start) >= read_at - os.sysconf('SC_CLK_TCK'):
                continue  # started as the roots were read, or after: its pane may be one they missed
        except (ValueError, OSError):
            continue
        if stopping_now(trusted):
            continue  # the seat's own stop is ending it now
        hold_data = held(trusted)
        if hold_data is not None and (hold_data['pids'] is None or pid in hold_data['pids']):
            continue  # the hold owns it, and the status line already names it
        if not write_bound.alive({pid: start}):
            continue  # read again just before the kill: ended, or the pid is another process's now
        again = live_roots(runtime, service.tmux)
        if again is None or rooted(pid, again):
            continue  # the roots read again just before the kill: rooted now, or unreadable, it is left
        pids = write_bound.tree(pid, start)
        if not pids:
            continue
        ran = _ran(start)
        write_bound.kill(pids)
        left = write_bound.alive(pids)
        record = dict(seat=seat, pid=pid, start=start, ran_seconds=None if ran is None else round(ran),
                      program_running=len(pids) > 1, killed=sorted(pids), confirmed=not left, left=sorted(left),
                      at=_now().isoformat())
        _record(runtime, seat, record)
        service.log('jail-swept', **record)
        ended.append(record)
    return ended


def _record(runtime: Path, seat: str, record: dict) -> None:
    """Into the evidence, one file each, and into the seat's trusted record of the last day's sweeps."""
    folder = Path(runtime) / 'evidence' / EVIDENCE
    folder.mkdir(parents=True, exist_ok=True)
    stamp = record['at'].replace(':', '').replace('-', '').split('.')[0]
    durable.replace_file(folder / f"{seat}-{stamp}-{record['pid']}.json", json.dumps(record, sort_keys=True) + '\n')
    trusted = trusted_files.folder(runtime, seat)
    trusted.mkdir(mode=0o700, parents=True, exist_ok=True)
    kept = [entry for entry in recent(runtime, seat) if entry.get('at') != record['at']] + [record]
    durable.replace_file(trusted / trusted_files.JAIL_SWEPT, json.dumps(kept, sort_keys=True) + '\n')


def recent(runtime: Path, seat: str, now: datetime | None = None) -> list[dict]:
    """The seat's sweeps within the last day, oldest first."""
    try:
        data = json.loads(trusted_files.path(runtime, seat, trusted_files.JAIL_SWEPT).read_text())
    except (OSError, ValueError):
        return []
    now = now or _now()
    kept = []
    for entry in data if isinstance(data, list) else []:
        try:
            if isinstance(entry, dict) and now - datetime.fromisoformat(entry['at']) < DAY:
                kept.append(entry)
        except (KeyError, TypeError, ValueError):
            continue
    return kept


def _duration(seconds) -> str:
    if not isinstance(seconds, int):
        return 'for an unknown time'
    if seconds < 120:
        return f'for {seconds}s'
    if seconds < 7200:
        return f'for {seconds // 60}m'
    return f'for {seconds // 3600}h'


def describe(seat: str, entry: dict, count: int) -> str:
    """What was killed, in the status line's words: the seat, the pid and its start, how long it ran, whether its
    program ran, and whether its end was confirmed."""
    at = entry.get('at', '')[11:16]
    program = 'its program running' if entry.get('program_running') else 'no program in it'
    gone = '' if entry.get('confirmed') else '; not confirmed gone'
    again = f'; {count} in the last day' if count > 1 else ''
    return (f"{seat}: a stray sandbox was ended at {at}Z (pid {entry.get('pid')}, start {entry.get('start')}, "
            f"ran {_duration(entry.get('ran_seconds'))}, {program}){gone}{again}")


def status(ship) -> list[str]:
    """Every seat's latest sweep within the last day, for the status line (law: nothing-waits-silently)."""
    base = trusted_files.root(ship.runtime)
    lines = []
    for folder in sorted(base.iterdir()) if base.is_dir() else []:
        entries = recent(ship.runtime, folder.name)
        if entries:
            lines.append(describe(folder.name, entries[-1], len(entries)))
    return lines


def unproved(runtime: Path, seat: str, since: datetime | None = None) -> str | None:
    """Why the seat's sandbox is not proved ended: held at its stop, or a stray one of it swept since ``since``.
    None when neither. A screen that meets either is never clean."""
    data = held(trusted_files.folder(runtime, seat))
    if data is not None:
        return words(seat, data)
    for entry in recent(runtime, seat):
        try:
            if since is None or datetime.fromisoformat(entry['at']) >= since:
                return describe(seat, entry, 1)
        except (KeyError, TypeError, ValueError):
            return f'{seat}: a sweep record of its sandbox is unreadable'
    return None


def _held_reason(seat) -> str | None:
    """The hold, as relaunch_intent reads every hold that keeps a seat down."""
    data = held(trusted_files.folder(seat.ship.runtime, seat.name))
    return words(seat.name, data) if data is not None else None


def alarm(runtime: Path, seat: str) -> tuple[str | None, str | None, bool]:
    """(key, words, second) for the seat's sweep alarm: the latest sweep within the day, and whether it is the
    second or later in that day, which goes to security as well."""
    entries = recent(runtime, seat)
    if not entries:
        return None, None, False
    latest = entries[-1]
    return (f"{latest.get('at')}/{latest.get('pid')}", describe(seat, latest, len(entries))
            + f'; evidence in runtime/evidence/{EVIDENCE}/', len(entries) > 1)


from . import relaunch_intent  # noqa: E402  (registered once, as the write bound's hold is)

if _held_reason not in relaunch_intent.HOLDS:
    relaunch_intent.HOLDS.append(_held_reason)
