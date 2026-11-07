"""Control of the service process: its heartbeat, its lock, start and stop, and requests to it.

One service owns a ship, held by an exclusive lock for its whole life; the heartbeat only
says how recently it ticked. The CLI never types or relaunches a working seat itself: it
files a request here and waits for the service's answer.
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

from .seat import qd_bin
from .ship import Ship, ShipError

STALE_SECONDS = 10.0
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
    except (FileNotFoundError, ValueError):
        return None
    age = datetime.now(timezone.utc).timestamp() - datetime.fromisoformat(stamp).timestamp()
    return int(pid), age


def running(pid: int) -> bool:
    """Whether a process is alive and not a zombie waiting to be reaped."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        return state != "Z"
    except (FileNotFoundError, IndexError):
        pass
    except OSError:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


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
        with lock_path(runtime).open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle, fcntl.LOCK_UN)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return False


def alive(runtime: Path) -> bool:
    """A service owns the ship and has ticked recently; a held lock with a stale heartbeat is a hung one."""
    beat = read_heartbeat(runtime)
    return locked(runtime) and beat is not None and beat[1] <= STALE_SECONDS


def start(ship: Ship) -> str:
    """Start the service detached, logging under the runtime; a proof line."""
    if alive(ship.runtime):
        pid, _ = read_heartbeat(ship.runtime)
        return f"service alive · pid {pid}"
    if locked(ship.runtime):
        beat = read_heartbeat(ship.runtime)
        raise ShipError(f"a service holds {lock_path(ship.runtime)} but last ticked {int(beat[1]) if beat else '?'}s ago; "
                        f"qd down stops it" + (f", or kill {beat[0]}" if beat else ""))
    log_dir = ship.runtime / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    out = (log_dir / "service.out").open("ab")
    process = subprocess.Popen(
        [str(qd_bin() / "qd"), "--ship", str(ship.folder), "service"],
        stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True, cwd=str(ship.folder),
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not alive(ship.runtime):
        time.sleep(0.1)
    if not alive(ship.runtime):
        raise ShipError(f"service did not write its heartbeat within 5s; see {log_dir / 'service.out'}")
    return f"service started · pid {process.pid} · heartbeat {heartbeat_path(ship.runtime).relative_to(ship.folder)}"


def stop(runtime: Path) -> str:
    beat = read_heartbeat(runtime)
    if beat is None or not alive(runtime):
        return "service was not running"
    os.kill(beat[0], signal.SIGTERM)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and running(beat[0]):
        time.sleep(0.05)
    heartbeat_path(runtime).unlink(missing_ok=True)
    return f"service stopped · pid {beat[0]}"


def request(runtime: Path, kind: str, **data) -> str:
    """File a request and wait for the service's answer; refuse when the service is down."""
    if not alive(runtime):
        raise ShipError("service down; qd up restarts it")
    folder = runtime / "requests"
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{datetime.now().strftime('%H%M%S')}-{kind}-{os.getpid()}"
    path = folder / f"{name}.json"
    path.write_text(json.dumps({"kind": kind, **data}) + "\n")
    answer = folder / f"{name}.done"
    deadline = time.monotonic() + ANSWER_SECONDS
    while time.monotonic() < deadline:
        if answer.exists():
            proof = answer.read_text().strip()
            answer.unlink()
            return proof
        time.sleep(0.1)
    return f"pending: the service has not answered within {ANSWER_SECONDS:.0f}s (request {name}); it applies when it can"
