"""The daily live test: one minimal check for each program the ship did not use that day.

The schedule is a user timer of the same kind as the desk's morning report. This module
does not install that timer. Operations installs the text from ``qd daily-live --units``
after the desk agrees, as it does for the other ``qd-*`` units. Adapter-declared
exclusions remain held for the person to decide.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import shlex
import shutil
import stat
import subprocess
import sys
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from . import check_capacity, durable, harness as harnesses, post, program_use, ship as shipmod
from .ship import Ship, ShipError
from .tmux import Tmux, TmuxError

WHEN = "23:00:00"  # the civil day is the one ending; the machine's zone, never a named zone
REPORTED = "daily-live-reported"  # in the ship's runtime: the latest civil day whose report is filed
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
    from .check import check_pool
    pool, refused = check_pool(ship, harnesses.get(program))
    if pool is None:
        return f"{refused or 'no compatible declared pool'}; try again the next night"
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


def covered_day(moment: datetime) -> datetime:
    """The latest civil day whose 23:00 has passed, at its 23:00 in ``moment``'s zone.

    The timer is persistent, so a 23:00 the machine missed runs on its next start, after
    midnight: that catch-up covers the day that ended, never the day just begun.
    """
    moment = _aware(moment)
    hour, minute, second = (int(part) for part in WHEN.split(":"))
    day = moment.date()
    if moment.time().replace(tzinfo=None) < time(hour, minute, second):
        day -= timedelta(days=1)
    return datetime.combine(day, time(hour, minute, second), tzinfo=moment.tzinfo)


def last_reported(ship: Ship) -> tuple[date, str] | None:
    """The latest civil day this ship's daily live test has filed a report for, and that report's id."""
    path = ship.runtime / REPORTED
    try:
        data = json.loads(path.read_text())
        day, report = date.fromisoformat(data["day"]), data["report"]
        if not isinstance(report, str):
            raise ValueError
        return day, report
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, KeyError):
        raise ShipError(f"{path}: the daily live test's record of its last reported day is unreadable; "
                        "nothing was run, so no day is reported twice") from None


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


def exclusions() -> dict[str, str]:
    """Pure adapter metadata; no installed program or provider is called."""
    return {name: reason for name in harnesses.NAMES
            if (reason := harnesses.get(name).daily_live_hold_reason) is not None}


def checkable(name: str) -> bool:
    """A native program that is not excluded from unattended checks."""
    if name not in harnesses.NAMES:
        return False
    try:
        adapter = harnesses.get(name)
        return not adapter.synthetic and adapter.daily_live_hold_reason is None
    except ShipError:
        return False


def plan(readings, *, history_error: str | None, day: datetime) -> list[Choice]:
    """Excluded and used programs are skipped. Unknown use is checked."""
    day = _aware(day)
    found: dict[str, dict] = {}
    for row in readings or ():
        if not isinstance(row, dict):
            continue
        name = row.get("harness")
        if not isinstance(name, str) or not name.strip():
            continue
        found[name] = row
    held = exclusions()
    choices = [Choice(name, "skip", "held", reason) for name, reason in held.items()]
    history_known = history_error is None
    for name in sorted(found, key=str.casefold):
        if name.casefold() in held:
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


def _stage_program_pin(origin: Ship, copied: Ship, program: str) -> None:
    """Give the check its own copy of the verified executable for this program."""
    pin = origin.program_pins.get(program)
    staged = copied.program_pins.get(program)
    if pin is None and staged is None:
        return
    if pin is None or staged is None or (pin.path.name, pin.sha256) != (staged.path.name, staged.sha256):
        raise shipmod.ProgramPinError(f"{program} pin declaration changed while preparing the check. Try again.")

    changed = f"{program} pin {pin.path}: file changed while preparing the check. Restore the declared runtime copy."
    try:
        original = pin.path.lstat()
        pin.verified()
        staged.path.parent.parent.mkdir(mode=0o700)
        staged.path.parent.mkdir(mode=0o700)
        # Reopen without following links, and copy only the inode the origin verifier saw.
        directory = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        with ExitStack() as opened:
            runtime_fd = os.open(origin.runtime, directory)
            opened.callback(os.close, runtime_fd)
            programs_fd = os.open('programs', directory, dir_fd=runtime_fd)
            opened.callback(os.close, programs_fd)
            fd = os.open(pin.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=programs_fd)
            with os.fdopen(fd, 'rb') as source:
                before = os.fstat(fd)
                if not os.path.samestat(original, before) or original.st_ctime_ns != before.st_ctime_ns:
                    raise shipmod.ProgramPinError(changed)
                with staged.path.open('xb') as target:
                    shutil.copyfileobj(source, target)
                    os.fchmod(target.fileno(), 0o500)
                if os.fstat(fd).st_ctime_ns != before.st_ctime_ns:
                    raise shipmod.ProgramPinError(changed)
    except FileNotFoundError:
        raise shipmod.ProgramPinError(
            f"{program} pin {pin.path}: file is missing. Restore the declared runtime copy."
        ) from None
    except OSError as error:
        raise shipmod.ProgramPinError(f"{program} pin {pin.path}: could not stage the executable: {error}") from None


def invoke(origin: Ship, program: str, root: Path, qd: str) -> CheckRun:
    """``qd check PROGRAM --minimal`` with every copied seat's workspace inside the throwaway.

    The workspace sits beside the copied ship, outside its folder and away from the originating project.
    Remove both with the throwaway and its server afterward.
    """
    parent = root / f"qd-daily-live-{secrets.token_hex(8)}"
    parent.mkdir(mode=0o700, parents=True, exist_ok=False)
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
        _stage_program_pin(origin, copied, program)
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
    except (OSError, TmuxError, shipmod.ProgramPinError) as error:
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
    held = exclusions()
    lines.extend(held.values())
    for choice in choices:
        if choice.program.casefold() in held:
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
    """Check what the covered day did not show as used, then one report to the desk.

    The day is ``covered_day``. A day already reported is not run again: nothing is checked
    or filed, and the message is None. Returns the message and whether any check failed.
    """
    moment = covered_day(now if now is not None else current_time())
    to = desk_name(ship)
    reported = last_reported(ship)
    if reported is not None and reported[0] >= moment.date():
        return None, False
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
    # Recorded once the report is filed: a cut between the two can repeat a report, never lose one.
    durable.replace_file(ship.runtime / REPORTED,
                         json.dumps({"day": moment.date().isoformat(), "report": message.id}) + "\n")
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
