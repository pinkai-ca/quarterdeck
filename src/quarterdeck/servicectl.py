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
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import durable
from .ship import Ship, ShipError

STALE_SECONDS = 10.0  # a tick longer than this reads as busy
ANSWER_SECONDS = 8.0
START_SECONDS = 30.0
LIFECYCLE_REQUESTS = frozenset({'relaunch', 'stop', 'select', 'control'})


def heartbeat_path(runtime: Path) -> Path:
    return runtime / "service.heartbeat"


def lock_path(runtime: Path) -> Path:
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
    """Take the ship's service lock for this process's life, or refuse."""
    handle = lock_path(runtime).open("a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise ShipError(f"another service holds {lock_path(runtime)}") from None
    return handle


def locked(runtime: Path) -> bool:
    """Whether a service holds the runtime's lock: the one fact that says a service owns this ship."""
    try:
        with lock_path(runtime).open("rb") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle, fcntl.LOCK_UN)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return False


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
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
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
    environment = dict(os.environ)
    environment.pop('QD_COPY_START', None)
    if startup_token:
        environment['QD_COPY_START'] = startup_token
    with (log_dir / 'service.out').open('ab') as out:
        process = subprocess.Popen(
            [str(qd or qd_bin() / 'qd'), '--ship', str(ship.folder), 'service'],
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
    return f"service started · pid {process.pid} · heartbeat {heartbeat_path(ship.runtime).relative_to(ship.folder)}"


def stop(runtime: Path) -> str:
    """Signal the service that holds the lock and wait for the lock to drop; one that ignores SIGTERM is killed."""
    if not alive(runtime):
        heartbeat_path(runtime).unlink(missing_ok=True)
        return "service was not running"
    beat = read_heartbeat(runtime)
    if beat is None:
        raise ShipError(f"a service holds {lock_path(runtime)} and has written no heartbeat; fuser {lock_path(runtime)} names it")
    pid = beat[0]
    for sig, patience, how in ((signal.SIGTERM, 3.0, "stopped"), (signal.SIGKILL, 2.0, "killed")):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + patience
        while time.monotonic() < deadline and locked(runtime):
            time.sleep(0.05)
        if not locked(runtime):
            heartbeat_path(runtime).unlink(missing_ok=True)
            return f"service {how} · pid {pid}" + (" · it did not answer SIGTERM within 3s" if how == "killed" else "")
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
