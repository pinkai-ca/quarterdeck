"""qd service --settle-delivery: a delivery pointer the next service cannot resolve, moved aside unchanged.

Only the case ``startup_diagnosis`` names: ``runtime/service.delivery`` is there, no service holds the lock, and
``finish`` cannot resolve it (unknown owner stamp, unreadable pointer, identity mismatch, displaced or unreadable
journal, or evidence that overflows). A pointer ``finish`` would resolve is refused: a start clears it.

The proofs are read while this act holds the service lock (taken nonblocking, never written, so no start can
race it) and the stop guard:

1. The lock is free in the kernel's table, and the act then takes it. A failure to take it refuses.
2. ``barrier_settle``'s (b) and (c), called, not copied: no live process of this user in a
   ``qd-service-<ship>-*.scope``, and none outside the ship's tmux tree and this act's own ancestors carries
   ``QD_SHIP`` naming this ship. The same non-dumpable reading; names only.
3. No live process of this user holds an open descriptor to the lock, by the lock path's inode or by the device
   and inode the stamp names when those integers read. ``/proc/<pid>/fd`` is checked by stat, not by readlink
   text, so a renamed or deleted lock inode still counts. A process whose open files this user cannot see (its
   descriptor directory cannot be listed, or none of its descriptor symlinks can be followed) is named, by pid
   and process name, and is not a holder, unless it sits in any service's scope (``qd-service-*``), which
   refuses, or the kernel's lock table shows a flock on one of those inodes, which refuses whoever holds it.
   A descriptor that was listed beside others this user could state, and that one cannot be stated, refuses:
   it might be the lock.
4. Where the stamp reads, ``process_gone`` as a stop uses it. Where it does not read, the record says so. The
   stamp is never required, and it never overrides 1 to 3.
5. The pointer's stamp (device, inode, size, ctime) is the same at the move as at the first reading.

Its limit, the same one accepted for the barrier: a process that cleared its environment, left the scope and
closed the lock is not seen. One whose descriptors this user cannot read, and that left every service scope,
is named and not treated as a holder when the lock table shows it holding no flock.

What it may move is one file, ``runtime/service.delivery``. Opened with ``O_NOFOLLOW``, it must be a regular
file of at most 4096 bytes, and it is moved unchanged into ``runtime/evidence/delivery-boundary/<utc>/``. Its
bytes are never parsed for a path to follow. The lock's stamp may be copied beside it, never moved or rewritten.
The journal the pointer names is not edited, cancelled, deleted or resent, and neither are the heartbeat, the
lock, the attempts or a human-wake reservation.

Before the move the act records itself in ``runtime/trusted/delivery-boundary.jsonl`` (and, when the pointer
names a declared seat, that seat's ``acts.jsonl``, as set-aside does). The next service honours a settled mark,
or a settling mark whose pointer is already gone: the named journal is outcome uncertain and is never typed
again. A prepared journal that was never pasted is not cancelled. A refusal before that line writes no hold.

Only a systems seat may run it, or the operator's own shell, and never from inside a jail. Spock tells the
captain and the desk, as the orders already say for set-aside; this act does not post.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import barrier_settle, servicectl, ship_locks, shutdown, trusted_files
from .ship import Ship, ShipError

EVIDENCE = 'delivery-boundary'
MARK = 'delivery-boundary.jsonl'
LIMIT = 4096
HOLD_DETAIL = 'delivery boundary settled; outcome uncertain; never resent'
WHAT = 'settles the delivery boundary'


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def mark_path(runtime: Path) -> Path:
    """The ship-level mark the next service honours. A file, never a directory: a directory under
    ``runtime/trusted`` is a seat."""
    return trusted_files.root(runtime) / MARK


def _jailed() -> str | None:
    """A caller inside a jail, or one that cannot tell. The barrier act is unchanged.

    ``nested_pid_namespace`` sees a namespace whose proc still shows the outer pid. The ship's own jail mounts a
    fresh proc (``sandbox``), so that reading shows only the inner pid; there pid 1 is bwrap (``jail_end``).
    """
    from .lock_holders import nested_pid_namespace
    if nested_pid_namespace():
        return _JAIL
    try:
        comm = (barrier_settle.PROC / '1' / 'comm').read_text()
    except OSError:
        return _JAIL  # cannot tell
    if comm == 'bwrap\n':
        return _JAIL
    return None


_JAIL = ('refused: this act runs inside a jail; only a systems seat or the operator, outside a jail, '
         'settles the delivery boundary')


def _shape(path: Path) -> str | None:
    """Why the pointer is not a regular file of at most 4096 bytes, or None. Nothing is read for a path."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return f'no delivery pointer at {path}'
    except OSError as error:
        return f'the delivery pointer cannot be read ({error.strerror})'
    if stat.S_ISLNK(info.st_mode):
        return 'the delivery pointer is a symlink'
    if stat.S_ISFIFO(info.st_mode):
        return 'the delivery pointer is a fifo'
    if not stat.S_ISREG(info.st_mode):
        return 'the delivery pointer is not a regular file'
    if info.st_size > LIMIT:
        return 'the delivery pointer is oversize'
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        if error.errno == errno.ELOOP:
            return 'the delivery pointer is a symlink'
        return f'the delivery pointer cannot be opened ({error.strerror})'
    else:
        try:
            opened = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    if not stat.S_ISREG(opened.st_mode):
        return 'the delivery pointer is not a regular file'
    if opened.st_size > LIMIT:
        return 'the delivery pointer is oversize'
    return None


def _take(path: Path) -> int:
    """The service lock, exclusive and nonblocking, opened and never written. The caller closes it."""
    descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(errno.EINVAL, 'not a regular file')
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current = os.lstat(path)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise OSError(errno.EAGAIN, 'changed while taken')
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _stamped_inode(runtime: Path) -> tuple[int, int] | None:
    """Device and inode the lock file names, even when ``owner`` rejects the stamp. None when those integers
    do not read."""
    read = shutdown._read(servicectl.lock_path(runtime), LIMIT)
    if read is None:
        return None
    data, _info = read
    if not isinstance(data, dict):
        return None
    device, inode = data.get('device'), data.get('inode')
    if type(device) is not int or type(inode) is not int or device < 0 or inode < 0:
        return None
    return device, inode


def _lock_inodes(runtime: Path) -> set[tuple[int, int]]:
    """The current lock, the legacy lock, and the stamped inode when the stamp names one."""
    found = set()
    for path in (servicectl.lock_path(runtime), servicectl.legacy_lock_path(runtime)):
        try:
            info = os.lstat(path)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode):
            found.add((info.st_dev, info.st_ino))
    stamped = _stamped_inode(runtime)
    if stamped is not None:
        found.add(stamped)
    return found


def _device_text(device: int) -> str:
    return f'{os.major(device):02x}:{os.minor(device):02x}'


def _flock_holders(targets: set[tuple[int, int]]) -> tuple[set[int], str | None]:
    """Pids the kernel's lock table shows holding an exclusive flock on one of ``targets``.

    The table can omit a held row for three readings back to back (``lock_holders``), so this reads it four times
    and keeps every pid it showed. A read that fails returns why, and no pids.
    """
    from .lock_holders import table
    wanted = {(_device_text(device), str(inode)) for device, inode in targets}
    found: set[int] = set()
    for _ in range(4):
        try:
            lines = table().splitlines()
        except (OSError, UnicodeError):
            return set(), "the kernel's lock table cannot be read"
        for line in lines:
            fields = line.split()
            if len(fields) < 6 or fields[1:4] != ['FLOCK', 'ADVISORY', 'WRITE']:
                continue
            where = fields[5].rsplit(':', 1)
            if len(where) != 2 or (where[0], where[1]) not in wanted:
                continue
            try:
                found.add(int(fields[4]))
            except ValueError:
                return set(), "the kernel's lock table names a holder that is not a pid"
    return found, None


def _service_unit(pid: int, cgroup_of) -> str | None:
    """A ``qd-service-*`` unit this process sits in, or None. Unreadable is not a scope."""
    from . import service_processes
    try:
        path = cgroup_of(pid)
    except (OSError, StopIteration, UnicodeError, ValueError):
        return None
    return next((part for part in Path(str(path)).parts if service_processes.UNIT.fullmatch(part)), None)


def _comm_of(pid: int, proc: Path) -> str:
    try:
        return (proc / str(pid) / 'comm').read_text().rstrip('\n')
    except (OSError, UnicodeError):
        return '?'


def lock_descriptors(runtime: Path, *, cgroup_of=barrier_settle._cgroup, proc: Path = barrier_settle.PROC
                     ) -> tuple[dict[int, str], dict[int, str], dict[int, str], dict[int, str], str | None]:
    """Who holds the lock open, and who this user cannot check.

    Returns holders (a stated descriptor matches a lock inode, or the kernel's table shows their flock), processes
    whose descriptor directory this user cannot read and that sit in no service scope (named, not a refusal), those
    that sit in a ``qd-service-*`` scope, processes with a descriptor that was listed but could not be stated, and
    why the lock table or ``/proc`` could not be read.

    ``/proc/<pid>/fd`` is stated, not read as a link, so a renamed or deleted inode still matches. This act's own
    pid is left out: it holds the lock it took.
    """
    targets = _lock_inodes(runtime)
    empty: dict[int, str] = {}
    if not targets:
        return empty, {}, {}, {}, None
    flocks, table_why = _flock_holders(targets)
    if table_why:
        return empty, {}, {}, {}, table_why
    me = os.getpid()
    held = {pid: ('outside this namespace' if pid <= 0 else _comm_of(pid, proc))
            for pid in flocks if pid != me}
    named: dict[int, str] = {}
    scoped: dict[int, str] = {}
    unstated: dict[int, str] = {}
    try:
        entries = list(proc.iterdir())
    except OSError:
        return held, named, scoped, unstated, 'proc cannot be listed'
    uid = os.getuid()
    from .procstat import EXITED
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        pid = int(entry.name)
        if pid == me:
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            state = (entry / 'stat').read_text().rsplit(')', 1)[1].split()[0]
            comm = (entry / 'comm').read_text().rstrip('\n')
        except (OSError, IndexError, UnicodeError):
            continue
        if state in EXITED:
            continue
        folder = entry / 'fd'
        try:
            descriptors = list(folder.iterdir())
        except PermissionError:
            _hide(pid, comm, cgroup_of, named, scoped)
            continue
        except OSError:
            continue
        stated = False
        denied = False
        for descriptor in descriptors:
            try:
                info = os.stat(descriptor)  # the open file, not the readlink text: a deleted inode still matches
            except PermissionError:
                denied = True  # the symlink is there; following it is not this user's
                continue
            except OSError:
                continue
            stated = True
            if (info.st_dev, info.st_ino) in targets:
                held[pid] = comm
                break
        else:
            if denied and stated:
                unstated[pid] = comm  # one descriptor might be the lock, and the others were not
            elif denied:
                _hide(pid, comm, cgroup_of, named, scoped)  # none of its open files can be stated
    return held, named, scoped, unstated, None


def _hide(pid: int, comm: str, cgroup_of, named: dict[int, str], scoped: dict[int, str]) -> None:
    """A process whose open files this user cannot see. In a service scope that refuses; otherwise it is named."""
    unit = _service_unit(pid, cgroup_of)
    if unit is not None:
        scoped[pid] = f'{comm} in {unit}'
    else:
        named[pid] = comm


def _case(runtime: Path) -> tuple[str | None, str | None]:
    """(problem, refusal). ``problem`` is the settle case. ``refusal`` is why this pointer is not that case."""
    try:
        if shutdown.finish_would_resolve(runtime):
            return None, 'a start clears it'
        problem = shutdown._startup_problem(runtime)
    except (OverflowError, RecursionError):
        return 'unreadable delivery evidence', None
    if problem is not None:
        return problem, None
    expected = shutdown.owner(runtime)
    if expected is not None and not shutdown.process_gone(expected):
        return None, 'the service owner the stamp names is still alive'
    return None, 'the pointer is not a boundary the next service cannot resolve'


def _named_journal(runtime: Path) -> tuple[str | None, str | None]:
    """The relative journal the pointer names, and its kind. The file is not opened. None when it does not parse."""
    data = shutdown._pointer_data(runtime)
    if data is None:
        return None, None
    return data['journal'], data.get('kind', 'pending')


def _seat_of(journal: str | None) -> str | None:
    """The seat a pending journal or a human-wake reservation names, from the path alone."""
    if not journal:
        return None
    parts = Path(journal).parts
    if len(parts) == 4 and parts[:2] == ('post', 'pending'):
        return parts[2]
    if len(parts) == 3 and parts[0] == trusted_files.TRUSTED and parts[2] == trusted_files.HUMAN_WAKE:
        return parts[1]
    return None


def _append(path: Path, line: str) -> None:
    """Append one line. A link is refused, and so is anything that is not a regular file."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise OSError(errno.ELOOP, 'a link on the way to the delivery boundary record')
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise OSError(errno.ELOOP, 'the delivery boundary record is a link') from None
        raise
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(errno.EINVAL, 'the delivery boundary record is not a regular file')
        os.write(descriptor, line.encode())  # before the move, as set-aside's acts.write: no forced sync of its own
    finally:
        os.close(descriptor)


def _record(ship: Ship, entry: dict) -> None:
    """The mark, then the same line in the seat's acts when the pointer names a declared seat.

    Only the ship-level mark is required. A seat copy that cannot be written leaves the mark standing.
    """
    _append(mark_path(ship.runtime), json.dumps(entry, ensure_ascii=False) + '\n')
    seat_name = _seat_of(entry.get('journal'))
    if seat_name is None or seat_name not in ship.seats:
        return
    from types import SimpleNamespace
    from . import acts
    seat = SimpleNamespace(name=seat_name, trusted=trusted_files.folder(ship.runtime, seat_name))
    try:
        acts.write(seat, entry)
    except (OSError, ShipError):
        return


def _rows(runtime: Path) -> list[dict]:
    path = mark_path(runtime)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return []
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
            return []
        raw = os.read(descriptor, info.st_size)
    finally:
        os.close(descriptor)
    rows = []
    for line in raw.decode(errors='replace').splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _final(runtime: Path) -> dict[str, dict]:
    """One row per act: a later line replaces the outcome."""
    merged: dict[str, dict] = {}
    for row in _rows(runtime):
        key = row.get('act')
        if not isinstance(key, str) or row.get('verb') != 'delivery-boundary':
            continue
        if key not in merged:
            merged[key] = dict(row)
        else:
            merged[key]['outcome'] = row.get('outcome')
            if 'journal' in row:
                merged[key]['journal'] = row.get('journal')
    return merged


def _honoured(runtime: Path) -> list[dict]:
    pointer_gone = not os.path.lexists(shutdown.pointer_path(runtime))
    found = []
    for row in _final(runtime).values():
        outcome = row.get('outcome')
        if outcome == 'settled' or outcome == 'settling' and pointer_gone:
            found.append(row)
    return found


def held_journal(runtime: Path, relative: str) -> bool:
    """Whether the next service must hold this runtime-relative journal: outcome uncertain, never typed again."""
    if not isinstance(relative, str) or not relative or relative.startswith('/') or '..' in Path(relative).parts:
        return False
    return any(row.get('journal') == relative for row in _honoured(runtime))


def held_path(runtime: Path, path: Path) -> bool:
    try:
        relative = Path(path).relative_to(runtime).as_posix()
    except ValueError:
        return False
    return held_journal(runtime, relative)


def message_held(runtime: Path, seat_name: str, message_id: str) -> bool:
    """Whether a resend of this message would retype a journal the mark holds. The service's check, not the act's."""
    prefix = f'{message_id}-'
    pending = f'post/pending/{seat_name}/'
    for row in _honoured(runtime):
        journal = row.get('journal')
        if not isinstance(journal, str):
            continue
        name = Path(journal).name
        if name == message_id or name.startswith(prefix):
            return True
        if not journal.startswith(pending):
            continue
        try:
            descriptor = os.open(runtime / journal, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            continue
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > shutdown.MAX_JOURNAL_BYTES:
                continue
            data = json.loads(os.read(descriptor, info.st_size) or b'null')
        except (OSError, ValueError):
            continue
        finally:
            os.close(descriptor)
        if isinstance(data, dict) and (data.get('message') == message_id
                                       or any(isinstance(item, str) and item.startswith(prefix)
                                              for item in data.get('attempts') or ())):
            return True
    return False


def _copy_stamp(runtime: Path, folder: Path) -> bool:
    """Copy the lock's bytes beside the moved pointer. Never move or rewrite the lock. False when it could not."""
    source = servicectl.lock_path(runtime)
    try:
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > LIMIT:
            return False
        raw = os.read(descriptor, LIMIT)
    finally:
        os.close(descriptor)
    target = folder / 'service.lock'
    try:
        out = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError:
        return False
    try:
        os.write(out, raw)
    finally:
        os.close(out)
    return True


def _seen_note(skipped: str, described: str) -> str:
    """Names only: processes whose environment or descriptor directory this user could not read."""
    unread = f' · environment unreadable to this user, skipped: {skipped}' if skipped else ''
    shown = f' · descriptors unreadable to this user, named: {described}' if described else ''
    return unread + shown


def _pids(scope, found, holders) -> list[dict]:
    rows = {}
    for pid, (comm, _unit) in scope.items():
        rows[pid] = comm
    rows.update(found)
    rows.update(holders)
    return [{'pid': pid, 'comm': comm} for pid, comm in sorted(rows.items())]


def _log(runtime: Path, event: str, **data) -> None:
    servicectl.append_log(runtime, event, **data)


def settle(ship: Ship, tmux, caller: str | None, *, cgroup_of=barrier_settle._cgroup, proc=barrier_settle.PROC,
           environ_of=barrier_settle._environ) -> str:
    """Move a delivery pointer the next service cannot resolve. Else refuse, and move nothing."""
    if refusal := barrier_settle.authority(ship, caller, what=WHAT):
        return refusal
    if refusal := _jailed():
        return refusal
    runtime = ship.runtime
    who = caller or 'the operator'
    pointer = shutdown.pointer_path(runtime)
    with ship_locks.held(ship_locks.path(runtime, barrier_settle.GUARD), 'delivery boundary settling'):
        if why := _shape(pointer):
            _log(runtime, 'delivery-boundary-settle-refused', caller=who, problem=why, proofs=[], pids=[])
            return f'refused: {why}; nothing was moved'
        first = shutdown._pointer_stamp(pointer)
        if first is None:
            return 'refused: the delivery pointer is gone; nothing was moved'
        seen: list[str] = []
        proofs: list[str] = []
        if why := barrier_settle.lock_free(runtime):
            seen.append(f'(1) {why}')
        lock_fd = None
        if not seen:
            try:
                lock_fd = _take(servicectl.lock_path(runtime))
            except BlockingIOError:
                seen.append("(1) the service's lock is held")
            except OSError as error:
                seen.append(f"(1) the service's lock cannot be taken ({error.strerror})")
            else:
                proofs.append('lock free and held by this act')
        scope, found, unreadable, holders = {}, {}, {}, {}
        named: dict[int, str] = {}
        scoped_hidden: dict[int, str] = {}
        unstated: dict[int, str] = {}
        problem = None
        try:
            if lock_fd is not None:
                problem, why = _case(runtime)
                if why:
                    seen.append(why)
                scope = barrier_settle.in_scope(ship, cgroup_of=cgroup_of, proc=proc)
                if scope:
                    seen.append('(2) in this ship\'s service scope: ' + ', '.join(
                        f'pid {pid} {comm} in {unit}' for pid, (comm, unit) in sorted(scope.items())))
                else:
                    proofs.append('no scoped service')
                server, unknown = barrier_settle.ship_server(tmux)
                if unknown:
                    seen.append(f'(2) {unknown}')
                elif why := barrier_settle.undumpable():
                    seen.append(f'(2) {why}')
                else:
                    found, unreadable, scoped = barrier_settle.carriers(
                        ship, server, cgroup_of=cgroup_of, proc=proc, environ_of=environ_of)
                    if found:
                        seen.append('(2) outside the ship\'s tmux server, QD_SHIP names this ship in: ' + ', '.join(
                            f'pid {pid} {comm}' for pid, comm in sorted(found.items())))
                    if scoped:
                        seen.append('(2) environment unreadable to this user, in a service\'s scope: ' + ', '.join(
                            f'pid {pid} {where}' for pid, where in sorted(scoped.items())))
                    if not found and not scoped:
                        proofs.append('no QD_SHIP carrier')
                holders, named, scoped_hidden, unstated, table_why = lock_descriptors(
                    runtime, cgroup_of=cgroup_of, proc=proc)
                if table_why:
                    seen.append(f'(3) {table_why}')
                if holders:
                    seen.append('(3) holds the service lock: ' + ', '.join(
                        f'pid {pid} {comm}' for pid, comm in sorted(holders.items())))
                if scoped_hidden:
                    seen.append("(3) descriptors unreadable to this user, in a service's scope: " + ', '.join(
                        f'pid {pid} {where}' for pid, where in sorted(scoped_hidden.items())))
                if unstated:
                    seen.append('(3) a descriptor could not be stated: ' + ', '.join(
                        f'pid {pid} {comm}' for pid, comm in sorted(unstated.items())))
                if not holders and not scoped_hidden and not unstated and not table_why:
                    proofs.append('no lock descriptor')
                expected = shutdown.owner(runtime)
                if expected is None:
                    proofs.append('lock stamp unreadable')
                elif not shutdown.process_gone(expected):
                    seen.append('(4) the lock stamp names a process that is not gone')
                else:
                    proofs.append('owner gone')
                if shutdown._pointer_stamp(pointer) != first:
                    if 'the delivery pointer changed' not in seen:
                        seen.append('the delivery pointer changed')
                elif 'pointer stamp' not in proofs:
                    proofs.append('pointer stamp')
            pids = _pids(scope, {**found, **unreadable, **holders, **named, **scoped_hidden, **unstated}, {})
            skipped = ', '.join(f'pid {pid} {comm}' for pid, comm in sorted(unreadable.items()))
            described = ', '.join(f'pid {pid} {comm}' for pid, comm in sorted(named.items()))
            if seen:
                _log(runtime, 'delivery-boundary-settle-refused', caller=who, problem=problem or seen[0],
                     proofs=proofs, pids=pids, seen=seen, environment_unreadable=skipped)
                return 'refused: ' + '; '.join(seen) + '; nothing was moved'
            journal, kind = _named_journal(runtime)
            stamp_word = 'read' if shutdown.owner(runtime) is not None else 'unreadable'
            entry = dict(act=uuid.uuid4().hex[:12], verb='delivery-boundary', at=_now(), caller=who,
                         problem=problem, journal=journal, kind=kind, proofs=proofs, pids=pids,
                         lock_stamp=stamp_word, outcome='settling')
            try:
                _record(ship, entry)
            except (OSError, ShipError) as error:
                _log(runtime, 'delivery-boundary-settle-refused', caller=who, problem=problem, proofs=proofs,
                     pids=pids, seen=[f'the act could not be recorded ({error})'])
                return f'refused: the act could not be recorded ({error}); nothing was moved'
            if shutdown._pointer_stamp(pointer) != first:
                entry = dict(entry, at=_now(), outcome='not settled')
                try:
                    _record(ship, entry)
                except (OSError, ShipError):
                    pass
                _log(runtime, 'delivery-boundary-settle-refused', caller=who, problem=problem, proofs=proofs,
                     pids=pids, seen=['the delivery pointer changed'])
                return 'refused: the delivery pointer changed; nothing was moved'
            folder = runtime / 'evidence' / EVIDENCE / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            folder.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            folder.mkdir(mode=0o700)
            destination = folder / 'service.delivery'
            if os.path.lexists(destination):
                raise ShipError(f'{destination} exists already; nothing was moved')
            os.rename(pointer, destination)
            copied = _copy_stamp(runtime, folder)
            entry = dict(entry, at=_now(), outcome='settled')
            try:
                _record(ship, entry)
            except (OSError, ShipError) as error:
                copied_note = '' if copied else '; the lock stamp was not copied'
                _log(runtime, 'delivery-boundary-settled', caller=who, problem=problem, proofs=proofs, pids=pids,
                     journal=journal, moved=str(destination), lock_stamp=stamp_word, record=str(error),
                     environment_unreadable=skipped, descriptors_unreadable=described)
                return (f'delivery boundary moved aside unchanged: {destination} · {problem}'
                        f' · the settled line could not be written ({error}){copied_note}{_seen_note(skipped, described)}')
            _log(runtime, 'delivery-boundary-settled', caller=who, problem=problem, proofs=proofs, pids=pids,
                 journal=journal, moved=str(destination), lock_stamp=stamp_word, lock_stamp_copied=copied,
                 environment_unreadable=skipped, descriptors_unreadable=described)
            note = '' if copied else ' · the lock stamp was not copied'
            return (f'delivery boundary moved aside unchanged: {destination} · {problem}{note}'
                    f'{_seen_note(skipped, described)}')
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
