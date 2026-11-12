"""qd upgrade: the framework at one commit, installed as the ship's own copy and run by the service.

A ship that runs the development checkout changes under its seats whenever main moves. An
installed copy under the runtime moves only when ``qd upgrade`` is run: the commit named, the
checkout's HEAD by default, is archived from the repository, installed into a virtual environment
of the ship's own, ``runtime/venv``, recorded in ``runtime/installed.json`` with the commit it
replaced, and the service is restarted on it, the seats untouched. From then on every ``qd`` typed
against the ship hands off to the installed copy before it does anything, so the service, the seats
and the status lines run one code, whichever checkout the hand began in. ``qd upgrade`` with the
previous commit rolls back; removing ``runtime/venv`` returns the ship to the checkout.
"""

from __future__ import annotations

import io
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
from uuid import uuid4
from datetime import datetime, timezone
from pathlib import Path

from . import servicectl
from .ship import Ship, ShipError
from .tmux import Tmux

RECORD = "installed.json"


def launch_stamp(path: Path) -> str | None:
    """Bind the complete saved launch without copying its command/brief into requests."""
    if not path.exists():
        return None
    data = json.dumps(json.loads(path.read_text()), sort_keys=True)
    return hashlib.sha256(data.encode()).hexdigest()


def brief_snapshot(ship: Ship) -> dict:
    """Capture the existing offered brief and complete launch before installation/rendering."""
    before, launches = {}, {}
    for name in ship.seats:
        brief = ship.runtime / 'brief' / f'{name}.md'
        launch = ship.runtime / 'seats' / name / 'launch.json'
        before[name] = hashlib.sha256(brief.read_bytes()).hexdigest() if brief.exists() else None
        launches[name] = launch_stamp(launch)
    return dict(before=before, launches=launches)


def queue_rebrief(ship: Ship, commit: str, previous: dict | None, snapshot: dict) -> tuple[Path, dict]:
    """Reuse incomplete work on the current commit; another upgrade is a new event."""
    folder = ship.runtime / 'requests'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f'upgrade-rebrief-{commit}.json'
    if previous and previous['commit'] == commit and path.exists():
        data = json.loads(path.read_text())
    else:
        data = dict(kind='rebrief', commit=commit, token=uuid4().hex,
                    at=datetime.now(timezone.utc).isoformat(), **snapshot)
    path.with_suffix('.done').unlink(missing_ok=True)  # an old unknown-kind answer is not completion
    write_rebrief(path, data)
    return path, data


def write_rebrief(path: Path, data: dict) -> None:
    """Persist notice intent before rendering/posting; reuse the existing request file."""
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data) + '\n')
    os.replace(tmp, path)


def rebrief_result(path: Path, data: dict, timeout: float = servicectl.ANSWER_SECONDS) -> str:
    deadline = time.monotonic() + timeout
    answer = path.with_suffix('.done')
    while time.monotonic() < deadline:
        if answer.exists():
            try:
                result = json.loads(answer.read_text())
                if result.get('token') == data['token'] and result.get('status') == 'complete':
                    return result['proof']
            except (ValueError, AttributeError, KeyError):
                pass
            return 'rebrief incomplete · installed service did not confirm the request'
        time.sleep(0.1)
    return 'rebrief pending · service will retry; no delivery or read is confirmed'


def venv_qd(folder: Path) -> Path | None:
    """The installed copy's ``qd``, where a ship has one."""
    candidate = folder / "runtime" / "venv" / "bin" / "qd"
    return candidate if candidate.exists() else None


def running_from(bin_dir: Path) -> bool:
    """Whether this process runs from ``bin_dir``: the interpreter's own folder, never the system python a venv's
    interpreter is a link to, or an installed copy would hand off to itself without end."""
    return Path(sys.executable).parent.resolve() == bin_dir.resolve()


def handoff(folder: Path, argv: list[str]) -> None:
    """A ship with an installed copy is run by it: ``qd`` typed from any checkout execs the ship's own."""
    installed_qd = venv_qd(folder)
    if installed_qd is not None and not running_from(installed_qd.parent):
        os.execv(str(installed_qd), [str(installed_qd), *argv])


def installed(runtime: Path) -> dict | None:
    """What the ship runs, where it is an installed copy: the commit, when, and the commit before it."""
    path = runtime / RECORD
    return json.loads(path.read_text()) if path.exists() else None


def source_repo(ship: Ship) -> Path:
    """The framework repository: the project folder the ship sits in, else the one this code runs from."""
    import quarterdeck

    for candidate in (ship.folder.parent.parent, Path(quarterdeck.__file__).resolve().parents[2]):
        if (candidate / ".git").exists() and (candidate / "pyproject.toml").exists():
            return candidate
    raise ShipError("no framework repository beside the ship or behind this qd; run qd upgrade from the checkout")


def git(repo: Path, *args: str) -> bytes:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if done.returncode:
        raise ShipError(f"git {' '.join(args)}: {done.stderr.decode().strip()}")
    return done.stdout


def upgrade(ship: Ship, ref: str = "HEAD", tmux: Tmux | None = None) -> list[str]:
    """Install the framework at ``ref`` as the ship's own copy and run the service on it; the proof lines."""
    repo = source_repo(ship)
    commit = git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()
    runtime = ship.runtime
    staging = runtime / "upgrade" / commit
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(git(repo, "archive", "--format=tar", commit))) as archive:
        archive.extractall(staging, filter="data")
    previous = installed(runtime)
    was_running = servicectl.alive(runtime)
    stopped = servicectl.stop(runtime) if was_running else None
    snapshot = brief_snapshot(ship)  # last old-service offer, before live code/cards are replaced
    venv = runtime / "venv"
    if not (venv / "bin" / "python").exists():
        _run([sys.executable, "-m", "venv", "--system-site-packages", str(venv)], "making the ship's own venv")
    _run([str(venv / "bin" / "python"), "-m", "pip", "install", "--quiet", "--no-deps", "--no-build-isolation", str(staging)],
         f"installing {commit[:7]}")
    share = venv / "share" / "quarterdeck"  # the framework's files beside the code: the laws, the cards, the drydock
    for name in ("standing-orders", "ships/drydock", "ships/forum", "ships/cascade"):
        shutil.rmtree(share / name, ignore_errors=True)
        if name in ('ships/forum', 'ships/cascade') and not (staging / name).exists():
            continue  # rolling back to a revision before an optional template
        shutil.copytree(staging / name, share / name)
    shutil.rmtree(runtime / "upgrade", ignore_errors=True)
    version = subprocess.run([str(venv / "bin" / "qd"), "--version"], capture_output=True, text=True).stdout.strip()
    try:  # the installed copy answers for this ship before anything runs on it
        _run([str(venv / "bin" / "qd"), "--ship", str(ship.folder), "laws"], "proving the installed copy answers", timeout=60)
    except ShipError:
        shutil.rmtree(venv, ignore_errors=True)  # a copy that cannot answer is not left where every qd would hand off to it
        raise
    record = {"commit": commit, "ref": ref, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "previous": previous["commit"] if previous else None}
    (runtime / RECORD).write_text(json.dumps(record, indent=2) + "\n")
    rebrief, request = queue_rebrief(ship, commit, previous, snapshot)
    before = f"was {previous['commit'][:7]}" if previous else "the ship ran the checkout before"
    back = f"qd upgrade {previous['commit'][:7]} rolls back" if previous else "removing runtime/venv returns it to the checkout"
    proofs = [f"installed {commit[:7]} ({ref}) into runtime/venv · {version} · {before} · {back}"]
    if was_running:
        proofs.append(stopped)
        proofs.append(servicectl.start(ship, qd=venv / "bin" / "qd"))
        proofs.append(rebrief_result(rebrief, request))
    else:
        proofs.append("service not running; qd up starts it on the installed copy")
        proofs.append('rebrief pending · qd up renders current briefs and posts changed-brief notices')
    if tmux is not None:
        pointed = [s.title for s in ship.sessions.values() if tmux.has_session(s.title)]
        for title in pointed:
            tmux.status_line(title, shlex.join([str(venv / "bin" / "qd"), "--ship", str(ship.folder), "status", "--line"]))
        if pointed:
            proofs.append(f"status lines of {', '.join(pointed)} point at the installed copy")
    proofs.append("every qd typed against this ship now runs the installed copy")
    return proofs


def _run(argv: list[str], doing: str, timeout: float | None = None) -> None:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ShipError(f"{doing} failed: no answer within {timeout:.0f}s") from None
    if done.returncode:
        raise ShipError(f"{doing} failed: {done.stderr.strip()[-400:]}")
