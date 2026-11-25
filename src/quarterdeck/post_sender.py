"""Whether a post with no seat named may go out in the operator's name (law: identity).

``qd post`` signs as the operator, or as ``--from``, only from a shell that is no seat's. A seat's shell normally
says so by QD_SEAT, which its launch sets; a shell that lost it (a sourced script that unsets it, a child started
with a cleared environment) is still the seat's. So before qd post signs for the operator it looks for the seat by
its process ancestry: from this process up, a process that a ship's launch record names as a seat's pane process,
by its process id and its start time together. A bare process id is never an identity: one that matches a record
but started at another time is a reused id, and names nobody. A program that puts its own process id in every
command's environment (PROCESS_MARKERS, measured) gives one more process to join the same way, which finds a
process that has left its pane's tree.

The second signal is independent of the first: a program's own marker in this process's environment (PROGRAMS,
measured on the pinned versions) establishes the program, never a seat. It names a seat only where the program's
measured home or config home, resolved through every link, lies inside the seat folder (runtime/seats/<seat>) of a
seat declared by one of the ships looked at. A marker beside a home anywhere else (the person's own session of the same
program) signs as the operator, as before; so does a home under a seat folder with no marker.

The ships looked at are the ship posted to and its fleet's peers (fleet.declaration_files): the ships of one project
and its office. A seat of a ship outside them is found by no signal. Inside a jail neither /proc's other
processes nor the launch records are visible (measured: the ancestry ends at the jail's own bubblewrap, process 1
there), so a jailed shell is found by QD_SEAT, which its jail sets, by the second signal, or by a third: the jail
itself, a process namespace whose first process is bubblewrap, with HOME, which the jail sets, inside a declared
seat's own folder.

Nothing of another process is read but its parent and its start time from /proc/<pid>/stat, and the name of
process 1 from /proc/1/comm (the jail's bubblewrap, or the machine's init); of this process's own environment, only
the measured markers and homes, and a home's value is never printed.
"""

from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass
from pathlib import Path

from . import trusted_files
from .harness.session_markers import PROCESS_MARKERS, PROGRAMS, Program  # noqa: F401 (Program: for callers)
from .procstat import EXITED

PROC = Path('/proc')
ANCESTRY_LIMIT = 256  # a loop in /proc's answers, or a depth no shell reaches, ends the walk


@dataclass(frozen=True)
class SeatSession:
    """A seat whose session this process runs in: by its pane process (ancestry or a process marker joined to its
    launch record), by a program's marker beside that seat's home (``home`` names the variable, never its value), or
    by its jail (``jail``)."""

    ship: Path  # the ship's folder
    seat: str
    pane: str | None = None
    pid: int | None = None
    home: str | None = None
    jail: bool = False

    def refusal(self, target: Path, sender: str | None) -> str:
        """The words qd post refuses with: which seat's session, and how to post as that seat."""
        named = f"--from {sender}" if sender else "the operator"
        ship_name = self.ship.name
        from .seat_command import qd_command
        assign = f"QD_SHIP={shlex.quote(str(self.ship))} QD_SEAT={shlex.quote(self.seat)}"  # names bare: assignments
        command = f"{assign} {qd_command(self.ship, 'post')} ..."
        across = ("" if Path(os.path.realpath(target)) == Path(os.path.realpath(self.ship))
                  else f"; a seat of {target.name} is addressed from there as <seat>@{target.name}")
        found = ("it runs in that seat's jail" if self.jail
                 else f"pane {self.pane}, process {self.pid}" if self.home is None
                 else f"its program's marker is set, and {self.home} names that seat's home")
        return (f"this shell runs in the session of seat {self.seat} of ship {ship_name} ({found}), "
                f"though QD_SEAT is not set here; a seat posts as itself, never as {named}, "
                f"so nothing was posted. Post as {self.seat}: {command}{across}")


def _stat(pid: int, proc: Path) -> tuple[int, str] | None:
    """A live process's parent and start time, from /proc/<pid>/stat; an exited one, or none, is None."""
    try:
        fields = (proc / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
        if fields[0] in EXITED:
            return None
        return int(fields[1]), fields[19]
    except (OSError, IndexError, ValueError, UnicodeError):
        return None


def ancestry(pid: int, proc: Path = PROC) -> list[tuple[int, str]]:
    """This process and each of its ancestors below the first, oldest last: (process id, start time)."""
    found: list[tuple[int, str]] = []
    seen: set[int] = set()
    while pid > 1 and pid not in seen and len(found) < ANCESTRY_LIMIT:
        seen.add(pid)
        read = _stat(pid, proc)
        if read is None:
            break
        parent, start = read
        found.append((pid, start))
        pid = parent
    return found


def _ship_folders(ship) -> list[Path]:
    from . import fleet

    folders = [Path(ship.folder)]
    try:
        files = fleet.declaration_files(ship)
    except (OSError, ValueError):
        files = ()
    for file in files:
        if (folder := Path(file).parent) not in folders:
            folders.append(folder)
    return folders


def recorded_panes(folders: list[Path]) -> dict[int, list[tuple[Path, str, str, str]]]:
    """Every launch record's pane process in these ships' trusted folders: pid -> [(ship, seat, pane, start)].

    Every seat with a record counts, a temporary or retired one too: the start time decides whether it is live."""
    panes: dict[int, list[tuple[Path, str, str, str]]] = {}
    for folder in folders:
        root = trusted_files.root(folder / 'runtime')
        try:
            seats = sorted(entry for entry in root.iterdir() if entry.is_dir() and not entry.is_symlink())
        except OSError:
            continue
        for seat in seats:
            try:
                data = json.loads((seat / trusted_files.LAUNCH).read_text())
            except (OSError, ValueError, UnicodeError):
                continue
            if not isinstance(data, dict):
                continue
            pid, start, pane = data.get('pid'), data.get('start'), data.get('pane')
            if type(pid) is not int or not isinstance(start, str) or not start or not isinstance(pane, str):
                continue  # a record without its start time joins no process: a bare id is no identity
            panes.setdefault(pid, []).append((folder, seat.name, pane, start))
    return panes


def _joined(panes, process: int, start: str) -> SeatSession | None:
    matches = [(folder, seat, pane) for folder, seat, pane, recorded in panes.get(process, ()) if recorded == start]
    if not matches:
        return None
    folder, seat, pane = matches[0]  # two records claiming one live process name a seat's session all the same
    return SeatSession(folder, seat, pane, process)


def by_pane(ship, *, pid: int | None = None, proc: Path = PROC, environ=None,
            folders: list[Path] | None = None) -> SeatSession | None:
    """The first signal: this process's ancestry, or a program's process marker, joined to a launch record."""
    environ = os.environ if environ is None else environ
    lineage = ancestry(os.getpid() if pid is None else pid, proc)
    marked = []
    for name in PROCESS_MARKERS:
        value = environ.get(name, '')
        if value.isascii() and value.isdigit() and (read := _stat(int(value), proc)) is not None:
            marked.append((int(value), read[1]))
    if not lineage and not marked:
        return None
    panes = recorded_panes(_ship_folders(ship) if folders is None else folders)
    for process, start in (*lineage, *marked):
        if (found := _joined(panes, process, start)) is not None:
            return found
    return None


def _declared(folder: Path, ship) -> tuple[str, ...]:
    if Path(folder) == Path(ship.folder):
        return tuple(ship.seats)
    from . import ship as shipmod
    try:
        return tuple(shipmod.load(folder).seats)
    except (shipmod.ShipError, OSError, ValueError):
        return ()  # an unreadable peer declares nothing: never guessed


def _seat_of_home(value: str, folders: list[Path], ship) -> tuple[Path, str] | None:
    """The declared seat whose own folder holds this home, every link resolved; a relative value proves nothing."""
    if not value or not os.path.isabs(value):
        return None
    try:
        real = Path(os.path.realpath(value))
    except (OSError, ValueError):
        return None
    for folder in folders:
        seats = Path(folder) / 'runtime' / 'seats'
        try:
            if seats.is_symlink():
                continue  # a seat folder elsewhere than its runtime is no evidence
            root = Path(os.path.realpath(seats))
            inside = real.relative_to(root).parts
            runtime = Path(os.path.realpath(Path(folder) / 'runtime'))
            root.relative_to(runtime)
        except (OSError, ValueError):
            continue
        if inside and inside[0] in _declared(folder, ship):
            return Path(folder), inside[0]
    return None


def by_home(ship, *, environ=None, programs: dict[str, Program] | None = None,
            folders: list[Path] | None = None) -> SeatSession | None:
    """The second signal: a measured program marker, and that program's measured home inside a declared seat's own
    folder of the ship posted to or a peer."""
    environ = os.environ if environ is None else environ
    programs = PROGRAMS if programs is None else programs
    marked = [program for program in programs.values() if any(environ.get(name) for name in program.markers)]
    if not marked:
        return None
    folders = _ship_folders(ship) if folders is None else folders
    for program in marked:
        for name in program.homes:
            if (found := _seat_of_home(environ.get(name, ''), folders, ship)) is not None:
                return SeatSession(found[0], found[1], home=name)
    return None


JAIL_FIRST = 'bwrap'  # measured: inside a seat's jail, its own process ids begin with bubblewrap's


def by_jail(ship, *, proc: Path = PROC, environ=None, folders: list[Path] | None = None) -> SeatSession | None:
    """The third signal, where a jail hides the first and a program sets no marker: this process runs in a process
    namespace of its own whose first process is bubblewrap, as every seat's jail does (sandbox: --unshare-pid), and
    HOME, which the jail sets to the seat's home, resolves inside a declared seat's own folder. Measured from inside a
    synthetic jailed seat (tests/integration/test_post_sender_jail.py)."""
    environ = os.environ if environ is None else environ
    try:
        if (proc / '1' / 'comm').read_text().strip() != JAIL_FIRST:
            return None
    except (OSError, UnicodeError):
        return None
    found = _seat_of_home(environ.get('HOME', ''), _ship_folders(ship) if folders is None else folders, ship)
    return None if found is None else SeatSession(found[0], found[1], home='HOME', jail=True)


def seat_session(ship, *, pid: int | None = None, proc: Path = PROC, environ=None,
                 programs: dict[str, Program] | None = None) -> SeatSession | None:
    """The seat whose session this process runs in, by any of the three signals, or None."""
    folders = _ship_folders(ship)
    if (found := by_pane(ship, pid=pid, proc=proc, environ=environ, folders=folders)) is not None:
        return found
    if (found := by_home(ship, environ=environ, programs=programs, folders=folders)) is not None:
        return found
    return by_jail(ship, proc=proc, environ=environ, folders=folders)
