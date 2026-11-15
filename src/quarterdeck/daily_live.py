"""The daily live test: one minimal check for each program the ship did not use that day.

The schedule is a user timer of the same kind as the desk's morning report. This module
does not install that timer. Operations installs the text from ``qd daily-live --units``
after the desk agrees, as it does for the other ``qd-*`` units. Muse is never checked:
a check costs about 3% of its week, and the Admiral decides.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import shlex
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import check_capacity, durable, harness as harnesses, post, program_use, ship as shipmod
from .ship import Ship, ShipError
from .tmux import Tmux, TmuxError

MUSE = "muse"
MUSE_LINE = (
    "Muse was skipped: a check costs it about 3% of its week, so the Admiral decides about Muse"
)
WHEN = "23:00:00"  # the civil day is the one ending; the machine's zone, never a named zone
OUTPUT_LIMIT = 2000

TIMER = f"""\
[Unit]
Description=Quarterdeck: the daily live test of programs the ship did not use

[Timer]
OnCalendar=*-*-* {WHEN}
Persistent=true

[Install]
WantedBy=timers.target
"""


@dataclass(frozen=True)
class Choice:
    program: str
    action: str  # check or skip
    use: str  # used, unused, unknown, held
    why: str


@dataclass(frozen=True)
class CheckRun:
    program: str
    exit_code: int | None
    output: str
    place: str | None = None
    socket: str | None = None
    retained: str | None = None
    not_tested: str | None = None


def wall_reason(ship: Ship, program: str, now: datetime) -> str | None:
    """Read the originating book for the exact first pool qd check will choose.

    No native usage refresh or pool fallback belongs to this nightly preflight.
    A missing book has no recorded wall; an unreadable one cannot establish
    whether this check is safe to start, so it is deferred without spending.
    """
    from .ledger import Ledger
    from .quota import reset_text
    from .usage_cache import parse
    harness = harnesses.get(program)
    pool = next((pool for pool in ship.pools.values() if harness.accepts(pool) is None), None)
    if pool is None:
        return 'no compatible declared pool; try again the next night'
    try:
        book = Ledger.reading(ship.runtime / 'ledger.sqlite')
        if book is None:
            return None
        with book:
            walls = book.walls(ship, now, pool=pool.name)
            if not walls:
                return None
            resets = [reset_text(row.resets_at) for row in book.usage.rows(pool.name)
                      if row.blocks and row.complete and row.used >= 1 and parse(row.resets_at) > now]
            detail = '; '.join(dict.fromkeys([*walls, *resets]))
            return f'pool {pool.name} is at its limit: {detail}; try again the next night'
    except (OSError, sqlite3.Error, ValueError):
        return 'pool limit reading is unavailable; try again the next night'


def machine_zone():
    """The zone the machine is set to, from its localtime link. A fixed offset is the fallback."""
    try:
        target = os.readlink("/etc/localtime")
    except OSError:
        return datetime.now().astimezone().tzinfo
    name = target.replace("\\", "/").split("zoneinfo/")[-1]
    if not name or name == target.replace("\\", "/"):
        return datetime.now().astimezone().tzinfo
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except (OSError, ValueError, KeyError, ModuleNotFoundError):
        return datetime.now().astimezone().tzinfo


def current_time() -> datetime:
    zone = machine_zone()
    try:
        return datetime.now(zone)
    except (TypeError, ValueError, OverflowError):
        return datetime.now().astimezone()


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ShipError("the daily live clock needs a timezone")
    return moment


def classify(row: dict, *, history_known: bool, day: datetime) -> str:
    """``used``, ``unused`` or ``unknown`` for one program on ``day``'s civil date.

    Used is a positive reading only: a seat working now, or a turn that ended on that
    civil date while the history reading is complete. Anything else is not treated as used.
    """
    if row.get("working_now") is True:
        return "used"
    ended = program_use.instant(row.get("last_turn_ended"))
    on_day = ended is not None and ended.astimezone(day.tzinfo).date() == day.date()
    if history_known and on_day:
        return "used"
    stamp = row.get("last_turn_ended")
    stamp_known = stamp in (None, "") or ended is not None
    if history_known and row.get("working_now") is False and stamp_known and not on_day:
        return "unused"
    return "unknown"


def checkable(name: str) -> bool:
    """A program ``qd check`` accepts. Muse is decided by name before this."""
    if name not in harnesses.NAMES:
        return False
    try:
        return not harnesses.get(name).synthetic
    except ShipError:
        return False


def plan(readings, *, history_error: str | None, day: datetime) -> list[Choice]:
    """Muse is skipped by name. Used programs are skipped. Unknown use is checked."""
    day = _aware(day)
    found: dict[str, dict] = {}
    for row in readings or ():
        if not isinstance(row, dict):
            continue
        name = row.get("harness")
        if not isinstance(name, str) or not name.strip():
            continue
        found[name] = row
    choices = [Choice(MUSE, "skip", "held", MUSE_LINE)]
    history_known = history_error is None
    for name in sorted(found, key=str.casefold):
        if name.casefold() == MUSE:
            continue
        use = classify(found[name], history_known=history_known, day=day)
        if use == "used":
            choices.append(Choice(name, "skip", use, "used that day"))
        elif not checkable(name):
            choices.append(Choice(name, "skip", use, "not a program a live check runs"))
        else:
            why = "use unknown" if use == "unknown" else "not used that day"
            choices.append(Choice(name, "check", use, why))
    return choices


def memory_root() -> Path | None:
    """``/dev/shm`` when it is a real writable directory. A link is not accepted."""
    path = Path("/dev/shm")
    try:
        info = path.lstat()
    except OSError:
        return None
    if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode) and os.access(path, os.W_OK | os.X_OK):
        return path
    return None


def ship_root(scratch: Path) -> Path:
    """Throwaway ships live in memory when they can, and in this command's scratch otherwise."""
    found = memory_root()
    if found is not None:
        return found
    scratch.mkdir(parents=True, exist_ok=True)
    return scratch


def _bounded(text: str) -> str:
    text = text.strip()
    if len(text) <= OUTPUT_LIMIT:
        return text
    return text[:OUTPUT_LIMIT] + "\n… truncated"


def _restore(previous: str | None) -> None:
    if previous is None:
        os.environ.pop("TMUX_TMPDIR", None)
    else:
        os.environ["TMUX_TMPDIR"] = previous


SYSTEM_TMP = Path('/tmp')  # what every jail replaces with its own; TMPDIR may name a folder a jail is given

def open_private(ship_parent: Path) -> Tmux:
    """A creator-owned server. An inherited ``TMUX_TMPDIR`` is not where it is created."""
    places: list[Path] = []
    memory = memory_root()
    if memory is not None:
        places.append(memory)
    places.append(ship_parent)
    # Last, a private folder of this user's own under the system's temporary folder: never the project's work/,
    # which jails see through the workspaces bound from it.
    private = SYSTEM_TMP / f"qd-live-tmux-{os.getuid()}"  # the system's own, never TMPDIR's
    try:
        private.mkdir(mode=0o700, exist_ok=True)
        info = private.lstat()
        if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not stat.S_ISLNK(info.st_mode):
            places.append(private)
    except OSError:
        pass
    errors = []
    for place in places:
        try:
            place.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            errors.append(f"{place}: {error}")
            continue
        previous = os.environ.get("TMUX_TMPDIR")
        os.environ["TMUX_TMPDIR"] = str(place)
        try:
            return Tmux.private(place)
        except (TmuxError, OSError) as error:
            errors.append(f"{place}: {error}")
        finally:
            _restore(previous)
    raise TmuxError("no private tmux server; " + "; ".join(errors))


def _child_env(folder: Path, socket: str) -> dict[str, str]:
    env = os.environ.copy()
    for key in ("QD_SEAT", "TMUX", "TMUX_PANE", "TMUX_TMPDIR", "QD_DEFAULT_SHIP"):
        env.pop(key, None)
    env["QD_SHIP"] = str(folder)
    env["QD_TMUX_SOCKET"] = socket
    return env


def start_private(tmux: Tmux) -> None:
    """Only the creator starts the server; a check child is an observer.

    The creator API keeps exit-empty off. Retire the bootstrap session by its
    identity, leaving the empty server available until this creator closes it.
    """
    previous = {key: os.environ.pop(key, None) for key in ('TMUX', 'TMUX_PANE')}
    try:
        bootstrap = tmux.new_session('qd-daily-live-bootstrap')
        tmux.kill_session_id(bootstrap)
    finally:
        for key, value in previous.items():
            if value is not None:
                os.environ[key] = value


def _release(tmux: Tmux | None, parent: Path | None) -> str | None:
    if tmux is not None and not tmux.close():
        return f"private tmux retained: {tmux.held}; ship retained at {parent}"
    if parent is not None:
        shutil.rmtree(parent, ignore_errors=False)
    return None


def invoke(origin: Ship, program: str, root: Path, qd: str) -> CheckRun:
    """``qd check PROGRAM --minimal`` with every copied seat's workspace inside the throwaway.

    The workspace sits beside the copied ship, outside its folder and away from the originating project.
    Remove both with the throwaway and its server afterward.
    """
    parent = root / f"qd-daily-live-{secrets.token_hex(8)}"
    parent.mkdir(parents=True, exist_ok=False)
    tmux = None
    code: int | None = None
    output = ""
    socket = None
    retained = None
    try:
        folder = parent / origin.folder.name
        shutil.copytree(origin.folder, folder, symlinks=True,
                        ignore=shutil.ignore_patterns("runtime", ".git", "__pycache__"))
        copied = shipmod.load(folder)
        workspace = parent / 'workspace'
        workspace.mkdir(mode=0o700)  # a fresh writable folder, never the originating workspace
        declaration = shipmod.edit(copied.file.read_text(), 'ship', 'workspace', str(workspace))
        for name in copied.seats:
            declaration = shipmod.edit(declaration, f'seats.{name}', 'cwd', str(workspace))
        durable.replace_file(folder / 'ship.toml', declaration)
        tmux = open_private(parent)
        start_private(tmux)
        socket = tmux.socket
        completed = subprocess.run(
            [qd, "--ship", str(folder), "check", program, "--minimal"],
            env=_child_env(folder, socket),
            capture_output=True, text=True, check=False,
        )
        code = completed.returncode
        output = _bounded(completed.stdout + completed.stderr)
    except (OSError, TmuxError, shutil.Error) as error:
        code = None
        output = _bounded(f"check did not finish: {error}")
    finally:
        try:
            retained = _release(tmux, parent)
        except OSError as error:
            retained = f"throwaway retained at {parent}: {error}"
    return CheckRun(program, code, output, place=str(parent), socket=socket, retained=retained)


def report_body(day: datetime, choices: list[Choice], results: dict[str, CheckRun],
                history_error: str | None) -> str:
    lines = [f"Daily live test for {day.date().isoformat()}.", ""]
    if history_error:
        lines.append(f"Readings: {history_error}")
        lines.append("")
    lines.append(MUSE_LINE)
    for choice in choices:
        if choice.program.casefold() == MUSE:
            continue
        if choice.action == "skip":
            if choice.use == "used":
                lines.append(f"{choice.program}: used that day; not checked")
            else:
                lines.append(f"{choice.program}: {choice.why}; not checked")
            continue
        run = results[choice.program]
        if run.not_tested:
            lines.append(f'{choice.program}: not tested: {run.not_tested}')
            continue
        if run.exit_code == 0 and not run.retained:
            state = "check passed"
        elif run.exit_code is None:
            state = "check did not finish"
        else:
            state = f"check failed (exit {run.exit_code})"
        lines.append(f"{choice.program}: {choice.why}; {state}")
        if run.retained:
            lines.append(f"  retained: {run.retained}")
        if run.output and (run.exit_code != 0 or run.retained):
            lines.append(run.output)
    return "\n".join(lines) + "\n"


def desk_name(ship: Ship) -> str:
    desks = sorted(name for name, spec in ship.seats.items() if spec.role == "desk")
    if len(desks) != 1:
        found = ", ".join(desks) if desks else "none"
        raise ShipError(f"daily live test needs one seat with role desk; found {found}")
    return desks[0]


def run(ship: Ship, *, now: datetime | None = None, readings=None, history_error: str | None = None,
        runner=None, scratch: Path | None = None, qd: str | None = None):
    """Check what the day did not show as used, then one report to the desk. Returns the message and whether any check failed."""
    moment = _aware(now if now is not None else current_time())
    to = desk_name(ship)
    if readings is None:
        from .seat import seats
        readings, history_error = program_use.collect(ship, seats(ship, Tmux.for_ship(ship), False))
    choices = plan(readings, history_error=history_error, day=moment)
    check = runner or invoke
    results = {}
    for choice in choices:
        if choice.action != "check":
            continue
        try:
            reason = wall_reason(ship, choice.program, now if now is not None else current_time())
            if reason:
                results[choice.program] = CheckRun(choice.program, None, '', not_tested=reason)
                continue
            with check_capacity.reserve(ship) as reason:
                if reason:
                    results[choice.program] = CheckRun(choice.program, None, '', not_tested=reason)
                    continue
                # Finding the common repository can take a few seconds. A wall
                # newly recorded during that work still precedes any launch.
                reason = wall_reason(ship, choice.program, now if now is not None else current_time())
                if reason:
                    results[choice.program] = CheckRun(choice.program, None, '', not_tested=reason)
                    continue
                root = ship_root(scratch or (ship.runtime / 'daily-live'))
                binary = qd or installed_qd(ship)
                results[choice.program] = check(ship, choice.program, root, binary)
        except Exception as error:  # a stand-in or a copy that raises still has to be reported
            results[choice.program] = CheckRun(choice.program, None, _bounded(f"check did not finish: {error}"))
    shipmod.fence(ship)
    message = post.Store(ship.runtime).new(
        "report", post.SHIP, to,
        report_body(moment, choices, results, history_error),
        subject="Daily live test",
    )
    failed = any((item.exit_code != 0 or item.retained) and not item.not_tested for item in results.values())
    return message, failed


def installed_qd(ship: Ship) -> str:
    """The ship's installed command when it has one, otherwise the command now running."""
    candidate = ship.runtime / "venv" / "bin" / "qd"
    if os.access(candidate, os.X_OK):
        return str(candidate)
    invoked = Path(sys.argv[0])
    if invoked.is_file():
        return str(invoked.resolve())
    found = shutil.which("qd")
    return found or str(candidate)


def units(qd: str, folder: Path) -> str:
    """The timer and the oneshot, in the form of the desk's morning report. Nothing is installed."""
    service = f"""\
[Unit]
Description=Quarterdeck: check each program the ship did not use today, and report to the desk

[Service]
Type=oneshot
TimeoutStartSec=infinity
Environment=QD_SHIP={shlex.quote(str(folder))}
UnsetEnvironment=QD_SEAT
ExecStart={shlex.quote(qd)} daily-live
"""
    return TIMER + "\n" + service
