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

from . import durable
from .ship import Ship, ShipError
from .procstat import EXITED

STALE_SECONDS = 10.0  # a tick longer than this reads as busy
JAILED_HEARTBEAT_SECONDS = 120.0  # inside a jail, which cannot see the lock: a service that ticked this recently is up
ANSWER_SECONDS = 8.0
EXIT_SETTLE = 2.0  # after the lock frees, how long stop waits for the kernel to mark the process gone
START_SECONDS = 30.0
LIFECYCLE_REQUESTS = frozenset({'relaunch', 'stop', 'select', 'control'})


def heartbeat_path(runtime: Path) -> Path:
    return runtime / "service.heartbeat"


def lock_path(runtime: Path) -> Path:
    """The service's lock, under runtime/locks, which every jail hides: a jailed seat cannot hold it."""
    from . import ship_locks
    return ship_locks.path(runtime, "service.lock")


def legacy_lock_path(runtime: Path) -> Path:
    """Where the lock lay before it moved. A service of the earlier code may still hold it during a sync, so it
    counts as held while it exists; the first service of this code to start removes it."""
    return runtime / "service.lock"


def write_heartbeat(runtime: Path) -> None:
    path = heartbeat_path(runtime)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(f"{os.getpid()} {datetime.now(timezone.utc).isoformat()}\n")
    os.replace(tmp, path)


def read_heartbeat(runtime: Path) -> tuple[int, float] | None:
    """The service's pid and the age of its last tick in seconds, or None when none was written."""
    try:
        pid, stamp = heartbeat_path(runtime).read_text().split()
        moment = datetime.fromisoformat(stamp)
        if int(pid) <= 0 or moment.tzinfo is None:
            return None
        age = datetime.now(timezone.utc).timestamp() - moment.timestamp()
        return int(pid), age
    except (OSError, ValueError, OverflowError):
        return None


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
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
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


def _held(path: Path) -> bool | None:
    """True when a process holds the lock, False when it is free, None when there is no such file."""
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


def alive(runtime: Path) -> bool:
    """A service owns the ship: its lock is held. A dead process has dropped it; a busy one keeps it."""
    return locked(runtime)


def stale(runtime: Path) -> float | None:
    """How long the owning service has gone without a tick, when longer than a tick should take; else None."""
    beat = read_heartbeat(runtime)
    return beat[1] if beat is not None and beat[1] > STALE_SECONDS else None


def started(runtime: Path) -> bool:
    return alive(runtime) and read_heartbeat(runtime) is not None


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

    # Declared, never inherited: not the caller's seat (QD_SEAT) nor the desktop's secrets.
    environment = jail_environment.service_environment(os.environ, pool_keys(ship))
    environment['QD_SHIP'] = str(ship.folder)
    if named := environment.get('QD_TMUX_SOCKET'):
        # The service keeps a caller's binding only where the ship's own choice of server accepts it (a
        # private test namespace, or the default server the ship still runs on); otherwise it finds the
        # ship's server itself, from the ship, and never runs on another ship's.
        try:
            kept = Tmux.for_ship(ship).socket == named
        except TmuxError:
            kept = False
        if not kept:
            environment.pop('QD_TMUX_SOCKET')
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


def pool_keys(ship: Ship) -> list[str]:
    """The variables the ship file names for its pools' keys (api_key = "env:NAME"): the service reads them."""
    return [pool.api_key.partition(':')[2] for pool in ship.pools.values()
            if pool.api_key and pool.api_key.startswith('env:')]


def scope_unit(scope: list[str]) -> str:
    return next(word.partition('=')[2] for word in scope if word.startswith('--unit='))


def scoped(ship: Ship, environment: dict[str, str]) -> tuple[list[str], str | None]:
    """The words that start the service in a transient systemd scope of its own, named for the ship, so that a stop of
    the caller's scope (the pane that ran qd sync) leaves it running; or none, and why, where the user's service
    manager cannot be reached. systemd-run --scope execs the command itself, so the pid is the service's."""
    import re
    import secrets
    import shutil

    program = shutil.which(SCOPE_PROGRAM, path=environment.get('PATH'))
    if program is None:
        return [], f"{SCOPE_PROGRAM} is absent"
    name = re.sub(r'[^A-Za-z0-9_.-]', '_', ship.name)[:64] or 'ship'
    unit = f"qd-service-{name}-{secrets.token_hex(4)}.scope"
    words = [program, '--user', '--scope', '--quiet', '--collect', '--expand-environment=no', f'--unit={unit}',
             f'--description=qd service of {ship.name}', '--']
    try:
        probe = subprocess.run([*words[:-1], f'--unit=qd-probe-{name}-{secrets.token_hex(4)}.scope', '--', 'true'],
                               env=environment, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as error:
        return [], f"{SCOPE_PROGRAM} --user did not answer ({type(error).__name__})"
    if probe.returncode:
        reason = ' '.join(probe.stderr.split())[:160] or f'exit {probe.returncode}'
        return [], f"{SCOPE_PROGRAM} --user cannot reach the user's service manager ({reason})"
    return words, None


def stop(runtime: Path) -> str:
    """Signal the service that holds the lock and wait for the lock to drop; one that ignores SIGTERM is killed."""
    if not alive(runtime):
        heartbeat_path(runtime).unlink(missing_ok=True)
        return "service was not running"
    beat = read_heartbeat(runtime)
    if beat is None:
        raise ShipError(f"a service holds {lock_path(runtime)} and has written no heartbeat; fuser {lock_path(runtime)} names it")
    pid = beat[0]
    from . import shutdown
    owner = shutdown.owner(runtime, pid)
    if owner is not None and shutdown.process_start(pid) != owner["start"]:
        owner = None
    from . import relaunch_intent
    term_started = time.monotonic()
    extended = shutdown.inflight(runtime, owner) is not None
    relaunching = bool(relaunch_intent.held_by(runtime, owner))
    for sig, patience, how in ((signal.SIGTERM, shutdown.ORDINARY_GRACE, "stopped"), (signal.SIGKILL, 2.0, "killed")):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass
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
            while owner is not None and time.monotonic() < gone_by and not shutdown.process_gone(owner):
                time.sleep(0.05)
            extended = extended or shutdown.inflight(runtime, owner) is not None
            detail = shutdown.annotate(runtime, owner, forced=how == "killed")
            stopped_beat = read_heartbeat(runtime)
            if stopped_beat is not None and stopped_beat[0] == pid:
                heartbeat_path(runtime).unlink(missing_ok=True)
            # A relaunch still open when the service ended: released, logged, and on the line for the next service.
            released = relaunch_intent.release(runtime, owner) if owner is not None else []
            bound = max(shutdown.DELIVERY_GRACE if extended else shutdown.ORDINARY_GRACE,
                        relaunch_intent.GRACE if relaunching else 0)
            return (f"service {how} · pid {pid}"
                    + (f" · it did not answer SIGTERM within {bound:g}s" if how == "killed" else "")
                    + (f" · waited {time.monotonic() - term_started:.2f}s for delivery boundary (limit {shutdown.DELIVERY_GRACE:g}s)"
                       if extended else "")
                    + (f" · waited {time.monotonic() - term_started:.2f}s for relaunch boundary (limit {relaunch_intent.GRACE:g}s)"
                       if relaunching and not extended else "")
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
