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


def start(ship: Ship, qd: Path | None = None) -> str:
    """Start the service detached, logging under the runtime; a proof line. It runs the ``qd`` given, else this one."""
    from .seat import qd_bin

    if alive(ship.runtime):
        beat, late = read_heartbeat(ship.runtime), stale(ship.runtime)
        return f"service alive · pid {beat[0] if beat else '?'}" + (f" · busy, last tick {int(late)}s ago" if late else "")
    log_dir = ship.runtime / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    out = (log_dir / "service.out").open("ab")
    process = subprocess.Popen(
        [str(qd or qd_bin() / "qd"), "--ship", str(ship.folder), "service"],
        stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True, cwd=str(ship.folder),
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not started(ship.runtime):
        time.sleep(0.1)
    if not started(ship.runtime):
        raise ShipError(f"service did not take its lock and write a heartbeat within 5s; see {log_dir / 'service.out'}")
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
    folder = runtime / "requests"
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{datetime.now().strftime('%H%M%S')}-{kind}-{os.getpid()}"
    path = folder / f"{name}.json"
    durable.replace_file(path, json.dumps({"kind": kind, **data}) + "\n")
    answer = folder / f"{name}.done"
    deadline = time.monotonic() + ANSWER_SECONDS
    while time.monotonic() < deadline:
        if answer.exists():
            proof = answer.read_text().strip()
            answer.unlink()
            return proof
        time.sleep(0.1)
    late = stale(runtime)
    return (f"pending: the service has not answered within {ANSWER_SECONDS:.0f}s (request {name})"
            + (f"; it last ticked {int(late)}s ago" if late else "") + "; it applies when it can")
