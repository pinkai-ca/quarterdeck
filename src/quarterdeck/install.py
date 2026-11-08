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
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from . import servicectl
from .ship import Ship, ShipError
from .tmux import Tmux

RECORD = "installed.json"


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
    venv = runtime / "venv"
    if not (venv / "bin" / "python").exists():
        _run([sys.executable, "-m", "venv", "--system-site-packages", str(venv)], "making the ship's own venv")
    _run([str(venv / "bin" / "python"), "-m", "pip", "install", "--quiet", "--no-deps", "--no-build-isolation", str(staging)],
         f"installing {commit[:7]}")
    share = venv / "share" / "quarterdeck"  # the framework's files beside the code: the laws, the cards, the drydock
    for name in ("standing-orders", "ships/drydock"):
        shutil.rmtree(share / name, ignore_errors=True)
        shutil.copytree(staging / name, share / name)
    shutil.rmtree(runtime / "upgrade", ignore_errors=True)
    version = subprocess.run([str(venv / "bin" / "qd"), "--version"], capture_output=True, text=True).stdout.strip()
    try:  # the installed copy answers for this ship before anything runs on it
        _run([str(venv / "bin" / "qd"), "--ship", str(ship.folder), "laws"], "proving the installed copy answers", timeout=60)
    except ShipError:
        shutil.rmtree(venv, ignore_errors=True)  # a copy that cannot answer is not left where every qd would hand off to it
        raise
    previous = installed(runtime)
    record = {"commit": commit, "ref": ref, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "previous": previous["commit"] if previous else None}
    (runtime / RECORD).write_text(json.dumps(record, indent=2) + "\n")
    before = f"was {previous['commit'][:7]}" if previous else "the ship ran the checkout before"
    back = f"qd upgrade {previous['commit'][:7]} rolls back" if previous else "removing runtime/venv returns it to the checkout"
    proofs = [f"installed {commit[:7]} ({ref}) into runtime/venv · {version} · {before} · {back}"]
    if servicectl.alive(runtime):
        proofs.append(servicectl.stop(runtime))
        proofs.append(servicectl.start(ship, qd=venv / "bin" / "qd"))
    else:
        proofs.append("service not running; qd up starts it on the installed copy")
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
