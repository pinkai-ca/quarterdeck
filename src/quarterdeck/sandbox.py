"""A seat jailed with bubblewrap: the operator's home hidden, the workspace and the seat's own folder writable.

A model served locally is trusted less than the subscription programs, so a seat on an endpoint pool
runs in a jail unless the ship says ``sandbox = false``, and any seat does with ``sandbox = true``. The
jail is one ``bwrap`` command around the program's own: the root read-only; a private ``/tmp``; the
operator's home replaced by an empty one, with only the folders the program and ``qd`` resolve through
bound back read-only; the workspace and the seat's own folder writable; the ship folder read-only with
the other seats' folders hidden; and the post store writable, so ``qd post`` works from inside. The
program's home is the seat home. The operator's files, sign-ins and the other seats' records are not
merely out of bounds but absent, so nothing the model does inside can read or change them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import standing
from .ship import Ship, ShipError

PROGRAM = "bwrap"
TMP_BYTES = 256 * 1024 * 1024  # a private /tmp lives in memory; this caps what a runaway write can take
HOPS = 20  # a chain of links longer than this is nobody's install
FRAMEWORK_TOOLS = ("qd", "node")  # what a seat's program needs beside itself: qd to post, node where the program is JavaScript


def seats_hidden(argv: object) -> bool | None:
    """Whether a saved managed launch hides sibling seats; malformed evidence is unknown.

    The jail is the outer command, including on launches made before an upgrade.
    Reading one's own launch needs no access to another seat or to the host's pids.
    """
    if (not isinstance(argv, (list, tuple)) or not argv or not argv[0]
            or any(not isinstance(arg, str) for arg in argv)):
        return None
    return Path(argv[0]).name == PROGRAM


def available() -> bool:
    return shutil.which(PROGRAM) is not None


@dataclass(frozen=True)
class Jail:
    """What a jailed seat may see and touch."""

    home: Path  # the operator's home, replaced by an empty one
    seat_home: Path  # the program's home inside the jail
    seat_folder: Path  # runtime/seats/<seat>, writable
    workspace: Path  # writable
    ship_folder: Path  # read-only, the other seats hidden
    runtime: Path
    read_only: tuple[Path, ...] = ()  # the program's own folders under the operator's home, bound back
    writable: tuple[Path, ...] = ()  # the post store, and the check's proof folder when there is one
    environment: tuple[tuple[str, str], ...] = ()  # set inside the jail: the git identity a commit from the seat carries


def git_identity(workspace: Path) -> tuple[tuple[str, str], ...]:
    """The name and address git would sign a commit with in the workspace, read outside the jail.

    Inside, the operator's home is gone and with it the global git configuration, so a commit from
    the seat would have no author. The workspace's identity travels in as the author and committer
    variables, and the commit reads as every other commit of the repository does.
    """
    found = []
    for key, variables in (("user.name", ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME")), ("user.email", ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"))):
        try:
            value = subprocess.run(["git", "-C", str(workspace), "config", "--get", key], capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            value = ""
        found += [(variable, value) for variable in variables if value]
    return tuple(found)


def plan(ship: Ship, seat_name: str, seat_folder: Path, seat_home: Path, workspace: Path, program: str, path: str,
         home: Path | None = None) -> Jail:
    """The jail for one seat, or a refusal naming what would lift it."""
    if not available():
        raise ShipError(f"{seat_name}: sandboxed, and {PROGRAM} is not on the PATH; install bubblewrap, or set sandbox = false on the seat")
    home = (home or Path.home()).resolve()
    workspace = workspace.resolve()
    if home.is_relative_to(workspace):
        raise ShipError(f"{seat_name}: sandboxed with its working directory at {workspace}, which contains the operator home and jails nothing; give the seat a cwd below the home or outside its ancestors")
    if workspace == ship.folder.resolve():
        raise ShipError(f"{seat_name}: sandboxed with the ship folder as its working directory; a jailed seat reads the ship folder and cannot write it")
    folders = [f for f in program_folders((program, *FRAMEWORK_TOOLS), path, home)
               if f != workspace and workspace not in f.parents and ship.folder not in f.parents and f != ship.folder]
    (ship.runtime / "post").mkdir(parents=True, exist_ok=True)  # bound writable, so it must exist before the launch, first post or not
    return Jail(home, seat_home, seat_folder, workspace, ship.folder, ship.runtime, tuple(folders),
                (ship.runtime / "post", ship.runtime / "check"), git_identity(workspace))


def program_folders(programs: tuple[str, ...], path: str, home: Path) -> list[Path]:
    """The folders under the operator's home that the programs resolve through, parents first.

    For each program found on the PATH: the folder of every link in its chain, the install prefix above a
    ``bin`` (not the home itself, nor ``~/.local``, whose ``share`` is the operator's), and the root above a
    ``node_modules``. Then every PATH entry, so the shell finds the operator's tools, and the framework's own
    interpreter, code and files, so ``qd`` runs inside.
    """
    found: set[Path] = set()
    for name in programs:
        file = shutil.which(name, path=path)
        if not file:
            continue
        hop = Path(file)
        for _ in range(HOPS):
            folder = hop.parent
            found.add(folder)
            if folder.name == "bin" and folder.parent not in (home, home / ".local", Path("/")):
                found.add(folder.parent)
            parts = hop.parts
            if "node_modules" in parts:
                found.add(Path(*parts[:parts.index("node_modules")]))
            if not hop.is_symlink():
                break
            target = Path(os.readlink(hop))
            hop = Path(os.path.normpath(target if target.is_absolute() else folder / target))
    found.update(Path(entry) for entry in path.split(os.pathsep) if entry and Path(entry).is_dir())
    found.add(Path(sys.prefix))
    found.add(Path(__file__).resolve().parent.parent)
    found.add(standing.PROJECT_DIR)
    return sorted((f for f in found if home in f.parents), key=lambda f: (len(f.parts), str(f)))


def command(jail: Jail, argv: list[str]) -> list[str]:
    """The ``bwrap`` command line around a program's own."""
    args = [PROGRAM, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
            "--size", str(TMP_BYTES), "--tmpfs", "/tmp", "--tmpfs", str(jail.home)]
    mounts: list[tuple[str, Path]] = [("--ro-bind-try", folder) for folder in jail.read_only]
    mounts += [("--bind", jail.workspace), ("--ro-bind", jail.ship_folder),
               ("--tmpfs", jail.runtime / "seats"), ("--bind", jail.seat_folder)]
    mounts += [("--bind-try", folder) for folder in jail.writable]
    for flag, folder in sorted(mounts, key=lambda m: (len(m[1].parts), str(m[1]))):  # parents first: a child mount is never hidden by its parent's
        args += [flag, str(folder)] if flag == "--tmpfs" else [flag, str(folder), str(folder)]
    for variable, value in jail.environment:
        args += ["--setenv", variable, value]
    # The launch's TMPDIR may name a filesystem the jail mounts read-only; the program
    # uses the jail's own /tmp for scratch, including spooled tool output.
    args += ["--setenv", "TMPDIR", "/tmp", "--setenv", "TMP", "/tmp", "--setenv", "TEMP", "/tmp"]
    return args + ["--setenv", "HOME", str(jail.seat_home), "--unshare-pid", "--die-with-parent", "--", *argv]
