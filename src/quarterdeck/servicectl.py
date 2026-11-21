"""Control of the service process: its heartbeat, its lock, start and stop, and requests to it.

One service owns a ship, held by an exclusive lock for its whole life, and the lock is the
one fact about whether it exists: the kernel drops it the moment the process dies. The
heartbeat only says how recently it ticked; a service that holds the lock and has not
ticked lately is busy, never dead. The CLI never types or relaunches a working seat itself:
it files a request here and waits for the service's answer.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import stat
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import durable, lock_holders, stop_barrier
from .ship import Ship, ShipError
from .procstat import EXITED

STALE_SECONDS = 10.0  # a tick longer than this reads as busy
# A main loop with no new stamp for this long is stuck: the line alerts. Above the longest legitimate duty in the
# service log's timings, deliveries at 56.8 s (and the longest whole pass, 184.7 s, is a run of many duties, each
# stamped as it begins), by a margin of five times.
LOOP_STUCK_SECONDS = 300.0
# The main loop's stamp reads busy only past this. The routine status work between duties, the longest stretch a
# healthy loop stamps, ran a median 4.5 s a call, 12.7 s at the 90th percentile and 17.6 s at the 99th, over 2,699
# timing rows of 15 to 17 November (its longest call 35.7 s): 30 s sits above the routine run, so an ordinary pass reads
# ok and only an unusual one reads busy, by its own name. The beat's own staleness stays STALE_SECONDS.
BUSY_SECONDS = 30.0
JAILED_HEARTBEAT_SECONDS = 120.0  # inside a jail, which cannot see the lock: a service that ticked this recently is up
ANSWER_SECONDS = 8.0
EXIT_SETTLE = 2.0  # after the lock frees, how long stop waits for the kernel to mark the process gone
START_SECONDS = 30.0
LIFECYCLE_REQUESTS = frozenset({'relaunch', 'stop', 'select', 'control'})


HEARTBEAT_FOLDER = "heartbeat"


def heartbeat_path(runtime: Path) -> Path:
    """In a folder of its own, which a jail shows read-only: a replace inside it never races a jail's mount, and a
    running jail sees every beat. A single-file bind pinned the inode a jail started with, so a jail read its
    launch-time beat for ever, and a replace during bwrap's mount of the file stopped the launch.

    runtime/heartbeat is shown to every jail, so nothing but the service's two beats (this one, from a thread of
    its own, and the main loop's pass stamp, pass_path), and each one's own hidden temporary while it is written
    (post_dir.temporary_name), may ever be written there."""
    return runtime / HEARTBEAT_FOLDER / "service.heartbeat"


def pass_path(runtime: Path) -> Path:
    """The main loop's own stamp, beside the beat: the process may be alive while its loop is stuck in one duty."""
    return runtime / HEARTBEAT_FOLDER / "service.pass"


# Where the heartbeat lay before it moved into its folder. The copy that moved it (11ca0ea6) beat here too, for the
# installers before it; this code never writes or reads it, and only clears what that copy left.
EARLIER_HEARTBEAT = "service.heartbeat"


def remove_heartbeat(runtime: Path) -> None:
    """The beat and the pass stamp, with any temporary a write cut short left (each writer's own, named after its
    target; and the one temporary the earlier code shared), and what a copy that also beat at the earlier place left
    there. A service's start and a stop both run it, so a temporary a crash left never outlives the next start."""
    for path in (heartbeat_path(runtime), pass_path(runtime), runtime / EARLIER_HEARTBEAT):
        path.unlink(missing_ok=True)
        path.with_suffix(".tmp").unlink(missing_ok=True)
    folder = heartbeat_path(runtime).parent
    for name in (heartbeat_path(runtime).name, pass_path(runtime).name):
        for leftover in folder.glob(f".{name}.*.tmp"):
            leftover.unlink(missing_ok=True)


def lock_path(runtime: Path) -> Path:
    """The service's lock, under runtime/locks, which every jail hides: a jailed seat cannot hold it."""
    from . import ship_locks
    return ship_locks.path(runtime, "service.lock")


def legacy_lock_path(runtime: Path) -> Path:
    """Where the lock lay before it moved. A service of the earlier code may still hold it during a sync, so it
    counts as held while it exists; the first service of this code to start removes it."""
    return runtime / "service.lock"


def write_heartbeat(runtime: Path) -> None:
    """One beat, whole by rename, in the heartbeat's folder."""
    path = heartbeat_path(runtime)
    path.parent.mkdir(exist_ok=True)
    _beat(path)


def write_pass(runtime: Path, duty: str, *, now: datetime | None = None, pid: int | None = None) -> None:
    """The main loop's stamp, whole by rename: its pid, the time it began this duty, and the duty's name."""
    path = pass_path(runtime)
    path.parent.mkdir(exist_ok=True)
    from .post_dir import temporary_name
    tmp = path.with_name(temporary_name(path.name))  # its own: the beat thread writes beside it at any moment
    moment = (now or datetime.now(timezone.utc)).isoformat()
    tmp.write_text(json.dumps(dict(pid=pid or os.getpid(), at=moment, duty=duty)) + "\n")
    os.replace(tmp, path)


def read_pass(runtime: Path, *, now: datetime | None = None) -> tuple[int, float, str] | None:
    """The main loop's pid, the age of its last stamp in seconds, and the duty it was in; None when unreadable."""
    try:
        data = json.loads(pass_path(runtime).read_text())
        moment = datetime.fromisoformat(data["at"])
        if (type(data["pid"]) is not int or data["pid"] <= 0 or moment.tzinfo is None
                or not isinstance(data["duty"], str) or not data["duty"]):
            return None
        age = (now or datetime.now(timezone.utc)).timestamp() - moment.timestamp()
        return data["pid"], age, data["duty"]
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return None


def _beat(path: Path) -> None:
    from .post_dir import temporary_name
    tmp = path.with_name(temporary_name(path.name))  # its own: the main loop stamps its pass beside it at any moment
    tmp.write_text(f"{os.getpid()} {datetime.now(timezone.utc).isoformat()}\n")
    os.replace(tmp, path)


def read_heartbeat(runtime: Path, *, now: datetime | None = None) -> tuple[int, float] | None:
    """The service's pid and the age of its last tick in seconds, or None when none was written.

    Only the folder is read. Every copy a sync can return to beats there: the copy that moved the beat (11ca0ea6) and
    every later one; a service clears a beat it did not write at its start (Service.first_beat)."""
    return _read_beat(heartbeat_path(runtime), now=now)


def _read_beat(path: Path, *, now: datetime | None = None) -> tuple[int, float] | None:
    try:
        pid, stamp = path.read_text().split()
        moment = datetime.fromisoformat(stamp)
        if int(pid) <= 0 or moment.tzinfo is None:
            return None
        age = (now or datetime.now(timezone.utc)).timestamp() - moment.timestamp()
        return int(pid), age
    except (OSError, ValueError, OverflowError):
        return None


TAKE_SECONDS = 1.0  # how long a start keeps trying a lock that a reader of an earlier copy may hold for an instant


def _take(descriptor: int) -> None:
    """Take an exclusive flock without waiting on it, trying again for up to TAKE_SECONDS while it is held.

    This code's readers never take the lock (``lock_holders``), but a reader of an earlier copy still probes by taking it
    for an instant, and a start that met such a probe would otherwise refuse a lock nobody keeps. A lock still held
    at the end raises BlockingIOError, as one attempt does; any other error is raised at once."""
    deadline = time.monotonic() + TAKE_SECONDS
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise
        time.sleep(0.02)


def acquire_lock(runtime: Path):
    """Take the ship's service lock for this process's life, or refuse.

    The lock lies under runtime/locks. Its earlier place is taken too while it exists, and removed once held:
    no service of the earlier code can then be running, and no jail can make that file again."""
    from . import ship_locks
    path = lock_path(runtime)
    ship_locks.prepare(path)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    handle = os.fdopen(descriptor, "a")
    try:
        _take(handle.fileno())
    except OSError:
        handle.close()
        raise ShipError(f"another service holds {path}") from None
    try:
        _retire_legacy(runtime)
    except BaseException:
        handle.close()
        raise
    return handle


def _retire_legacy(runtime: Path) -> None:
    legacy = legacy_lock_path(runtime)
    try:
        descriptor = os.open(legacy, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return
    except OSError as error:
        raise ShipError(f"the earlier service lock {legacy} cannot be opened ({error.strerror}); retained") from None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ShipError(f"the earlier service lock {legacy} is not a regular file; retained")
        try:
            _take(descriptor)
        except BlockingIOError:
            raise ShipError(f"a service of the earlier code, or a seat that can see it, holds {legacy}; "
                            "stop that service (or that seat) and start again") from None
        legacy.unlink()
    finally:
        os.close(descriptor)


def hold_legacy(runtime: Path) -> int:
    """Hold the service lock's earlier place as well, for an installer of the earlier code that started this service.

    That installer confirms its service by the earlier place alone (``runtime/service.lock``), and rolls back a
    service it cannot see. So the service it started holds that place too, without waiting, until the installer has
    exited. Held by anything else, the start is refused, as the earlier lock's holder is refused at any start.
    """
    legacy = legacy_lock_path(runtime)
    try:
        descriptor = os.open(legacy, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as error:
        raise ShipError(f"the earlier service lock {legacy} cannot be opened ({error.strerror}); retained") from None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ShipError(f"the earlier service lock {legacy} is not a regular file; retained")
        try:
            _take(descriptor)
        except BlockingIOError:
            raise ShipError(f"a service of the earlier code, or a seat that can see it, holds {legacy}; "
                            "stop that service (or that seat) and start again") from None
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def release_legacy(runtime: Path, descriptor: int) -> None:
    """Let the earlier place go once its installer has exited: the file is removed while still held, then closed.

    Any failure but the file's absence is raised with the descriptor still open and the lock still held, so the
    caller can try again on its next pass with the same descriptor.
    """
    legacy = legacy_lock_path(runtime)
    try:
        held, current = os.fstat(descriptor), os.lstat(legacy)
        if (held.st_dev, held.st_ino) == (current.st_dev, current.st_ino):
            legacy.unlink()
    except FileNotFoundError:
        pass
    os.close(descriptor)


def _probe(path: Path) -> bool | None:
    """Whether the lock is held, found by taking it for an instant: only where the kernel's table cannot say.
    True when a process holds it, False when it is free, None when there is no such file."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError:
        return True
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        os.close(descriptor)


def _held(path: Path) -> bool | None:
    """True when a process holds the lock, False when it is free, None when there is no such file: read from the
    kernel's table, never by taking the lock, which would make a service starting that instant refuse it. Only where
    the table cannot say is the lock probed by taking it."""
    reading = lock_holders.reading(path)
    return _probe(path) if reading is None else reading[0]


def locked(runtime: Path) -> bool:
    """Whether a service holds the runtime's lock: the one fact that says a service owns this ship.

    Inside a jail runtime/locks is an empty folder of the jail's own, so no lock file is there to read. Where
    neither the lock nor its earlier place exists, a fresh heartbeat is the reading instead: the jail sees
    that file read-only and cannot write it."""
    found = [_held(lock_path(runtime)), _held(legacy_lock_path(runtime))]
    if True in found:
        return True
    if False in found:
        return False
    beat = read_heartbeat(runtime)
    return beat is not None and 0 <= beat[1] <= JAILED_HEARTBEAT_SECONDS


HOLDER_SECONDS = 1.0  # how long a held lock's holder may take to read the same twice in the kernel's table


def _lock_rows(path: Path) -> list[str]:
    """The kernel's lock-table rows for ``path``, for a refusal that must say what it saw."""
    try:
        info = os.stat(path, follow_symlinks=False)
        key = f"{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}:{info.st_ino}"
        return [" ".join(line.split()[1:]) for line in lock_holders.table().splitlines() if key in line]
    except (OSError, UnicodeError) as error:
        return [type(error).__name__]


def holder(runtime: Path) -> int | None:
    """The service: the process that holds the ship's lock, as the kernel lists it, never a heartbeat's word.

    A dead service's last beat outlives it, and a beat at a place a copy no longer writes names whoever wrote last;
    the lock is held by the live service alone. The table is read, never the lock taken (``lock_holders``). A reader of an
    earlier copy probes by taking a free lock for an instant, so a holder counts only when two readings 50 ms apart
    name the same process, and never this process itself, within HOLDER_SECONDS. None when no process holds it, or
    when the table cannot name one: inside a jail, which sees no lock, and for a row whose pid is 0 or less."""
    for path in (lock_path(runtime), legacy_lock_path(runtime)):
        deadline = time.monotonic() + HOLDER_SECONDS
        while True:
            first = lock_holders.reading(path)
            if first is None or not first[0] or first[1] is None or first[1] == os.getpid():
                break  # free, no such file, a table that cannot say, or no holder it names: none at this place
            time.sleep(0.05)
            if lock_holders.reading(path) == first:
                return first[1]
            if time.monotonic() >= deadline:  # a holder the table would not name twice: unknown, never guessed
                break
    return None


def alive(runtime: Path) -> bool:
    """A service owns the ship: its lock is held. A dead process has dropped it; a busy one keeps it."""
    return locked(runtime)


def stale(runtime: Path) -> float | None:
    """How long the owning service has gone without a tick, when longer than a tick should take; else None."""
    beat = read_heartbeat(runtime)
    return beat[1] if beat is not None and beat[1] > STALE_SECONDS else None


def started(runtime: Path) -> bool:
    """A service owns the ship and has beaten: a beat counts only when its pid is the lock's holder, so the last beat
    of a service that died is never read as the start of the next. Where the holder cannot be read (a jail), the beat
    stands alone, as it must there."""
    if not alive(runtime):
        return False
    beat = read_heartbeat(runtime)
    if beat is None:
        return False
    pid = holder(runtime)
    return (pid is None and not lock_path(runtime).exists()) or pid == beat[0]


def process_start(pid: int) -> str | None:
    """The start time of a process that has not exited: an exited one (Z, X, x) proves no identity, so None."""
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return fields[19] if fields[0] not in EXITED else None
    except (OSError, IndexError, UnicodeError):
        return None


def service_identity(runtime: Path) -> dict | None:
    """Only an acknowledgement from the current lock holder is an observed identity."""
    try:
        data = json.loads((runtime / 'service.identity.json').read_text())
        beat = read_heartbeat(runtime)
        if (not isinstance(data, dict) or type(data.get('pid')) is not int
                or not alive(runtime) or beat is None or data['pid'] != beat[0]
                or data.get('process_start') is None
                or data['process_start'] != process_start(data['pid'])):
            return None
        return data
    except (OSError, ValueError, TypeError, UnicodeError):
        return None


def acknowledge(runtime: Path, identity: dict | None, token: str | None) -> None:
    """Called after the service lock, using identity captured when its code loaded."""
    durable.replace_file(runtime / 'service.identity.json', json.dumps(dict(
        pid=os.getpid(), process_start=process_start(os.getpid()), copy=identity, startup_token=token)) + '\n')


def append_log(runtime: Path, event: str, **data) -> None:
    """One line in the service's log, as the service writes its own, from code that holds no Service."""
    log = Path(runtime) / "log" / "service.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as stream:
        stream.write(json.dumps({"at": datetime.now().astimezone().isoformat(timespec="seconds"),
                                 "event": event, **data}) + "\n")


@stop_barrier.guard_start
def start(ship: Ship, qd: Path | None = None, *, expected: dict | None = None,
          startup_token: str | None = None) -> str:
    """Start the service detached, logging under the runtime; a proof line. It runs the ``qd`` given, else this one."""
    from .seat import qd_bin

    if alive(ship.runtime):
        if expected is not None:
            identity = service_identity(ship.runtime)
            wanted = {key: expected.get(key) for key in ('commit', 'instance', 'package_digest')}
            if identity is None or identity.get('copy') != wanted:
                raise ShipError('service already owns the ship but its loaded copy identity is unknown or different')
        beat, late = read_heartbeat(ship.runtime), stale(ship.runtime)
        return f"service alive · pid {beat[0] if beat else '?'}" + (f" · busy, last tick {int(late)}s ago" if late else "")
    log_dir = ship.runtime / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    from .tmux import Tmux, TmuxError

    from . import jail_environment

    # Declared, never inherited: not the caller's seat (QD_SEAT) nor the desktop's secrets, nor any pool's key. Each
    # launch's own launcher reads its key from the key file (key_launch), so the service needs none, and an unjailed
    # seat of the same user could read one in its /proc/<pid>/environ.
    environment = jail_environment.service_environment(os.environ)
    environment['QD_SHIP'] = str(ship.folder)
    from .tmux_socket import private_endpoint

    if (named := os.environ.get('QD_TMUX_SOCKET')) and private_endpoint(named):
        # Declared: a test's or the drill's private namespace, where the ship's own choice of server is that one. Any
        # other binding is the caller's and stays with it: the service finds the ship's server itself.
        try:
            if Tmux.for_ship(ship).socket == named:
                environment['QD_TMUX_SOCKET'] = named
        except TmuxError:
            pass
    environment.pop('QD_COPY_START', None)
    if startup_token:
        environment['QD_COPY_START'] = startup_token
    command = [str(qd or qd_bin() / 'qd'), '--ship', str(ship.folder), 'service']
    scope, unscoped = scoped(ship, environment)
    with (log_dir / 'service.out').open('ab') as out:
        process = subprocess.Popen(
            [*scope, *command],
            stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True,
            cwd=str(ship.folder), env=environment,
        )
    record_for_test_run(ship, process.pid, scope)
    def confirmed():
        if not started(ship.runtime):
            return False
        if expected is None:
            beat = read_heartbeat(ship.runtime)
            return beat is not None and beat[0] == process.pid
        identity = service_identity(ship.runtime)
        return (identity is not None and identity.get('pid') == process.pid
                and identity.get('copy') == {key: expected.get(key) for key in ('commit', 'instance', 'package_digest')}
                and identity.get('startup_token') == startup_token)
    deadline = time.monotonic() + START_SECONDS
    while time.monotonic() < deadline and process.poll() is None and not confirmed():
        time.sleep(0.1)
    if not confirmed():
        # A bootstrap delayed before its lock must not wake after rollback and start stale code.
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        raise ShipError(f'service startup identity/heartbeat not confirmed within {START_SECONDS:.0f}s; '
                        f'see {log_dir / "service.out"}')
    where = f"in its own scope {scope_unit(scope)}" if scope else f"in the caller's scope: {unscoped}"
    return (f"service started · pid {process.pid} · heartbeat {heartbeat_path(ship.runtime).relative_to(ship.folder)}"
            f" · {where}")


SCOPE_PROGRAM = 'systemd-run'
TEST_RUN_RECORDS = 'TESTRUN_SERVICE_RECORDS'  # tests/testrunner/services.py: a test run's folder of the services it started


def record_for_test_run(ship: Ship, pid: int, scope: list[str]) -> None:
    """Under the repository's test tool, record the service just started, so the run stops it before it deletes the
    ship: it runs in a scope and a session of its own, which no test's ending reaches. Written once, at once; the
    interpreter and package that started it are named, so its own stop can be run through them."""
    folder = os.environ.get(TEST_RUN_RECORDS)
    if not folder:
        return
    import sys
    record = dict(ship=str(ship.folder), runtime=str(ship.runtime), pid=pid, start=process_start(pid),
                  unit=scope_unit(scope) if scope else None, python=sys.executable,
                  package=str(Path(__file__).resolve().parent.parent))
    try:
        target = Path(folder)
        target.mkdir(parents=True, exist_ok=True)
        durable.replace_file(target / f'{pid}-{record["start"]}.json', json.dumps(record, sort_keys=True) + '\n')
    except OSError:
        pass  # the run's scan by the ship's path still finds it; a record never stops a start


def scope_unit(scope: list[str]) -> str:
    return next(word.partition('=')[2] for word in scope if word.startswith('--unit='))


def scoped(ship: Ship, environment: dict[str, str], *, kind: str = 'service',
           run=subprocess.run) -> tuple[list[str], str | None]:
    """The words that start the service in a transient systemd scope of its own, named for the ship, so that a stop of
    the caller's scope (the pane that ran qd sync) leaves it running; or none, and why, where the user's service
    manager cannot be reached. systemd-run --scope execs the command itself, so the pid is the service's.

    ``kind`` names what starts there: 'service', or 'tmux-server' for the ship's own terminal server, whose unit
    (qd-tmux-server-*) the stop's service pattern never matches. ``run`` runs the probe."""
    import re
    import secrets
    import shutil

    program = shutil.which(SCOPE_PROGRAM, path=environment.get('PATH'))
    if program is None:
        return [], f"{SCOPE_PROGRAM} is absent"
    name = re.sub(r'[^A-Za-z0-9_.-]', '_', ship.name)[:64] or 'ship'
    unit = f"qd-{kind}-{name}-{secrets.token_hex(4)}.scope"
    what = 'tmux server' if kind == 'tmux-server' else kind
    words = [program, '--user', '--scope', '--quiet', '--collect', '--expand-environment=no', f'--unit={unit}',
             f'--description=qd {what} of {ship.name}', '--']
    try:
        probe = run([*words[:-1], f'--unit=qd-probe-{name}-{secrets.token_hex(4)}.scope', '--', 'true'],
                    env=environment, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as error:
        return [], f"{SCOPE_PROGRAM} --user did not answer ({type(error).__name__})"
    if probe.returncode:
        reason = ' '.join(probe.stderr.split())[:160] or f'exit {probe.returncode}'
        return [], f"{SCOPE_PROGRAM} --user cannot reach the user's service manager ({reason})"
    return words, None


def recorded_owner(runtime: Path, left: dict[int, str]) -> tuple[int, str] | None:
    """The live service's own owner, by pid and start, when an earlier stop recorded it among ``left``; else None.

    Then that stop was interrupted, or runs beside this one, while the service still lives: a stop run again owes
    it SIGTERM and its grace, as a first stop does, never the kill an earlier stop's leftovers get."""
    if not alive(runtime):
        return None
    pid = holder(runtime)
    if pid is None or pid not in left:
        return None
    from . import shutdown
    start = shutdown.process_start(pid)
    return (pid, start) if start is not None and left[pid] == start else None


def stop(runtime: Path) -> str:
    """Signal the lock's owner; confirm its exit, ending its tree if it outlives the grace."""
    previous = stop_barrier.read(runtime)
    told: set[int] = set()  # the tmux servers the line already names
    line = _stop(runtime, previous, told)
    words = stop_barrier.spared_words({pid: start for pid, start in (previous or {}).items() if pid not in told})
    return line + (f" · {words}" if words else "")


def _stop(runtime: Path, previous: dict[int, str] | None, told: set[int]) -> str:
    from . import service_processes, shutdown, write_bound

    def end_tree(known):
        def observe(found):
            known.update(found)
            stop_barrier.retain(runtime, known)
        write_bound.kill(known, observe=observe, spare=service_processes.server)  # never a tmux server's panes

    leftovers_ended = ""
    if previous is not None:
        left = stop_barrier.remaining(previous)
        if left and (live := recorded_owner(runtime, left)) is not None:
            # The earlier stop never finished with the service that still holds the lock. Its own tree and scope
            # take the ordinary path below, SIGTERM and its grace first; only what the earlier stop recorded beyond
            # them, its leftovers, is ended now. A scope that cannot be read ends nothing here: the path below
            # reads it again and refuses as a first stop would.
            owner_pid, owner_start = live
            service = {owner_pid: owner_start}
            try:
                service.update(write_bound.tree(owner_pid, owner_start, spare=service_processes.server))
                service.update(service_processes.capture(owner_pid, owner_start, runtime=runtime).read())
                leftovers = {pid: start for pid, start in left.items() if pid not in service}
            except (OSError, ValueError, IndexError):
                leftovers = {}
            if leftovers:
                end_tree(dict(leftovers))
                if still := stop_barrier.remaining(leftovers):
                    raise ShipError(f"earlier stop's leftovers exit cannot be confirmed: {stop_barrier.names(still)}")
                leftovers_ended = f"an earlier stop's leftovers ended: {stop_barrier.names(leftovers)}"
        elif left:
            bound = None
            for process, began in left.items():
                try:
                    bound = service_processes.capture(process, began, owners=previous)
                    break
                except (OSError, ValueError, IndexError):
                    continue
            if bound is None:
                raise ShipError('earlier service process scope cannot be read; exit cannot be confirmed')
            try:
                previous.update(bound.read())
                stop_barrier.retain(runtime, previous)
            except (OSError, ValueError, IndexError):
                raise ShipError('earlier service process re-read failed; exit cannot be confirmed') from None
            end_tree(previous)
            try:
                previous.update(bound.read())
                stop_barrier.retain(runtime, previous)
            except (OSError, ValueError, IndexError):
                raise ShipError('earlier service process re-read failed; exit cannot be confirmed') from None
            if stop_barrier.remaining(previous):
                raise ShipError(f'earlier service exit cannot be confirmed: {stop_barrier.names(previous)}')
            stop_barrier.require_clear(runtime)
            told.update(bound.servers)
            return (f"service killed · earlier stop's tree confirmed ended: {stop_barrier.names(previous)}"
                    f" · {bound.proof}" + (f" · {bound.spared()}" if bound.servers else ""))
        else:
            stop_barrier.require_clear(runtime)
    if not alive(runtime):
        remove_heartbeat(runtime)
        return "service was not running"
    pid = holder(runtime)  # the lock's holder, never a beat: a dead service's beat names a pid that is not the service
    if pid is None:
        if not alive(runtime):  # it ended between the two readings
            remove_heartbeat(runtime)
            return "service was not running"
        raise ShipError(f"a service holds {lock_path(runtime)} but its holder cannot be read"
                        f" (lock table: {_lock_rows(lock_path(runtime))}); fuser {lock_path(runtime)} names it")
    start = shutdown.process_start(pid)
    identity = dict(pid=pid, start=start)
    if start is None and not shutdown.process_gone(identity):
        raise ShipError(f"service {pid} start cannot be read; exit cannot be confirmed")
    owner = shutdown.owner(runtime, pid)
    if owner is not None and start != owner["start"]:
        owner = None
    known = {pid: start} if start is not None else {}
    try:
        known.update(write_bound.tree(pid, start, spare=service_processes.server))
    except OSError as error:
        raise ShipError(f"service {pid} tree cannot be read ({type(error).__name__}); exit cannot be confirmed") from None
    stop_barrier.retain(runtime, known)
    try:
        process_scope = service_processes.capture(pid, start, runtime=runtime)
    except (OSError, ValueError, IndexError) as error:
        raise ShipError(f'service {pid} process scope cannot be read ({type(error).__name__}); '
                        'exit cannot be confirmed') from None

    def reread_processes():
        try:
            found = process_scope.read()
        except (OSError, ValueError, IndexError) as error:
            raise ShipError(f'service {pid} process re-read failed ({type(error).__name__}); '
                            'exit cannot be confirmed') from None
        known.update(found)
        stop_barrier.retain(runtime, known)

    reread_processes()  # before the first signal, while the owner still proves its scope
    from . import relaunch_intent
    term_started = time.monotonic()
    extended = shutdown.inflight(runtime, owner) is not None
    relaunching = bool(relaunch_intent.held_by(runtime, owner))
    for sig, patience, how in ((signal.SIGTERM, shutdown.ORDINARY_GRACE, "stopped"), (signal.SIGKILL, 2.0, "killed")):
        try:
            if sig == signal.SIGKILL:
                reread_processes()
                end_tree(known)
            elif not shutdown.process_gone(identity):
                os.kill(pid, sig)
        except ProcessLookupError:
            pass
        except OSError as error:
            raise ShipError(f"service {pid} tree could not be ended ({type(error).__name__}); exit cannot be confirmed") from None
        deadline = time.monotonic() + patience
        if sig == signal.SIGTERM and extended:
            deadline = max(deadline, term_started + shutdown.DELIVERY_GRACE)
        if sig == signal.SIGTERM and relaunching:
            deadline = max(deadline, term_started + relaunch_intent.GRACE)
        while time.monotonic() < deadline and locked(runtime):
            if sig == signal.SIGTERM and shutdown.inflight(runtime, owner) is not None:
                extended = True
                deadline = max(deadline, term_started + shutdown.DELIVERY_GRACE)
            if sig == signal.SIGTERM and not relaunching and relaunch_intent.held_by(runtime, owner):
                relaunching = True  # a relaunch section opened as the signal arrived: it is held as well
                deadline = max(deadline, term_started + relaunch_intent.GRACE)
            time.sleep(0.05)
        if not locked(runtime):
            # The lock frees as the process closes its files, a moment before the kernel marks it gone: wait for
            # that event itself, within the same bound, so a slow machine is not read as an owner still alive.
            gone_by = time.monotonic() + EXIT_SETTLE
            while time.monotonic() < gone_by and not shutdown.process_gone(identity):
                time.sleep(0.05)
            reread_processes()  # includes children born during SIGTERM even after their parent ended
            released_alive = how == "stopped" and not shutdown.process_gone(identity)
            remaining = any(not shutdown.process_gone(dict(pid=child, start=began))
                            for child, began in known.items())
            if remaining or released_alive:
                try:
                    end_tree(known)
                except OSError as error:
                    raise ShipError(f"service {pid} tree could not be ended ({type(error).__name__}); exit cannot be confirmed") from None
            reread_processes()  # a new member after the kill holds the proof too
            unconfirmed = [child for child, began in known.items()
                           if not shutdown.process_gone(dict(pid=child, start=began))]
            if unconfirmed or not shutdown.process_gone(identity):
                names = ', '.join(map(str, sorted(set(unconfirmed) | {pid})))
                raise ShipError(f"service {pid} exit cannot be confirmed after ending its tree; processes {names}")
            stop_barrier.require_clear(runtime)
            extended = extended or shutdown.inflight(runtime, owner) is not None
            detail = shutdown.annotate(runtime, owner, forced=how == "killed" or remaining or released_alive)
            stopped_beat = read_heartbeat(runtime)
            if stopped_beat is not None and stopped_beat[0] == pid:
                remove_heartbeat(runtime)
            # A relaunch still open when the service ended: released, logged, and on the line for the next service.
            released = relaunch_intent.release(runtime, owner) if owner is not None else []
            told.update(process_scope.servers)
            bound = max(shutdown.DELIVERY_GRACE if extended else shutdown.ORDINARY_GRACE,
                        relaunch_intent.GRACE if relaunching else 0)
            return (f"service {how} · pid {pid}"
                    + f" · {process_scope.proof}"
                    + (f" · {leftovers_ended}" if leftovers_ended else "")
                    + (f" · {process_scope.spared()}" if process_scope.servers else "")
                    + (" · it released its lock but did not exit; killed with its tree" if released_alive else "")
                    + (" · its remaining children were killed" if remaining and not released_alive and how == "stopped" else "")
                    + (f" · it did not answer SIGTERM within {bound:g}s" if how == "killed" else "")
                    + (f" · waited {time.monotonic() - term_started:.2f}s for delivery boundary (limit {shutdown.DELIVERY_GRACE:g}s)"
                       if extended else "")
                    + (f" · waited {time.monotonic() - term_started:.2f}s for relaunch boundary (limit {relaunch_intent.GRACE:g}s)"
                       if relaunching and not extended else "")
                    + (f" · waited {time.monotonic() - term_started:.1f}s for the service to end"
                       if how == "stopped" and not extended and not relaunching
                       and time.monotonic() - term_started > shutdown.SLOW_STOP else "")
                    + (f" · relaunch of {', '.join(released)} left unfinished for the next service" if released else "")
                    + (f" · {detail}" if detail else ""))
    raise ShipError(f"service {pid} still holds {lock_path(runtime)} after SIGKILL")


def request(runtime: Path, kind: str, **data) -> str:
    """File a request and wait for the service's answer; refuse when no service owns the ship."""
    if not alive(runtime):
        raise ShipError("service down; qd up restarts it")
    from . import request_queue
    name = f"{datetime.now().strftime('%H%M%S')}-{kind}-{os.getpid()}"
    path = request_queue.enqueue(runtime, kind, data, name=name)
    folder, name = path.parent, path.stem
    answer = folder / f"{name}.done"
    deadline = time.monotonic() + ANSWER_SECONDS
    while time.monotonic() < deadline:
        if answer.exists():
            proof = answer.read_text().strip()
            answer.unlink()
            request_queue.acknowledged(path)
            return proof
        time.sleep(0.1)
    late = stale(runtime)
    if answer.exists():
        proof = answer.read_text().strip()
        answer.unlink()
        request_queue.acknowledged(path)
        return proof
    try:
        detail = request_queue.describe(request_queue.view(path))
    except (OSError, ValueError, TypeError, ShipError):
        detail = 'request decision unknown; inspect qd status'
    return (f"pending: the service has not answered within {ANSWER_SECONDS:.0f}s (request {name})"
            + (f"; it last ticked {int(late)}s ago" if late else "") + f'; it applies when it can; {detail}')
