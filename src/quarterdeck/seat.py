"""The Seat: one running harness in one tmux pane under one role, and its lifecycle.

Up, down, restart and fresh are one relaunch path with one difference, whether the program
resumes its record. A seat's identity is its pane id, its process id and its record; the
persona and the window name are display (law: identity).
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import sys
import tempfile
import threading
import time
from contextlib import contextmanager, ExitStack
from typing import Iterator
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from . import providers, durable, harness as harnesses, input_receipt, pending, post, record_owner, ship as shipmod, standing
from . import carriage as carriagemod
from .harness.base import Harness, Join, Launch, _version_in_runtime
from .harness.capabilities import validate_selection
from .provenance import remember_launch, retained_launch
from .pricing import save_price
from . import trusted_cache, trusted_files
from .harness import resume_target
from .ship import Pool, SeatSpec, Ship, ShipError
from .tmux import Pane, Tmux, check_private_folder


PROCESS_END_SECONDS = 10.0  # how long down watches a closed pane's program end before it leaves the rest to the tick
RELAUNCH_END_SECONDS = 60.0  # a relaunch (restart, fresh, a pool move) watches a program end this long in all
RETIRING = trusted_files.RETIRING
TEMPORARY = 'temporary.json'  # in a temporary seat's trusted folder: its raising, for qd down if it is left behind
REFUSED_RELAUNCH = trusted_files.REFUSED_RELAUNCH  # a relaunch refused while the old program was ending, until up succeeds


def process_start(pid: int) -> str | None:
    """A process of this user's, by its start time; any other process, or none, is None."""
    from .servicectl import process_start as start
    try:
        if os.stat(f"/proc/{pid}").st_uid != os.getuid():
            return None  # never another user's process, a kernel thread among them, taken for a seat's program
    except (OSError, ValueError):
        return None
    return start(pid)


def process_alive(pid: int, start: str | None) -> bool:
    """The process recorded at launch is still running: the same pid with the same start time."""
    return start is not None and process_start(pid) == start


def wait_for_end(pid: int, start: str | None, seconds: float) -> bool:
    """Observe, never signal: True once that process has ended within ``seconds``."""
    deadline = time.monotonic() + seconds
    while process_alive(pid, start):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    return True


@dataclass(frozen=True)
class Launched:
    """What ``qd up`` wrote the moment a seat's program started."""

    pane: str
    pid: int
    at: str
    pool: str
    resume: str | None
    argv: list[str]
    version: str | None
    harness: str | None = None
    records_before: list[str] = field(default_factory=list)
    price: dict | None = None
    recovery: dict | None = None
    notice: str | None = None
    answered: list[dict] | None = None  # the account holder's questions the framework answered in this launch
    start: str | None = None  # the program process's start time, read from /proc at launch: its identity with pid

    def write(self, path: Path) -> None:
        data = asdict(self)
        price = data.pop("price")
        if data['recovery'] is None:
            data.pop('recovery')  # unchanged launch shape for programs without this optional proof
        if data['notice'] is None:
            data.pop('notice')
        if data['answered'] is None:
            data.pop('answered')  # unchanged launch shape until a question is answered
        if data['start'] is None:
            data.pop('start')  # unchanged launch shape where the start was not read
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)  # the seat's trusted folder (trusted_files)
        durable.replace_file(path, json.dumps(data, indent=2) + "\n")
        save_price(path, price)

    @property
    def when(self) -> datetime:
        return datetime.fromisoformat(self.at)


def qd_bin() -> Path:
    """The folder holding the ``qd`` the seats will run: beside this interpreter, else on the PATH."""
    beside = Path(sys.executable).parent
    if (beside / "qd").exists():
        return beside
    found = shutil.which("qd")
    return Path(found).parent if found else beside


def program_path() -> str:
    """Find programs on the caller's PATH without adding the framework's interpreter."""
    framework_bin = Path(sys.prefix) / 'bin' if sys.prefix != sys.base_prefix else None
    return os.pathsep.join(entry for entry in os.environ.get('PATH', '').split(os.pathsep)
                           if not framework_bin or Path(entry).resolve() != framework_bin.resolve())


def prepare_program_path(ship: Ship, *, bin_dir: Path | None = None, synthetic: bool = False) -> str:
    """Publish only the framework commands in a ship-owned PATH folder, before launching."""
    source = bin_dir or qd_bin()
    # No import here: during an upgrade this runs while another copy's modules may be the ones on disk.
    elsewhere = next((folder for folder in (source.resolve(), *source.resolve().parents)
                      if folder.name == 'runtime' and (folder.parent / 'ship.toml').is_file()), None)
    own = ship.runtime / 'venv' / 'bin'
    if (elsewhere is not None and elsewhere.resolve() != ship.runtime.resolve()
            and (own / 'qd').is_file() and os.access(own / 'qd', os.X_OK)):
        source = own  # raised from another ship's shell: this ship's seats run this ship's own installed qd
    commands = ship.runtime / 'bin'
    commands.mkdir(parents=True, exist_ok=True)
    names = ['qd', *(['qd-fakeharness'] if synthetic else [])]
    for name in names:
        target = (source / name).resolve()
        if not target.is_file() or not os.access(target, os.X_OK):
            if name != 'qd':
                continue  # the synthetic adapter can also launch through its interpreter
            raise ShipError('qd is not executable; install the framework before launching a seat')
        link = commands / name
        if link.is_symlink() and link.resolve() == target:
            continue
        temporary = commands / f'.{name}-{secrets.token_hex(8)}'
        try:
            temporary.symlink_to(target)
            os.replace(temporary, link)
        finally:
            temporary.unlink(missing_ok=True)
    excluded = {commands.resolve(), (ship.runtime / 'venv/bin').resolve()}
    if (source.parent / 'pyvenv.cfg').is_file():
        excluded.add(source.resolve())
    entries = [entry for entry in program_path().split(os.pathsep) if Path(entry).resolve() not in excluded]
    return os.pathsep.join([str(commands), *entries])


def launch_words(path: str, argv: list[str], *, socket: str | None, tmux_tmpdir: Path,
                 markers: tuple[str, ...] | list[str] = (), qd_command: Path | None = None) -> list[str]:
    """The words launch.sh hands to exec: the seat's program, cut off from the tmux server it runs in.

    tmux gives every pane TMUX and TMUX_PANE, and a bare tmux command takes its server from TMUX: a
    seat's shell that ran ``tmux kill-server`` would stop the whole ship. So both go; TMUX_TMPDIR names
    a fresh private folder of this launch's own, where a bare tmux finds no server but one it starts
    itself; QD_TMUX_SOCKET names the ship's server for qd alone. The ``markers`` are the session marks
    another program left in the server's environment, removed by name; the refused names
    (jail_environment.REFUSED) are removed always.
    """
    from .jail_environment import REFUSED

    reach = ["QD_TMUX_SOCKET=" + socket] if socket else ["-u", "QD_TMUX_SOCKET"]
    # The refused names go too, whatever the server or the session holds (jail_environment.REFUSED).
    unset = [word for name in (*markers, *(name for name in REFUSED if name not in markers)) for word in ("-u", name)]
    # Add this after launch.sh's sh: a shell may discard imported function variables. Child Bash
    # shells import it before reading their rc files, so a later PATH prepend cannot shadow qd.
    function = ([f'BASH_FUNC_qd%%=() {{ {shlex.quote(str(qd_command))} "$@"; }}']
                if qd_command is not None else [])
    return ["env", "-u", "TMUX", "-u", "TMUX_PANE", *unset, *reach, f"TMUX_TMPDIR={tmux_tmpdir}", f"PATH={path}",
            *function, *argv]


def refuse_secret_names(seat: str, env: dict[str, str], pool) -> None:
    """An unjailed seat's program receives no variable whose name looks secret, by the jail's rule (jail_environment):
    a refusal before anything opens. One exception: the one variable its own pool's declared key (api_key) fills,
    which is how its program pays. That variable is found by holding the key, never by its name or by showing it."""
    from . import jail_environment

    # A refused name is dropped at the window, never judged here: naming it does not stop the launch.
    secret = sorted(name for name in env if jail_environment.looks_secret(name) and not jail_environment.refused(name))
    if secret and getattr(pool, 'api_key', None):
        from .harness.home import key_environment

        key = key_environment(pool, 'key')['key']
        filled = [name for name in secret if env[name] == key]
        if len(filled) == 1:
            secret.remove(filled[0])
    if secret:
        raise ShipError(f"{seat}: its program's environment names {', '.join(secret)}, which looks secret; no "
                        "secret-named variable reaches a seat but the one its own pool's declared key fills, so "
                        "nothing was started")


def launch_script(seat: str, words: list[str]) -> str:
    """The seat's command as a script for tmux to run: tmux refuses a window command over 16 KiB, and a
    brief or a first message can be longer. The PATH travels inside it, since the login shell resets the
    one tmux passes."""
    return (f"#!/bin/sh\n# {seat}: the command qd up ran, kept in a file because tmux's command line is short\n"
            f"exec {shlex.join(words)}\n")


SEAT_HOLDS = "seat-holds.json"  # runtime/seat-holds.json: the service's steps held for one seat alone, for the line


class LaunchUnreadable(ShipError, ValueError):
    """A launch record that is not one qd wrote. The service holds that seat alone; no reader acts on it.

    ``malformed``: the file is not JSON at all, which readers that name evidence say as such."""

    malformed = False


# The fields qd writes in launch.json, and the types each may hold. The record sits in the seat's folder, which the
# seat's jail can write, so it is read as a claim: an unknown key, a missing one or a wrong type is refused whole.
_LAUNCH_FIELDS = {
    'pane': (str,), 'pid': (int,), 'at': (str,), 'pool': (str,), 'resume': (str, type(None)), 'argv': (list,),
    'version': (str, type(None)), 'harness': (str, type(None)), 'records_before': (list,),
    'recovery': (dict, type(None)), 'notice': (str, type(None)), 'answered': (list, type(None)),
    'start': (str, type(None)),
}
_LAUNCH_REQUIRED = ('pane', 'pid', 'at', 'pool', 'resume', 'argv', 'version')


IN_HAND_LAUNCH_DETAIL = 'typed whole at launch, the order in hand'
_LAUNCHES = trusted_cache.Kept()  # launch.json path -> its Launched, by its stamp and its price file's
_LOOKUPS = trusted_cache.Kept()  # a seat's trusted folder -> its last read-only record reader answer


def _binding(info: os.stat_result, raw: bytes) -> list:
    """pricing.binding's exact form (inode, size, mtime_ns, ctime_ns and the sha256 of the bytes), taken from one read
    of the file and its stat. pricing.py is in reader stamps and stays as it is; a test pins the two forms equal."""
    return [info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, hashlib.sha256(raw).hexdigest()]


def _saved_price(price_path: Path, info: os.stat_result, raw: bytes) -> dict | None:
    """pricing.saved_price, with the launch's binding taken from the one read of it, never a second read."""
    binding = _binding(info, raw)
    try:
        extra = json.loads(price_path.read_text())
        return extra['price'] if extra['binding'] == binding else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def parse_launch(text: str, name: str) -> dict:
    """The launch record's fields, checked whole; LaunchUnreadable says why not."""
    def refuse(why: str):
        return LaunchUnreadable(f"{name}: launch record refused ({why}); this seat is held, no other")
    try:
        data = json.loads(text)
    except ValueError:
        error = refuse('not JSON')
        error.malformed = True
        raise error from None
    if not isinstance(data, dict):
        raise refuse('not an object')
    if unknown := sorted(set(data) - set(_LAUNCH_FIELDS)):
        raise refuse(f"unknown field {unknown[0]!r}")
    if missing := [key for key in _LAUNCH_REQUIRED if key not in data]:
        raise refuse(f"missing field {missing[0]!r}")
    for key, value in data.items():
        if not isinstance(value, _LAUNCH_FIELDS[key]) or isinstance(value, bool):
            raise refuse(f"field {key!r} of the wrong type")
    if data['pid'] <= 0 or not all(isinstance(item, str) for item in [*data['argv'], *data.get('records_before', [])]):
        raise refuse('a field out of its range')
    try:
        datetime.fromisoformat(data['at'])
    except ValueError:
        raise refuse("field 'at' is not a time") from None
    return data


def declared_pools(seat) -> tuple[str, ...] | None:
    """The pools the ship file declares for the seat: the only ones its launch record may name.

    Every Seat carries its declaration. None only for an object that carries none (a test's stand-in), which the
    service never builds: there the launch's pool is not judged.
    """
    spec = getattr(seat, 'spec', None)
    pools = getattr(spec, 'pools', None)
    return tuple(pools) if pools is not None else None


def jailed_seat(seat) -> bool:
    """Whether the ship jails this seat: then its own folder, launch record included, is its jail's to write."""
    spec, ship = getattr(seat, 'spec', None), getattr(seat, 'ship', None)
    if spec is None or ship is None or not hasattr(ship, 'jailed'):
        return False
    try:
        return ship.jailed(spec, seat.pool)
    except (ShipError, AttributeError, KeyError):
        return True  # a seat whose jail cannot be read is judged as jailed


def launch_pool_refusal(seat, launched, *, every_seat: bool = False) -> str | None:
    """Why a launch record's pool is refused: it names a pool not declared for the seat. None when it is declared.

    For accounting, walls and the line this is judged for jailed seats alone: the threat is a jail writing its own
    folder, and an unjailed seat already holds the operator's power, so its launch is charged to the pool it names
    (a harness or pool switch still charges a running launch's turns to its old pool). Credentials judge every
    seat (``every_seat``).
    """
    if not every_seat and not jailed_seat(seat):
        return None
    declared = declared_pools(seat)
    if launched is not None and declared is not None and launched.pool not in declared:
        return (f"{seat.name}: launch record names pool {launched.pool!r}, not one declared for it; "
                + ("refused for credentials, accounting and walls" if jailed_seat(seat) else "refused for credentials"))
    return None


def declared_launch_pool(seat, launched, default):
    """The launch's pool where the ship file declares it for the seat, else ``default``: what accounting, walls and
    usage may charge or read."""
    return launched.pool if launched is not None and launch_pool_refusal(seat, launched) is None else default


class SelectionError(ShipError):
    """One seat's saved selection cannot establish its current program and pool."""

    def __init__(self, seat: str, reason: str):
        self.seat = seat
        super().__init__(reason)


@dataclass(frozen=True)
class UnresolvedSeat:
    ship: Ship
    spec: SeatSpec
    tmux: Tmux
    error: SelectionError

    @property
    def name(self) -> str:
        return self.spec.name


EXPLICIT = trusted_files.EXPLICIT  # beside ``pool``: a person chose that pool for the next launch, which takes it


class Seat:
    def __init__(self, ship: Ship, spec: SeatSpec, tmux: Tmux, *, launching: bool = False):
        self.ship, self.spec, self.tmux = ship, spec, tmux
        # A launch of a seat with no launch on file reads a saved choice the ship file no longer declares as
        # absent, so the declared order chooses (``launch_choice``); every other reader still refuses it.
        self._launching = launching and not trusted_files.path(ship.runtime, spec.name, trusted_files.LAUNCH).exists()
        self.folder = ship.runtime / "seats" / spec.name  # its own: its jail may write all of it
        self.trusted = trusted_files.folder(ship.runtime, spec.name)  # what the service writes and trusts: no jail sees it
        if self.folder.is_dir():  # a seat that exists has both; reading a ship that has none makes nothing
            try:
                trusted_files.make(ship.runtime, spec.name)  # private; inside a jail, in that jail's own empty tmpfs
            except OSError:
                pass  # a runtime that cannot be written: nothing is written there either
        self.home = self.folder / "home"
        self._prepared_observation: tuple[str | None, str | None] | None = None
        try:
            self.spec = replace(spec.for_pool(self.pool.name), human_session=self.session.human)
            self.harness = self._adapter_for(self.spec)
            if self.spec.pool_choices and (launched := self.launched()) is not None:
                if launched.pool != self.pool.name or record_owner.launch_reader(launched) != self.spec.harness:
                    raise ShipError(f"{self.name}: saved launch and selected program disagree; restore its pool declaration/selection")
        except (ShipError, OSError, ValueError, TypeError) as error:
            reason = str(error).removeprefix(f'{spec.name}: ')
            raise SelectionError(spec.name, f"{spec.name}: selection unknown: {reason}") from error

    @property
    def name(self) -> str:
        return self.spec.name

    def _adapter_for(self, spec: SeatSpec) -> Harness:
        return shipmod.program_adapter(self.ship, harnesses.get(spec.harness))

    @property
    def session(self) -> shipmod.Session:
        return self.ship.session_of(self.spec)

    # -- state files -------------------------------------------------------------------

    @property
    def pool(self) -> Pool:
        """The pool the seat is on: the first declared, or the one failover moved it to."""
        current = self.trusted / trusted_files.POOL
        try:
            name = current.read_text().strip()
        except FileNotFoundError:
            name = None
        if self.spec.pool_choices:
            if name is not None and name not in self.spec.pools and self._launching:
                return self.ship.pool(self.spec.pools[0])  # the ship file dropped it: the declared order chooses
            if name is not None and name not in self.spec.pools:
                raise ShipError(f"{self.name}: saved pool {name!r} has no declared choice; restore its declaration")
            if name is None:
                launched = self.launched()
                if launched is not None:
                    name = launched.pool
                    if name not in self.spec.pools:
                        raise ShipError(f"{self.name}: saved launch pool has no declared choice; restore its declaration")
                elif any((self.trusted / file).exists() for file in
                         (trusted_files.RECORD, trusted_files.READERS, trusted_files.LAUNCHES)):
                    raise ShipError(f"{self.name}: saved pool missing with retained record; restore the selected pool")
        return self.ship.pool(name if name in self.spec.pools else self.spec.pools[0])

    def move_to(self, pool: str, *, explicit: bool | None = False) -> None:
        """Select ``pool``. ``explicit``: True records a person's choice for the next launch, False clears any, and
        None leaves it as it is, for a selection made before a launch that can still be refused."""
        self.spec.for_pool(pool)
        trusted_files.make(self.ship.runtime, self.name)
        path = self.trusted / trusted_files.POOL
        durable.replace_file(path, pool + '\n')
        if explicit:
            durable.replace_file(self.trusted / EXPLICIT, pool + '\n')
        elif explicit is False:
            (self.trusted / EXPLICIT).unlink(missing_ok=True)

    def _saved_name(self) -> str | None:
        try:
            return (self.trusted / trusted_files.POOL).read_text().strip() or None
        except FileNotFoundError:
            return None

    def launch_choice(self, *, current_walled: bool = False) -> tuple[str, str]:
        """The pool a launch of this down seat takes, and why: the ship file is the Admiral's order.

        A person's explicit choice for this launch (``qd set pool=`` on a down seat) is taken as it is. Otherwise
        the first declared choice with room wins over the saved choice: a saved choice later in the list than a
        choice with room, or one the ship file no longer declares, gives way. Room is what the service routes by
        (``quota.cached_open``): a choice whose room is unknown, with no ledger, its usage not yet read or its
        history still catching up, is never taken over the saved one. The saved choice stays while the ledger does
        not show it walled; with no choice that has room, the saved choice stays, or the first declared one. A
        choice paid by a key is never taken while another is not shown walled, a saved one included
        (ship.key_refusal); left alone, its launch is refused.
        """
        pools = self.spec.pools
        current = self.pool.name
        saved = self._saved_name() or current
        try:
            explicit = (self.trusted / EXPLICIT).read_text().strip()
        except FileNotFoundError:
            explicit = None
        if explicit is not None and explicit == current:
            return current, 'chosen for this launch by qd set'
        from . import ledger, quota
        book = ledger.Ledger.reading(self.ship.runtime / 'ledger.sqlite')
        unknown = []
        try:
            for name in pools:
                if self.ship.pool(name).paid_by_key:
                    continue  # never a launch's choice, saved or not: its launch is refused (ship.key_refusal)
                if current_walled and name == current:
                    continue  # the running seat proved this wall before choosing the relaunch destination
                if name == saved:
                    if book is None or not book.walled(name, self.ship, scope=quota.model_scope(self, name)):
                        return name, 'its saved choice'
                    continue  # spent: a declared choice with room after it may take its place
                if book is not None and quota.cached_open(self.ship, book, name, self):
                    return name, f'the first declared choice with room, before its saved {saved}'
                if book is None or not book.walled(name, self.ship, scope=quota.model_scope(self, name)):
                    unknown.append(name)  # not shown walled, and its room not known either
        finally:
            if book is not None:
                book.close()
        if saved not in pools:
            # The ship file is the Admiral's order: a saved choice it no longer declares gives way even where no
            # declared choice's room is known, to the first one not shown walled, else to the first declared.
            if unknown:
                return unknown[0], (f'its saved {saved} is no longer declared; the first declared choice not shown '
                                    f'walled, its room unknown')
            return pools[0], f'its saved {saved} is no longer declared and every declared choice is walled'
        if unknown and self.ship.pool(saved).paid_by_key:
            return unknown[0], (f'its saved {saved} is paid by a key; the first declared choice not shown walled, '
                                'its room unknown')
        if unknown:
            return current, f"saved choice retained; room unknown on {', '.join(unknown)}"
        return current, 'every declared choice is walled; its saved choice stays'

    def failover_to(self, pool: str) -> str:
        """Preserve the source before selecting another program or paying pool."""
        with self._lifecycle():
            return self._failover_to(pool)

    def _failover_to(self, pool: str) -> str:
        from .recovery import require_clear
        require_clear(self)
        from . import credential

        selected = self.spec.for_pool(pool)
        observation = self._observe_selection(selected, self.ship.pool(pool))
        same_program = selected.harness == self.spec.harness
        resume = same_program and self.resumable()
        target = self._require_resume(selected) if resume else None
        from . import relaunch_intent
        relaunch_intent.record_destination(self, resume, pool=pool, harness=selected.harness, target=target)
        self.record()
        credential.reseed(self.ship, [self], wait=credential.DOWN_WAIT)
        stopped = self._relaunch_down()
        self.move_to(pool)
        self.spec = selected
        if not same_program:
            self.harness = self._adapter_for(selected)
        proof = stopped + '\n' + self._up(resume=resume, observation=observation, target=target)
        return proof if resume else f'{proof} · {resume_target.NEW_CONVERSATION}'

    def launch_stamp(self) -> tuple | None:
        """Canonical launch/selection/readability metadata, without reading a launch."""
        from . import seat_migration
        result = []
        for name in (trusted_files.LAUNCH, trusted_files.POOL, seat_migration.UNCONFIRMED):
            try:
                info = trusted_files.path(self.ship.runtime, self.name, name).stat()
            except FileNotFoundError:
                result.append(None)
                continue
            except OSError:
                return None
            result.append((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                           info.st_ctime_ns, info.st_mode))
        return tuple(result)

    def launched(self) -> Launched | None:
        path = self.trusted / trusted_files.LAUNCH
        from . import seat_migration
        if (hold := seat_migration.unconfirmed(self.trusted)) is not None:
            raise LaunchUnreadable(f"{self.trusted.name}: its live window could not be joined to its old launch record when "
                                   f"its files moved ({hold.get('reason', 'unknown')}); this seat is held, no other; "
                                   f"qd down {self.trusted.name} closes that window")
        # launch.json is read once, its price binding taken over those same bytes, and the result kept while it and
        # launch-price.json stand as they were read (trusted_cache). Each caller gets its own copy.
        price_path = path.with_name(path.stem + '-price.json')
        key, started = str(path), trusted_cache.now()
        try:
            before = (trusted_cache.stamp(path), trusted_cache.stamp(price_path))
        except OSError:
            before = None  # read afresh below, which says why
        if before is not None and before[0] is None:
            _LAUNCHES.drop(key)
            return None
        if before is not None and (kept := _LAUNCHES.get(key, before)) is not None:
            return copy.deepcopy(kept)
        try:
            with open(path, 'rb') as stream:
                info = os.fstat(stream.fileno())
                raw = stream.read()
            text = io.TextIOWrapper(io.BytesIO(raw)).read()  # decoded as Path.read_text decodes
        except FileNotFoundError:
            _LAUNCHES.drop(key)
            return None
        except (OSError, UnicodeError) as error:
            raise LaunchUnreadable(f"{self.folder.name}: launch record unreadable ({type(error).__name__}); "
                                   "this seat is held, no other") from None
        data = parse_launch(text, self.folder.name)
        if data.get('recovery') is not None:
            from .harness.thinking import validate_launch
            try:
                validate_launch(data['recovery'])
            except ValueError as error:
                raise LaunchUnreadable(f"{self.folder.name}: launch record refused ({error}); "
                                       "this seat is held, no other") from None
        result = Launched(**data, price=_saved_price(price_path, info, raw))
        if before is None:
            return result
        try:
            after = (trusted_cache.stamp(path), trusted_cache.stamp(price_path))
        except OSError:
            return result
        if trusted_cache.stamp_of(info) == before[0] and _LAUNCHES.keep(key, before, after, started, result):
            return copy.deepcopy(result)
        return result

    def pane(self) -> Pane | None:
        """The live pane of this seat's launch, by pane id and process id, or None."""
        launched = self.launched()
        if launched is None:
            return None
        pane = self.tmux.pane(launched.pane)
        if pane is None or pane.pid != launched.pid:
            return None  # law: identity
        return pane

    def _lifecycle_pane(self, launched: Launched | None) -> Pane | None:
        """Unmatched live evidence is a refusal, not permission to forget a launch."""
        pane = self.tmux.pane(launched.pane) if launched is not None else None
        if self.launched() != launched:
            raise ShipError(f"{self.name}: launch identity changed; retained for inspection")
        if pane is not None and pane.pid != launched.pid and self._earlier_server():
            return None  # the saved id named a pane of an earlier server lifetime; this one is another's
        if pane is not None and pane.pid != launched.pid:
            raise ShipError(f"{self.name}: pane {launched.pane} no longer matches its launch PID; "
                            "launch retained, no window closed or new launch allowed")  # law: identity
        return pane

    def _earlier_server(self) -> bool:
        """The running tmux server began after this launch was written, so the launch's pane cannot be in it.

        launch.json is written after its pane exists, and a server's start is whole seconds rounded
        down, so a start later than the file's write time is proof, and any doubt (an unreadable
        start, a restart within the same second) keeps the refusal. The launch's own `at` is no
        proof: it is stamped before the window opens, and opening it can start the server.
        """
        started = self.tmux.started()
        try:
            written = (self.trusted / trusted_files.LAUNCH).stat().st_mtime
        except FileNotFoundError:
            return False
        return started is not None and started > written

    def _retire_window(self, launched: Launched | None) -> None:
        pane = self._lifecycle_pane(launched)
        if pane is not None:
            self.tmux.kill_window(pane.id, expected_pid=launched.pid)
            if self.tmux.pane(pane.id) is not None:
                raise ShipError(f"{self.name}: pane {pane.id} retirement is unconfirmed; launch retained")

    @property
    def is_up(self) -> bool:
        pane = self.pane()
        return pane is not None and not pane.dead

    def record_answer(self, launched: Launched, entry: dict) -> Launched | None:
        """Write one answered question into this launch's record, only while the launch is still ``launched``.

        Written before the press and again with its outcome, so a crash between them never lets the same
        launch be pressed twice.
        """
        if self.launched() != launched:
            return None  # law: identity
        answered = [item for item in (launched.answered or []) if item.get("question") != entry["question"]]
        updated = replace(launched, answered=[*answered, entry])
        updated.write(self.trusted / trusted_files.LAUNCH)
        return updated

    def read_pane(self, history: int = 0) -> tuple[Pane, str] | None:
        """A fresh screen still belongs to the launch whose identity we checked."""
        options = {"styles": True} if getattr(self.harness, "styled_screen", False) else {}
        if not hasattr(self.tmux, 'read_pane'):
            pane = self.pane()
            return (pane, self.tmux.capture(pane.id, history=history, **options)) if pane and not pane.dead else None
        launched = self.launched()
        if launched is None:
            return None
        found = self.tmux.read_pane(launched.pane, history, **options)
        if (found is None or found[0].pid != launched.pid or found[0].dead
                or self.launched() != launched):
            return None  # law: identity
        return found

    def read_control_frame(self):
        """Positive cursor/style evidence belongs to this same live launch, or remains unknown."""
        launched = self.launched()
        if launched is None or not hasattr(self.tmux, 'read_control_frame'):
            return None
        frame = self.tmux.read_control_frame(launched.pane)
        if (frame is None or frame.pane.pid != launched.pid or frame.pane.dead or frame.pane.in_mode
                or self.launched() != launched):
            return None  # law: identity
        return frame

    def _lookup_reader(self, record: Path) -> str | None:
        """record_owner.lookup_reader, read-only (bindings with publish=False), kept per seat while every file it reads
        stands as it was read (trusted_cache): the reader registry, the launches journal, the record pin, the launch
        and its unconfirmed-move hold, and the home's conversations.owned, that one stamped with and without following
        a link, since it is read without following one. The record, the home and the program's home variable are part
        of what it answered for. A refusal is never kept; publishing never comes through here."""
        from . import seat_migration
        trusted = self.trusted
        owned = self.home / 'conversations.owned'
        context = (str(record), str(self.home), getattr(self.harness, 'home_variable', ''))
        key, started = str(trusted), trusted_cache.now()
        files = (record_owner.registry(trusted), trusted / trusted_files.LAUNCHES, trusted / trusted_files.RECORD,
                 trusted / trusted_files.LAUNCH, trusted / seat_migration.UNCONFIRMED)
        try:
            before = (*(trusted_cache.stamp(path) for path in files), trusted_cache.stamp(owned),
                      trusted_cache.link_stamp(owned))
        except OSError:
            return record_owner.lookup_reader(self, record)
        kept = _LOOKUPS.get(key, before)
        if kept is not None and kept[0] == context:
            return kept[1]
        owner = record_owner.lookup_reader(self, record)
        try:
            after = (*(trusted_cache.stamp(path) for path in files), trusted_cache.stamp(owned),
                     trusted_cache.link_stamp(owned))
        except OSError:
            return owner
        _LOOKUPS.keep(key, before, after, started, (context, owner))
        return owner

    def _pinned_join(self, *, publish: bool) -> Join | None:
        """Validate a pin; None alone means that no join has been pinned yet."""
        pinned = self.trusted / trusted_files.RECORD
        if pinned.is_file():
            try:
                named = pinned.read_text().strip()
            except (OSError, UnicodeDecodeError) as error:
                return Join(None, f"record pointer unreadable: {error}; inspect with qd watch {self.name}")
            if not named:  # an emptied pointer names no record; Path('') would be the working folder
                return Join(None, f"record pointer empty: {pinned}; restore the owned record if available; inspect with qd watch {self.name}")  # law: one-record
            record = Path(named)
            from .harness.base import inside_home
            if self.harness.home_variable is not None and not inside_home(record, self.home):
                # A home is its jail's to write: a pin through a link there is never this seat's record.
                return Join(None, f"record outside the home: {record.name} is not this seat's own; inspect with qd watch {self.name}")  # law: one-record
            from . import seat_files
            try:
                owner = (record_owner.pinned_reader(self, record) if publish else
                         self._lookup_reader(record))
            except (OSError, seat_files.LinkRefused) as error:  # a link its jail planted on the way: refused, named
                return Join(None, f"record reader unknown: {error}; inspect with qd watch {self.name}")
            if owner != self.spec.harness:
                return Join(None, f"record reader {'unknown' if owner is None else 'changed'}: retained {owner or 'untyped'} record {record.name}; "
                            f"historical reader evidence is unchanged by a new launch; inspect with qd watch {self.name}")
            if self.launched() is not None and record_owner.launch_reader(self.launched()) != owner:
                return Join(None, f"record reader changed: {record.name} and saved launch disagree; inspect with qd watch {self.name}")
            from .record_location import retired
            if retired(record):
                return Join(None, f"record retired: {record.name}; use qd fresh {self.name}")
            if record.exists():
                if publish and not self._join_retained(record, self.launched()):
                    if refused := self._noted_or_refused(record):
                        return refused
                return Join(record)
            return Join(None, f"record missing: {record.name}; restore the owned record if available; inspect with qd watch {self.name}")
        return None

    def lookup_record(self) -> Path | None:
        """Read only an already-pinned join, without discovering or publishing one.

        A missing, unreadable or mismatched record is never a replacement candidate.
        This lookup neither registers a reader nor retains a launch boundary.
        """
        found = self._pinned_join(publish=False)
        return found.record if found is not None else None

    def join(self, *, publish=True) -> Join:
        """Find the first record after launch and ordinarily pin it and retain its ownership."""
        found = self._pinned_join(publish=publish)
        if found is not None:
            return found
        pinned = self.trusted / trusted_files.RECORD
        launched = self.launched()
        if launched is None:
            return Join(None, "not up")
        if record_owner.launch_reader(launched) != self.spec.harness:
            return Join(None, f"record reader changed: launch belongs to another or unknown harness; inspect with qd watch {self.name}")
        if self.harness.home_variable is None and self._shared_state_contended():
            return Join(None, f"ambiguous: another {self.harness.name} seat is up without a record of its own")  # law: one-record
        try:
            found = self.harness.join(self.home, launched.when, tuple(Path(p) for p in launched.records_before))
        except OSError as error:
            return Join(None, f"record unreadable: {error}; inspect with qd watch {self.name}")
        if found.record is not None and publish:
            trusted_files.make(self.ship.runtime, self.name)
            durable.replace_file(pinned, str(found.record) + "\n")  # law: one-record
            if refused := self._noted_or_refused(found.record):
                return refused
        return found

    def _noted_or_refused(self, record: Path) -> Join | None:
        """Note the record as owned; a link its jail planted where the home belongs refuses that, and the join with
        it: a record found through such a link is not this seat's own. Named, never raised."""
        from . import seat_files
        try:
            self._note_owned(record)
        except seat_files.LinkRefused as error:
            return Join(None, f"record ownership refused: {error}; inspect with qd watch {self.name}")
        return None

    def _note_owned(self, record: Path) -> None:
        record_owner.register(self.trusted, record, self.spec.harness)
        launched = self.launched()
        remember_launch(self.trusted, record, launched if record_owner.launch_reader(launched) == self.spec.harness else None)
        owned = self.home / "conversations.owned"
        from . import seat_files
        seat_files.make_folder(self.home)  # never through a link its jail planted  # the home is its jail's to write: read and write here never follow a link
        existing = (seat_files.read_text(owned) or '').splitlines()
        if str(record) not in existing:
            durable.replace_file(owned, "".join(f"{line}\n" for line in [*existing, str(record)]))

    def _shared_state_contended(self) -> bool:
        """A program with no home of its own writes every seat's record to one place: two unjoined seats cannot be told apart."""
        for other in self.ship.seats.values():
            if other.name != self.name:
                if not any(other.for_pool(pool).harness == self.spec.harness for pool in other.pools):
                    continue
                try:
                    sibling = Seat(self.ship, other, self.tmux)
                except SelectionError:
                    return True  # an unresolved sibling cannot certify exclusive shared state
                if sibling.spec.harness == self.spec.harness and sibling.is_up:
                    pinned = sibling.trusted / trusted_files.RECORD
                    if not pinned.is_file():
                        return True
                    from . import seat_files
                    try:
                        record = Path(pinned.read_text().strip())
                        if not record.exists() or record_owner.pinned_reader(sibling, record) != sibling.spec.harness:
                            return True
                    except (OSError, seat_files.LinkRefused):  # a sibling's planted link: it cannot certify either
                        return True
        return False

    def is_working(self) -> bool:
        """Whether a turn is open: from the record first, from the screen when its state is unknown.

        A screen that cannot show it, or a pane that cannot be read, answers True: every caller holds
        on working, and none may act as if the seat were idle on what it could not see (law: record-first).
        """
        pane = self.pane()
        if pane is None or pane.dead:
            return False
        if (record := self.record()) is not None:
            try:
                turn_open = self.harness.state(record).turn_open
            except OSError:
                turn_open = None
            if turn_open is not None:
                return turn_open  # law: record-first
        found = self.read_pane()
        return found is None or self.harness.screen(found[1]).working is not False

    def record(self) -> Path | None:
        """Read a retained join; otherwise discover/journal it through the one joining path."""
        launched = self.launched()
        record = self.lookup_record()
        if record is not None:
            if self._join_retained(record, launched):
                return record
        return self.join().record

    def _join_retained(self, record: Path, launched: Launched | None) -> bool:
        """Check all publication stages, including a resumed launch, without writing."""
        owned = self.home / "conversations.owned"
        registered = {"record": str(record), "harness": self.spec.harness}
        from . import seat_files  # the home is its jail's: read without following a link, and bounded
        return (registered in record_owner.registry_events(self.trusted)
                and str(record) in (seat_files.read_text(owned) or '').splitlines()
                and retained_launch(self.trusted, record, launched)
                and self.launched() == launched)

    # -- the one relaunch path -----------------------------------------------------------

    @contextmanager
    def _lifecycle(self, *, automatic: bool = False, wait: bool = False) -> Iterator[None]:
        """One caller owns the whole seat transition, including compound relaunches.

        Keep the empty inode outside removable seat homes. No lifecycle path unlinks
        it; old binaries do not participate in this advisory coordination protocol.
        """
        from . import lifecycle

        seconds = 0 if automatic else lifecycle.WAIT_SECONDS if wait else lifecycle.explicit_wait()
        with lifecycle.claim(self.ship.runtime, self.name, wait=seconds):
            if not automatic:
                from . import compaction
                compaction.disarm(self, adopt_successor=True)
            yield

    def up(self, resume: bool = True, *, pool: str | None = None) -> str:
        """Bring the seat up. A seat with declared choices, being down, launches on ``launch_choice``, or on
        ``pool`` for this one launch; a move to another program starts it fresh, as failover does."""
        with self._lifecycle():
            return self._up_with_pool_choice(resume, pool=pool, fresh_after_order=True)

    def _up_with_pool_choice(self, resume: bool, *, pool: str | None = None, fresh_after_order: bool = False,
                             recorded: bool = False, require_resume_target: bool = False,
                             expected: resume_target.Target | None = None) -> str:
        """Choose and launch while the caller holds the seat's lifecycle claim."""
        self.migrate()  # before the launch choice reads the pool selection in its new place
        if pool is not None and pool not in self.spec.pools:  # a plain list declares its choices as tables do
            raise ShipError(f"{self.name}: --pool {pool!r} is not one of its declared choices "
                            f"({', '.join(self.spec.pools)})")
        reported = (fresh_after_order and resume and self.spec.fresh == 'after-order' and not self.is_up
                    and self._last_delivered_order_reported())
        if reported:
            resume = False
        def proof(text):
            return text + (' · fresh: its last order is reported' if reported else '')
        from . import relaunch_intent
        resume = resume and self.resumable()
        if len(self.spec.pools) < 2 or self.is_up:  # a plain list of pools chooses as a table of them does
            prepared = self._require_resume(expected=expected) if resume and require_resume_target else None
            relaunch_intent.record_destination(self, resume, target=prepared)
            return proof(self._up(resume, target=prepared))
        from .recovery import require_clear
        require_clear(self)
        if pool is None:
            target, why = self.launch_choice()
        else:
            target = pool
            why = 'recorded relaunch destination' if recorded else 'chosen for this launch by qd up --pool'
        current = self.pool.name
        saved = self._saved_name()
        selected = replace(self.spec.for_pool(target), human_session=self.session.human)
        same_program = selected.harness == self.spec.harness
        resume = resume and same_program
        prior_launch = self.launched()
        prepared = (self._require_resume(selected, expected=expected)
                    if resume and (require_resume_target or prior_launch is not None) else None)
        relaunch_intent.record_destination(self, resume, pool=target, harness=selected.harness, target=prepared)
        if target == current and pool is None and why == 'its saved choice':
            (self.trusted / EXPLICIT).unlink(missing_ok=True)
            return proof(self._up(resume, target=prepared))  # nothing chosen: the launch is as it always was
        observation = self._observe_selection(selected, self.ship.pool(target))
        if prior_launch is not None:
            self._relaunch_down()  # retain the old record and reseed its source pool before changing selection
            if recorded and self.launched() is not None:
                raise ShipError(self.relaunch_refused() or f'{self.name}: old launch still retained')
        # A person's choice is used up only by a launch that starts: _up clears it once its pane is open, and a
        # launch refused at one of _up's doors leaves it as the person set it.
        self.move_to(target, explicit=None)
        self.spec = selected
        if not same_program:
            self.harness = self._adapter_for(selected)
        started = proof(self._up(resume=resume, observation=observation, target=prepared))
        if not resume and target != current:
            started += f' · {resume_target.NEW_CONVERSATION}'
        moved = f"{self.name}: launched on {target} ({why})" + (
            f"; its saved choice was {saved}" if saved is not None and target != saved else "")
        return moved + '\n' + started

    def _up_on_recorded_pool(self, pool: str, harness: str, *, resume: bool,
                             expected: resume_target.Target | None = None) -> str:
        """Finish a service transaction without choosing from current quota or a saved pool hint."""
        selected = self.spec.for_pool(pool)
        if selected.harness != harness:
            raise ShipError(f'{self.name}: its recorded destination no longer names the same program')
        if resume and (harness != self.harness.name or not self.resumable()):
            raise resume_target.Unproved(self.name)
        if pool == self.pool.name:
            target = self._require_resume(expected=expected) if resume else None
            return self._up(resume=resume, target=target)
        return self._up_with_pool_choice(resume, pool=pool, recorded=True, require_resume_target=resume,
                                         expected=expected)

    def _last_delivered_order_reported(self) -> bool:
        """Use the service's open-work reading; an ack is never a closing report.

        Leave the read-only view before launching, just as fresh_after_order does,
        so the normal fresh carriage reads any newly arrived open messages.
        """
        store, attempts = post.Store(self.ship.runtime), post.Attempts(self.ship.runtime)
        with post.reading_pass(store=store, attempts=attempts):
            if store.open_for(self.name):  # the same reading as Service._fresh_after_order_ready
                return False
            orders = {message.id: message for message in store.for_seat(self.name)
                      if message.kind == 'order'}
            latest = {attempt.message: attempt for attempt in attempts.all() if attempt.seat == self.name}
            delivered = [attempt for message, attempt in latest.items() if message in orders
                         and attempt.result in ('delivered', 'watch', 'launch')]
            last = max(delivered, key=lambda attempt: (attempt.when, attempt.message), default=None)
            return last is not None and store.closing_report(orders[last.message]) is not None

    def _accept_pool(self, spec: SeatSpec | None = None, pool: Pool | None = None) -> None:
        selected = spec or self.spec
        paid = pool or self.pool
        reader = self._adapter_for(selected)
        if reason := reader.accepts(paid):
            raise ShipError(f"{self.name}: {reason}")
        if reason := shipmod.key_refusal(paid):  # every launch door passes here, before anything of a launch is made
            raise shipmod.KeyRefused(f"{self.name}: {reason}")
        validate_selection(selected, reader)
        reader.check_defaults(selected, self.ship.folder / 'homes' / reader.name)

    def _observe_selection(self, spec: SeatSpec | None = None, pool: Pool | None = None):
        """Reuse the normal version observation before any retirement or startup."""
        selected = spec or self.spec
        self._accept_pool(selected, pool)
        reader = self._adapter_for(selected)
        if reader.private_version_home:
            shipmod.fence(self.ship)
        if self._prepared_observation is not None and spec is None:
            observation, self._prepared_observation = self._prepared_observation, None
            return observation
        version = _version_in_runtime(reader, self.ship.runtime)
        note = validate_selection(selected, reader, version=version, installed=True)
        return version, note

    def migrate(self) -> dict[str, str]:
        """Consider this seat's files in its own folder once, before anything reads their new place (seat_migration)."""
        from . import seat_migration
        return seat_migration.migrate(self.ship, self.spec, self.tmux)

    def _screened(self) -> bool:
        """A record a launch of this hand wrote while screening a pull request is still in its home: no launch of it
        resumes, whatever asked for one (a restart, a relaunch intent's finish, a failover), until the retirement
        has moved that record out (pr_screen, hand_transcripts)."""
        from . import pr_screen
        return pr_screen.screening_left(self.ship.runtime, self.name)

    def adapter_spec(self, spec: SeatSpec | None = None) -> SeatSpec:
        """The seat as its adapter is given it, at a launch and at every resume's preflight alike: with the working
        directory it runs in (ship.cwd_of: its declared cwd, else the ship's workspace), never None. One rule for
        both, so the folder a resume is checked against is the folder its launch works in."""
        spec = self.spec if spec is None else spec
        return replace(spec, cwd=self.ship.cwd_of(spec))

    def _never_resumed(self) -> bool:
        """A hand takes one order at a time in a fresh context, so no launch of it ever resumes its conversation, by
        whatever path it comes: qd up, restart or relaunch, the service's relaunch, a failover or pool move, an
        alarm's or a server's recovery, a sync's relaunch. Each starts it fresh with its open messages carried."""
        return self.spec.role == 'hand'

    def resumable(self) -> bool:
        return not self._never_resumed() and not self._screened()

    def _up(self, resume: bool = True, *, observation=None, target: resume_target.Target | None = None) -> str:
        self.migrate()
        resume = resume and self.resumable()
        self._accept_pool()
        if launchable := getattr(self.tmux, "launchable", None):
            launchable()  # a socket too long or not this user's refuses before anything of the launch is made
        from .recovery import require_clear
        require_clear(self)
        from . import write_bound
        write_bound.require_clear(self)  # a seat stopped at its write bound stays down until a person lifts it
        from . import jail_end
        if (sandbox := jail_end.held(self.trusted)) is not None:
            # The one hold every launch path meets: restart, fresh, failover, a pool move, the service's relaunch.
            raise ShipError(jail_end.words(self.name, sandbox) + "; the service's tick lifts it once they have")
        if (held := self.retiring()) is not None:
            if not isinstance(held.get("pid"), int) or process_alive(held["pid"], held.get("start")):
                raise ShipError(f"{self.name}: the program its last launch ran is still ending (pid {held.get('pid')}); "
                                "up waits for it, so nothing it writes meets the new launch")
            self._finish_retirement(self.launched())
        previous = self.launched()
        left = self._lifecycle_pane(previous)
        if left is not None and not left.dead:
            raise ShipError(f"{self.name} is up in {left.id}; qd restart {self.name} relaunches it")
        if target is not None and not resume:
            raise ShipError(f'{self.name}: the proved conversation cannot be resumed on this launch')
        if resume and target is None:
            target = self._resume_target()
        if self.spec.remote_control and not self.harness.remote_control:
            raise ShipError(f"{self.name}: remote_control = true, and {self.harness.name} has no Remote Control; "
                            "select a harness that offers it or drop the key")
        if self.ship.jailed(self.spec, self.pool):
            from . import sandbox  # the sandbox reads the ship and the standing orders, so it is imported here, not above

            # A jailed seat's own folder under its ship's work/ or its workspace's, or a common workspace that is its
            # ship's own work/ (the forum's shape), as declared, never through a link, before anything of the launch is
            # made (law: one-folder).
            sandbox.check_own_work(self.ship, self.name, self.spec)
        version, effort_note = observation if observation is not None else self._observe_selection()
        shipmod.fence(self.ship)
        self.folder.mkdir(parents=True, exist_ok=True)
        trusted_files.make(self.ship.runtime, self.name)
        if left is not None and left.dead:
            self._retire_window(previous)  # the last launch's failed pane goes only with matching identity
        if self.launched() is not None:
            from . import credential  # the credential module reads seats, so it is imported here, not above

            credential.reseed(self.ship, [self], wait=credential.DOWN_WAIT)  # a refresh left in this home reseeds the pool it came from, before any move
        prior = {item.path for item in record_owner.records(self)}
        from .harness.base import inside_home
        prior.update(path for path in self.harness.records(self.home)  # never through a link out of its home
                     if self.harness.home_variable is None or inside_home(path, self.home))
        for record in prior:
            record_owner.register(self.trusted, record, None)  # retain even untyped history before clearing a fresh pin
        brief = standing.render(self.ship, self.spec)
        pool = self.pool
        ship_home = self.ship.folder / "homes" / self.harness.name
        spec = self.adapter_spec()  # the adapter sees the seat's real working directory, as every preflight does
        self.harness.build_home(self.home, spec, pool, ship_home if ship_home.is_dir() else None)
        resume_id = target.id if target else None
        if resume and target is None and prior:
            self.harness.preflight_resume(spec, self.home, lambda: None)
        carriage = self.carriage(resume_id=resume_id, brief_at=datetime.now(timezone.utc))
        # Plan after supplying the brief, while the last record is still pinned.
        carried = carriage.carried
        if not resume_id:
            (self.trusted / trusted_files.RECORD).unlink(missing_ok=True)
        path = self._path()
        jailed = self.ship.jailed(self.spec, pool)

        def command(opening: str | None) -> tuple[Launch, list[str]]:
            launch_with_record = getattr(self.harness, 'launch_with_record', None)
            if target is not None and launch_with_record is not None:
                launch = launch_with_record(spec, pool, self.home, brief, target.id, opening, target.record)
            else:
                launch = self.harness.launch(spec, pool, self.home, brief, resume_id, opening)
            if shutil.which(launch.argv[0], path=path) is None:
                raise ShipError(f"{self.name}: {launch.argv[0]} is not on the PATH qd runs with; install it, or put it on that PATH")
            argv = list(launch.argv)
            if jailed:
                from . import sandbox  # the sandbox reads the ship and the standing orders, so it is imported here, not above

                endpoint = pool.endpoint if pool.kind == 'endpoint' else None  # its one way out, relayed in
                jail = sandbox.plan(self.ship, self.name, self.folder, self.home, self.ship.cwd_of(self.spec), launch.argv[0], path,
                                    endpoint=endpoint, pool=pool, synthetic=self.harness.synthetic)
                argv = sandbox.command(jail, argv, launch.env)  # the jail starts from nothing but these
            else:
                refuse_secret_names(self.name, launch.env, pool)
            return launch, argv

        self._launch_tmpdir = self._fresh_tmux_tmpdir()

        def words(argv: list[str], env: dict[str, str]) -> list[str]:
            return self._launch_words(argv, env, path)

        def misfit(launch: Launch, argv: list[str]) -> str | None:
            return carriagemod.misfit(words(argv, launch.env), launch.env)  # the words launch.sh hands to exec

        from . import carriage_plan, post_threads

        generation = self.folder / carriagemod.FOLDER / secrets.token_hex(12)

        def plan(force_files=False):
            try:
                if carriage.in_hand:  # a hand's fresh start: the order in hand, whole, and nothing else
                    return carriage_plan.in_hand(carriage.text, directory=generation,
                                                 limit=getattr(self.ship, 'carry_bytes', shipmod.CARRY_BYTES),
                                                 inline_limit=carriagemod.INLINE_BYTES, force_files=force_files)
                # law: context-is-cache -- only admitted self-orders are full, and only on an actual fresh conversation
                return carriage_plan.build(carriage.rows, handoff=carriage.text or '', header=carriage.header,
                                           directory=generation, limit=getattr(self.ship, 'carry_bytes', shipmod.CARRY_BYTES),
                                           inline_limit=carriagemod.INLINE_BYTES, force_files=force_files,
                                           history=carriage.history, idle=self._idle_line())
            except carriage_plan.ImpossibleBudget as error:
                raise ShipError(f'{self.name}: {error}; nothing was started or published') from None

        planned = plan()
        launch, argv = command(planned.opening)
        if misfit(launch, argv):
            planned = plan(force_files=True)
            launch, argv = command(planned.opening)
        carried_file = planned.index_path
        known = {**launch.env, "QD_SHIP": str(self.ship.folder), "QD_SEAT": self.name}  # a lower bound: tmux adds its own
        if reason := carriagemod.refused(words(argv, launch.env), known):  # refused only where exec surely would
            raise ShipError(f"{self.name}: exec would refuse this launch for its size, whatever it carries: {reason}; "
                            "nothing was started")
        if planned.files:
            try:
                carriagemod.folder_of(self.folder)
                carriage_plan.publish(planned)
            except (OSError, UnicodeError, carriage_plan.ImpossibleBudget) as error:
                raise ShipError(f'{self.name}: bounded carriage could not be published ({error}); nothing was started') from None
        at = datetime.now(timezone.utc).isoformat()  # before the program can write a record (law: one-record)
        pane = self._open_window(argv, launch.env, path)
        launched = Launched(pane.id, pane.pid, at, pool.name, resume_id, argv, version, self.spec.harness,
                            sorted(map(str, prior)), launch.price, launch.recovery, notice=launch.notice,
                            start=process_start(pane.pid))
        launched.write(self.trusted / trusted_files.LAUNCH)
        (self.trusted / REFUSED_RELAUNCH).unlink(missing_ok=True)  # up again: a refused relaunch is over
        from . import brief_change
        try:
            brief_change.mark(self.ship.runtime, self.name, brief.read_text())  # this launch loads the current brief
        except OSError:
            pass  # unmarked, the next message carries the change once more: nothing is lost
        if self.spec.pool_choices:
            self.move_to(pool.name)
        if resume_id:
            self.record()  # retain the resumed record's new pool boundary immediately
        self._survives(pane.id, launch.argv[0])
        attempts = post.Attempts(self.ship.runtime)
        for message in carried:
            attempt = attempts.open(message, self.name, pane.id, pane.pid, launched.at)
            attempts.close(attempt, "launch", IN_HAND_LAUNCH_DETAIL if carriage.in_hand
                           else post_threads.INDEXED_LAUNCH_DETAIL)
        how = f"resumed {resume_id[:8]}" if resume_id else "fresh"
        carried_note = (f" · its order typed whole" if carriage.in_hand and carried else
                        f" · {len(carriage.rows)} thread{'s' if len(carriage.rows) != 1 else ''} indexed")
        if carried_file:
            carried_note += f" in {carried_file.relative_to(self.ship.runtime)}"
        jail_note = " · jailed: the operator's home hidden, the workspace writable" if jailed else ""
        notice_note = f" · {launched.notice}" if launched.notice else ""
        unchecked_note = f" · {effort_note}" if effort_note else ""
        if started := getattr(self, "server_started", None):
            unchecked_note += f" · {started}"
        return f"{self.name} up in {pane.id} · {self.harness.name} {launched.version or ''} · provider {providers.display(pool.provider)} · pool {pool.name} · {how}{carried_note}{jail_note}{notice_note}{unchecked_note}"

    def _resume_target(self) -> resume_target.Target | None:
        from .record_location import retired
        pinned = self.trusted / trusted_files.RECORD
        try:
            launched = self.launched()
            if pinned.is_file() and retired(Path(pinned.read_text().strip())):
                raise resume_target.Unproved(self.name, 'the conversation is retired', launch=launched)
            record = self.record()
            identity = resume_target.identify(self.harness, record) if record is not None else None
            if self.launched() != launched or (launched is not None and launched.resume and identity != launched.resume):
                return None
        except (OSError, ValueError, TypeError, AttributeError, RecursionError):
            return None
        return resume_target.Target(identity, record) if identity is not None else None

    def _require_resume(self, spec: SeatSpec | None = None, *,
                        expected: resume_target.Target | None = None) -> resume_target.Target:
        launched = self.launched()
        target = self._resume_target() if self.resumable() else None
        if target is None or (expected is not None and target != expected):
            raise resume_target.Unproved(self.name, launch=launched)
        self.harness.preflight_resume(self.adapter_spec(spec), self.home, lambda: target.id)
        return target

    def first_message(self, *, resume_id: str | None = None) -> tuple[str | None, list[post.Message]]:
        """Preview the policy-bounded first message without publishing its files."""
        from . import carriage_plan
        carriage = self.carriage(resume_id=resume_id)
        if carriage.in_hand:
            planned = carriage_plan.in_hand(carriage.text, directory=self.folder / carriagemod.FOLDER / 'preview',
                                            limit=getattr(self.ship, 'carry_bytes', shipmod.CARRY_BYTES))
            return planned.opening, carriage.carried
        planned = carriage_plan.build(carriage.rows, handoff=carriage.text or '', header=carriage.header,
                                      directory=self.folder / carriagemod.FOLDER / 'preview',
                                      limit=getattr(self.ship, 'carry_bytes', shipmod.CARRY_BYTES),
                                      history=carriage.history, idle=self._idle_line())
        return planned.opening, carriage.carried

    def _idle_line(self) -> str:
        """A hand's opening with nothing open ends with this line: it runs no command and waits for an order."""
        return carriagemod.HAND_IDLE if self.spec.role == 'hand' else ''

    def carriage(self, *, resume_id: str | None = None, brief_at=None) -> carriagemod.Carriage:
        """Index incoming work and outgoing waits; fresh self-handoffs alone are full.

        Closed obligations never return through unread or unconfirmed transport
        history. Answers below this recipient's reported order are settled too;
        other unread answers and reports still arrive as before.
        """
        from . import post_reads, refusal, thread_context, post_threads
        from .pr_screen import held as screening

        if screening(self.ship.runtime, self.name):
            # A launch the pull-request screen starts sits beside an outsider's text: it gets its brief and, once
            # typed, the screen's own instruction, and nothing else of the post. No index line, no message id, no
            # handoff, no notice history. A hand screens only with no open order of its own; an unreadable hold
            # counts as one. The hand's next ordinary start, once the hold is released, carries its index again.
            return carriagemod.Carriage(None, [])  # no first message at all: the native brief still loads
        store, attempts = post.Store(self.ship.runtime), post.Attempts(self.ship.runtime)
        with post.admission(self.ship.runtime, self.name):
            # law: context-is-cache -- what the seat printed whole with qd thread is receipted here, by this unjailed
            # qd, before the index is built, so a reading made before this launch is dated before it.
            try:
                post_reads.take(self.ship.runtime, self.name, lambda: (
                    message.id for message in store.all() if self.name in (message.sender, message.to)))
            except Exception as error:  # a claim never stops a launch: the index is built without it
                post_reads.refused(self.ship.runtime, self.name, f'taking its claim failed ({error!r})')
            try:
                refused = refusal.excluded(self.ship.runtime, self.name)
            except refusal.JournalError as error:
                raise ShipError(f"{self.name}: {error}; inspect it before a launch carries anything") from None
            history = [attempt for attempt in attempts.all() if attempt.seat == self.name]
            latest = {attempt.message: attempt for attempt in history}
            unread = attempts.undelivered(store, self.name, results={key: value.result for key, value in latest.items()}, for_launch=True)
            queued = self._queued_unread(store, attempts)
            def outstanding(message):
                # law: open-until-closed -- a missing receipt cannot reopen completed work.
                return message.kind not in ('order', 'question') or store.is_open(message)

            extra = [message for message in (*unread, *queued) if outstanding(message)]
            # A message a launch carried as an index line alone leaves the index once the seat has printed it whole
            # after that launch: a reading before it belongs to a conversation this one does not hold.
            indexed: dict = {}
            for attempt in history:
                if attempt.result == 'launch':
                    indexed[attempt.message] = max(indexed.get(attempt.message, attempt.when), attempt.when)
            consumed = (self._consumed_messages(store, attempts, resume_id, refused)
                        | store.read_ids(self.name, after=indexed))
            settled = store.settled_notices(self.name, brief_at=brief_at)
            consumed |= settled.keys()
            from .pr_screen import spent_instructions  # a screen's instruction never comes back at a launch
            consumed |= spent_instructions(self.ship.runtime, self.name, store)
            consumed |= thread_context.closed_order_answers(store, self.name)
            consumed |= frozenset(message.id for message in store.for_seat(self.name)
                                  if message.obligation and not store.is_open(message))
            selection = thread_context.select(store, self.name, extra, consumed=consumed, excluded=refused)
            messages = store.all()
            sent = tuple(message for message in messages
                         if message.sender == self.name and message.to != self.name and store.is_open(message))
            pending_messages = tuple(message for message in extra if message.id not in consumed)
            delivered = {attempt.message for attempt in history if attempt.result in ('delivered', 'watch')}
            queued_ids = {message.id for message in queued}
            unconfirmed = tuple(message for message in store.for_seat(self.name)
                                if message.kind != 'ack' and outstanding(message)
                                and message.id not in consumed and message.id not in delivered
                                and not ((attempt := latest.get(message.id)) is not None
                                         and attempt.result == 'queued' and message.id not in queued_ids))
            if self.spec.role == 'hand' and not resume_id:
                # law: context-is-cache -- a hand's fresh start carries the order in hand, typed whole, and nothing
                # else: no index, no handoff, no earlier thread, no notice history. It holds one order at a time:
                # its oldest open order or question, with that order's own conversation so far, in thread order.
                return self._in_hand(store, messages, refused)
            rows = post_threads.index_rows(store, selection.roots, (*pending_messages, *unconfirmed), refused, sent,
                                           seat=self.name)
        handoffs = [entry.message for entry in selection.entries
                    if not resume_id and not entry.context_only and entry.message.kind == 'order'
                    and entry.message.sender == self.name and entry.message.to == self.name]
        carried = list({message.id: message for message in (*selection.roots, *pending_messages)
                        if message.id not in refused}.values())
        header = post.briefed_header(self.name, self.spec.role, list(selection.roots))
        from .post_notices import summary
        return carriagemod.Carriage(post.typed(handoffs, whole=True) if handoffs else None, carried, header, rows,
                                   summary(settled))

    def _in_hand(self, store: post.Store, messages: list[post.Message], refused) -> carriagemod.Carriage:
        """The order in hand: the oldest open order or question to this hand, and every message below it that the
        hand sent or received but an ack, each typed whole in thread order. The other open messages wait for it."""
        from .hand_door import order_in_hand  # the door holds by this same reading
        from .post_threads import _descends
        order = order_in_hand(store, self.name, refused)
        if order is None:
            return carriagemod.Carriage(None, [], in_hand=True)
        by_id = {message.id: message for message in messages}
        below = [message for message in messages
                 if message.id not in refused and message.kind != 'ack'
                 and self.name in (message.sender, message.to) and _descends(message, {order.id}, by_id)]
        in_hand = [order, *sorted(below, key=lambda message: (message.at, message.id))]
        carried = [message for message in in_hand if message.to == self.name]
        return carriagemod.Carriage(post.typed(in_hand), carried, in_hand=True)

    def _consumed_messages(self, store: post.Store, attempts: post.Attempts, resume_id: str | None,
                           excluded: frozenset[str]) -> frozenset[str]:
        from . import thread_context

        if not resume_id or (record := self.record()) is None:
            return frozenset()
        try:
            if (record_owner.pinned_reader(self, record) != self.spec.harness
                    or self.harness.session_id(record) != resume_id):
                return frozenset()
            consumed = thread_context.consumed_ids(store, self.name, self.spec.role, self.harness.inputs(record),
                                                   excluded=excluded)
        except (OSError, ValueError, TypeError, AttributeError):
            return frozenset()  # unknown consumption can never suppress amended work
        unacted = {message for message, result in attempts.last_results(self.name).items() if result == 'unacted'}
        return consumed - unacted  # retain the existing next-start carriage of proved unacted input

    def _queued_unread(self, store: post.Store, attempts: post.Attempts) -> list[post.Message]:
        """Carry queued messages whose exact input has not been consumed since their latest attempt."""
        latest = {a.message: a for a in attempts.all() if a.seat == self.name}
        queued = [m for m in store.for_seat(self.name) if m.id in latest and latest[m.id].result == "queued"]
        if not queued:
            return []
        owned = record_owner.records(self)
        unread = []
        for message in queued:
            consumed = False
            for item in owned:
                if item.reader is None:
                    continue
                try:
                    if input_receipt.confirmed(item.reader, item.path, post.delivered_forms(message), latest[message.id].receipt_since) == "delivered":
                        consumed = True
                        break
                except OSError:
                    continue
            if not consumed:
                unread.append(message)
        return unread

    def _path(self) -> str:
        return prepare_program_path(self.ship, synthetic=self.harness.synthetic)

    @property
    def _tmux_tmpdirs(self) -> Path:
        """Where each launch's private TMUX_TMPDIR is made: a folder of the seat's own, mode 700."""
        return self.folder / "tmux"

    def _fresh_tmux_tmpdir(self) -> Path:
        """A new empty private folder for this launch's TMUX_TMPDIR, so a bare tmux in the seat reaches no
        server, not even one an earlier launch started there. Earlier launches' folders go only when empty."""
        base = self._tmux_tmpdirs
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        check_private_folder(base)
        for earlier in base.iterdir():
            if earlier.is_dir() and not earlier.is_symlink():
                for folder in (earlier / f"tmux-{os.getuid()}", earlier):
                    try:
                        folder.rmdir()  # never a socket or a file: an earlier launch's server keeps its folder
                    except FileNotFoundError:
                        continue
                    except OSError:
                        break
        # Mode 700, made by this launch alone. Where the ship's folder is so deep that a socket in it would be
        # too long, a bare tmux there fails with "File name too long" and reaches no server at all.
        return Path(tempfile.mkdtemp(prefix="l", dir=base))

    def _launch_words(self, argv: list[str], env: dict[str, str], path: str) -> list[str]:
        """This launch's exec words: its fresh private TMUX_TMPDIR, the server it runs on, every adapter's
        session marks removed but the ones the seat's own adapter sets in ``env``, and the variables the seat's
        own adapter removes from its launch."""
        markers = [name for name in harnesses.inherited_markers() if name not in env]
        markers += [name for name in self.harness.launch_unset if name not in env and name not in markers]
        if getattr(self, "_launch_tmpdir", None) is None:
            self._launch_tmpdir = self._fresh_tmux_tmpdir()  # set afresh by every up; made here only for another path
        return launch_words(path, argv, socket=self._socket_for_seat(), tmux_tmpdir=self._launch_tmpdir, markers=markers,
                            qd_command=self.ship.runtime / 'bin/qd')

    def _socket_for_seat(self) -> str | None:
        """What QD_TMUX_SOCKET tells qd inside the seat: the server it runs on, by its path. A seat launched on
        tmux's default server, before its ship moved to its own, is told that server, explicitly. A jailed
        seat is told none: tmux stays out of its reach."""
        if self.ship.jailed(self.spec, self.pool):
            return None
        socket = getattr(self.tmux, "socket", None)
        return socket if socket and Path(socket).is_absolute() else None

    def _open_window(self, argv: list[str], env: dict[str, str], path: str) -> Pane:
        self.server_started = None  # where this launch started the ship's own tmux server, if it did
        session = self.session
        ship_env = {"QD_SHIP": str(self.ship.folder)}
        if split := (self.tmux.split(self.ship) if getattr(self.tmux, "ship", False) else []):
            raise ShipError(f"this ship runs in two places: {', '.join(split)}, and also on its own server "
                            f"{self.tmux.socket}; nothing was started. qd down, then qd up, puts it in one")
        if self.tmux.has_session(session.title):
            owner = self.tmux.session_environment(session.title, "QD_SHIP")
            if owner != str(self.ship.folder):
                raise ShipError(f"tmux session {session.title!r} belongs to {owner or 'another server user'}; "
                                f"give this ship's session another title")
        else:
            if getattr(self.tmux, "ship", False):
                self.tmux.started_server = None  # this launch's own start of the ship's server, and no earlier one
            self.tmux.new_session(session.title, env=ship_env)
            self.server_started = getattr(self.tmux, "started_server", None)
            status = shlex.join([str(qd_bin() / "qd"), "--ship", str(self.ship.folder), "status", "--line"])
            self.tmux.status_line(session.title, status)
        from .jail_environment import without_refused

        env = without_refused({**env, **ship_env, "QD_SEAT": self.name})  # a refused name reaches no pane
        cwd = self.ship.cwd_of(self.spec)
        if not cwd.is_dir():
            raise ShipError(f"{self.name}: working directory {cwd} does not exist; qd creates nothing outside its folder")  # law: one-folder
        # tmux runs it as the operator: kept where no jail reaches, and written through a temporary renamed over the
        # name, so a link at the name is replaced, never followed.
        script = trusted_files.make(self.ship.runtime, self.name) / trusted_files.LAUNCH_SCRIPT
        durable.replace_file(script, launch_script(self.name, self._launch_words(argv, env, path)))
        return self.tmux.new_window(session.title, self.spec.display, cwd, ["sh", str(script)], env)

    def _survives(self, pane_id: str, program: str, seconds: float = 0.5) -> None:
        """A program that exits as soon as it starts is a refusal, not an "up"; its pane stays for a look."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            time.sleep(0.01 if self.harness.synthetic else 0.05)
            pane = self.tmux.pane(pane_id)
            if pane is not None and pane.dead:
                shown = " ".join(self.tmux.capture(pane_id, history=20).split())[-240:]  # its last words scroll above the death notice
                exit_detail = ("exited at once; exit status not yet known" if pane.exit_status is None
                               else f"exited {pane.exit_status} at once")
                raise ShipError(f"{self.name}: {program} {exit_detail}; its pane stays for a look, and "
                                f"qd up {self.name} tries again. It printed: {shown}")
            if pane is not None and self.harness.synthetic and self.harness.synthetic_ready(self.home):
                return

    def down(self) -> str:
        with self._lifecycle(wait=True):
            from . import seat_migration
            if (lifted := seat_migration.close_unconfirmed(self.ship, self.name, self.tmux)) is not None:
                return lifted  # a person's down on a seat its files move held: its own window alone
            return self._down()

    def _relaunch_down(self) -> str:
        """Down as the first half of a relaunch in this lifecycle: the longer observation applies."""
        self._relaunching = True
        try:
            return self._down()
        finally:
            self._relaunching = False

    def _down(self, relaunch: bool = False) -> str:
        """Close the pane, observe the program end, then reseed and remove the launch.

        ``relaunch``: an up follows in this lifecycle (restart, fresh, a pool move), so past the first bound
        down keeps observing the same process up to RELAUNCH_END_SECONDS before the relaunch is refused.
        """
        relaunch = relaunch or getattr(self, "_relaunching", False)
        if (held := self.retiring()) is not None and (held["pid"] is None or process_alive(held["pid"], held["start"])):
            raise ShipError(f"{self.name}: prior retirement not proved; its launch is retained")
        launched = self.launched()
        pane = self._lifecycle_pane(launched)
        if launched is not None:
            from .pending_lifecycle import link_launch_record
            link_launch_record(self, launched)
            if self.spec.pool_choices:
                self.move_to(self.pool.name)  # retain the corroborated selection before its launch disappears
        # The program recorded at launch, by pid and start time; a launch without its start (written before
        # the start was recorded, or a stand-in's) is not waited for: no other process is taken for it.
        program = ((launched.pid, launched.start) if pane is not None and launched is not None and launched.start
                   and process_alive(launched.pid, launched.start) else None)
        from . import jail_end
        # The program's whole tree, read while the window still holds it: a jail's inner bwrap is still its child.
        tree = jail_end.snapshot(launched) if program is not None else {}
        with jail_end.stopping(self.trusted):
            return self._end_down(launched, pane, program, tree, relaunch)

    def _end_down(self, launched, pane, program, tree, relaunch: bool) -> str:
        """The rest of down, while the sweep leaves this seat's sandbox to it."""
        from . import jail_end
        self._retire_window(launched)
        if self.launched() != launched:
            raise ShipError(f"{self.name}: launch identity changed during retirement; launch retained")
        if program is not None:
            durable.replace_file(self.trusted / RETIRING, json.dumps(self._retirement_binding(launched)) + "\n")
        waited = ""
        if program is not None and not wait_for_end(*program, PROCESS_END_SECONDS):
            started = time.monotonic() - PROCESS_END_SECONDS
            if not (relaunch and wait_for_end(*program, RELAUNCH_END_SECONDS - PROCESS_END_SECONDS)):
                # The pane is closed but the program still runs: it may still write its credential or record.
                # Nothing is reseeded and the launch stays, for the service's tick to finish once it has ended.
                bound = RELAUNCH_END_SECONDS if relaunch else PROCESS_END_SECONDS
                if relaunch:
                    durable.replace_file(self.trusted / REFUSED_RELAUNCH, json.dumps(dict(
                        at=datetime.now(timezone.utc).isoformat(), pid=program[0])) + "\n")
                    from . import relaunch_intent  # a hold that keeps the seat down: no intent brings it up
                    relaunch_intent.clear(self.ship.runtime, self.name, 'a relaunch refused while its program was ending')
                return (f"{self.name} down · pane {pane.id} closed · process still ending after {bound:g}s; "
                        + ("relaunch refused; " if relaunch else "") + "credential and launch left for the next tick")
            waited = (f" · still ending after {PROCESS_END_SECONDS:g}s, ended after "
                      f"{time.monotonic() - started:.0f}s; the relaunch follows")
        # The program has ended. Its jail's tree, and any orphan of the seat, end now, confirmed by pid and start:
        # what still runs holds the seat, with its launch, so that nothing is launched beside it.
        if left := jail_end.end(self, tree):
            jail_end.hold(self.trusted, self.name, left)
            from . import relaunch_intent  # a hold that keeps the seat down: no intent brings it up
            relaunch_intent.clear(self.ship.runtime, self.name, 'held: its old sandbox did not end')
            if relaunch:
                durable.replace_file(self.trusted / REFUSED_RELAUNCH, json.dumps(dict(
                    at=datetime.now(timezone.utc).isoformat(), pid=launched.pid if launched else None)) + "\n")
            (self.trusted / RETIRING).unlink(missing_ok=True)  # the sandbox hold now owns deferred cleanup
            closed = f"pane {pane.id} closed" if pane is not None else "no pane"
            return (f"{self.name} down · {closed} · "
                    + jail_end.words(self.name, jail_end.held(self.trusted)).removeprefix(f"{self.name}: ")
                    + ("; relaunch refused" if relaunch else ""))
        self._finish_retirement(launched)
        if pane is None:
            return f"{self.name} was not up" + (f" · pane {launched.pane} is gone" if launched else "")
        return f"{self.name} down · pane {pane.id} closed · process ended" + waited

    def _finish_retirement(self, launched: Launched | None) -> None:
        if self.launched() != launched or ((held := self.retiring()) is not None and held["pid"] is None):
            raise ShipError(f"{self.name}: retirement does not prove this launch; launch and retirement record retained")
        if launched is not None:
            from .pending_lifecycle import link_launch_record
            link_launch_record(self, launched)  # include a last write before the program ended
            from . import credential  # the credential module reads seats, so it is imported here, not above

            credential.reseed(self.ship, [self], wait=credential.DOWN_WAIT)  # the program has ended: a refresh it left reseeds the pool its launch names, while it is named
        (self.trusted / RETIRING).unlink(missing_ok=True)
        if launched is not None:
            (self.trusted / trusted_files.LAUNCH).unlink(missing_ok=True)

    def relaunch_refused(self) -> str | None:
        """Plain words for the status line while the seat is down because its relaunch met a program still ending."""
        from . import jail_end
        if (sandbox := jail_end.held(self.trusted)) is not None:
            return jail_end.words(self.name, sandbox)  # the same count the refused launch gives
        held = self.retiring()
        if held is not None:
            return f"{self.name}: its program is still ending; " + (
                "its relaunch was refused" if (self.trusted / REFUSED_RELAUNCH).exists() else "it is not retired yet")
        if (self.trusted / REFUSED_RELAUNCH).exists():
            return f"{self.name}: its relaunch was refused while its program was ending; qd up {self.name}"
        return None

    def _retirement_binding(self, launched: Launched) -> dict:
        return dict(seat=self.name, pid=launched.pid, start=launched.start,
                    launch=dict(pane=launched.pane, at=launched.at))

    def retiring(self) -> dict | None:
        """The closed pane's old program, bound to its launch until retirement is proved."""
        from . import seat_files
        path = self.trusted / RETIRING
        unknown = dict(pid=None, start=None)
        try:
            raw = seat_files.read_text(path)
            if raw is None:
                path.lstat()  # an unreadable entry still holds
                return unknown
            data = json.loads(raw)
        except FileNotFoundError:
            return None
        except (OSError, ShipError, ValueError):
            return unknown
        if (not isinstance(data, dict) or type(data.get("pid")) is not int or data["pid"] <= 0
                or not isinstance(data.get("start"), str) or not data["start"]):
            return unknown
        try:
            launched = self.launched()
            if launched is None and trusted_cache.link_stamp(self.trusted / trusted_files.LAUNCH) is not None:
                return unknown  # absence requires the name itself to be gone
        except (OSError, ShipError, ValueError):
            return unknown
        if data.keys() == {"pid", "start"}:  # the older marker has only the process identity
            return data if launched is None or (data["pid"], data["start"]) == (launched.pid, launched.start) else unknown
        if launched is not None:
            return data if data == self._retirement_binding(launched) else unknown
        # An older removal order could leave this marker after completing and removing its launch.
        identity = data.get("launch")
        if (data.keys() != {"seat", "pid", "start", "launch"} or data["seat"] != self.name
                or not isinstance(identity, dict) or identity.keys() != {"pane", "at"}
                or not isinstance(identity["pane"], str) or not re.fullmatch(r"%[0-9]+", identity["pane"])
                or not isinstance(identity["at"], str)):
            return unknown
        try:
            return data if datetime.fromisoformat(identity["at"]).tzinfo is not None else unknown
        except ValueError:
            return unknown

    def finish_retirement(self) -> str | None:
        """The service's tick: reseed and remove the launch once the program down left running has ended, and end
        a held sandbox again until none of it runs."""
        from . import jail_end
        with self._lifecycle():
            marker = self.trusted / RETIRING
            held = self.retiring()
            if held is not None:
                if held["pid"] is None or process_alive(held["pid"], held["start"]):
                    return None
                if jail_end.held(self.trusted) is not None:
                    marker.unlink()  # an interruption may have left both records after the sandbox hold was written
                    held = None
                elif "launch" in held and self.launched() is None:
                    self._finish_retirement(None)
                    return f"{self.name} retired · process {held['pid']} ended · retirement record removed"
            lifted = jail_end.retry(self.trusted)
            if lifted is False:
                return None  # still held: the status line names it
            if lifted is True:
                self._finish_retirement(self.launched())
                return f"{self.name} retired · its old sandbox has ended · credential reseeded, launch removed"
            if held is None:
                return None
            with jail_end.stopping(self.trusted):
                if left := jail_end.end(self, {}):  # the program has ended: its jail's orphans end with it
                    jail_end.hold(self.trusted, self.name, left)
                    marker.unlink()
                    return jail_end.words(self.name, jail_end.held(self.trusted))
            self._finish_retirement(self.launched())
            return f"{self.name} retired · process {held['pid']} ended · credential reseeded, launch removed"

    def restart(self) -> str:
        """Relaunch with resume: the conversation continues in a new process."""
        with self._lifecycle():
            self._accept_pool()
            from .recovery import require_clear
            require_clear(self)
            hand = self._never_resumed()
            if moved := self._relaunch_off_wall(resume=not hand):
                return moved
            screened = self._screened()
            resume = not (screened or hand)
            target = self._require_resume() if resume else None
            observation = self._observe_selection()
            from . import relaunch_intent
            relaunch_intent.record_destination(self, resume, target=target)
            return (self._relaunch_down() + "\n" + self._up(resume=resume, observation=observation, target=target)
                    + (" · fresh: a hand starts fresh on every launch" if hand else
                       " · fresh: a record of a launch that screened a pull request is still in its home"
                       if screened else ""))

    def fresh(self) -> str:
        """Relaunch without resume: a new conversation that begins with the brief and the open messages."""
        with self._lifecycle():
            proof = self._fresh()
        if self.session.human and resume_target.NEW_CONVERSATION not in proof:
            proof = f'{self.name} {resume_target.NEW_CONVERSATION} on the explicit fresh request.\n{proof}'
        return proof

    def _fresh(self) -> str:
        """Start fresh under the caller's lifecycle claim."""
        self._accept_pool()
        from .recovery import require_clear
        require_clear(self)
        if moved := self._relaunch_off_wall(resume=False):
            return moved
        observation = self._observe_selection()
        from . import relaunch_intent
        relaunch_intent.record_destination(self, False)
        return self._relaunch_down() + "\n" + self._up(resume=False, observation=observation)

    def _walled_now(self) -> bool:
        """Whether the ledger shows the seat's current pool walled, under its running model's scope."""
        from . import ledger, quota
        book = ledger.Ledger.reading(self.ship.runtime / 'ledger.sqlite')
        if book is None:
            return False
        try:
            return quota.current_walled(self, book, declared_fallback=True)
        finally:
            book.close()

    def _off_wall_destination(self) -> tuple[str, str] | None:
        if len(self.spec.pools) > 1 and self._walled_now():
            target, why = self.launch_choice(current_walled=True)
            if target != self.pool.name:
                return target, why
        return None

    def require_restart_target(self) -> None:
        """Diagnose missing identity when the service cannot read the current turn."""
        if self.resumable():
            destination = self._off_wall_destination()
            if destination is None or self.spec.for_pool(destination[0]).harness == self.spec.harness:
                self._require_resume()

    def _relaunch_off_wall(self, *, resume: bool) -> str | None:
        """A relaunch never lands on a walled pool while a declared choice has room: it takes the launch's choice.

        Only a seat whose current pool is walled moves; one with room relaunches where it is. None: nothing moved.
        """
        destination = self._off_wall_destination()
        if destination is None:
            return None
        target, why = destination
        came_from = self.pool.name
        selected = replace(self.spec.for_pool(target), human_session=self.session.human)
        observation = self._observe_selection(selected, self.ship.pool(target))
        same_program = selected.harness == self.spec.harness
        resume = resume and same_program and self.resumable()
        prepared = self._require_resume(selected) if resume else None
        from . import relaunch_intent
        relaunch_intent.record_destination(self, resume, pool=target, harness=selected.harness, target=prepared)
        stopped = self._relaunch_down()
        self.move_to(target)
        self.spec = selected
        if not same_program:
            self.harness = self._adapter_for(selected)
        proof = self._up(resume=resume, observation=observation, target=prepared)
        if not resume:
            proof += f' · {resume_target.NEW_CONVERSATION}'
            post.Store(self.ship.runtime).new('report', post.SHIP, self.name,
                                              f'Moved to pool {target} from {came_from} and {resume_target.NEW_CONVERSATION}.')
        return (stopped + f"\n{self.name}: relaunched on {target}, not on its walled {came_from} ({why})\n" + proof)


@contextmanager
def temporary_seat(ship: Ship, tmux: Tmux, spec: SeatSpec, orders: list[post.Message], purpose: str,
                   *, installed_version: str | None = None, human: bool = False, check_inbox: bool = False) -> Iterator[Seat]:
    """Own a fresh temporary seat through transport retirement and every teardown stage.

    Callers register only orders this operation opened. Posts and receipts remain;
    a failure to retire transport leaves its home intact for later inspection.
    """
    spec = replace(spec, temporary=True)  # the jail's own-cwd rule binds declared seats, not this one
    session = shipmod.Session(spec.session, spec.session, human=human)
    treated = replace(ship, sessions={**ship.sessions, spec.session: session}, seats={**ship.seats, spec.name: spec})
    seat = Seat(treated, spec, tmux)
    if installed_version is not None:
        note = validate_selection(seat.spec, seat.harness, version=installed_version, installed=True)
        seat._prepared_observation = installed_version, note
    store = post.Store(ship.runtime)
    with pending.transient_lock(ship.runtime, seat.name) as owned:
        if not owned:
            raise ShipError(f"{seat.name}: temporary seat ownership is busy; retry {purpose}")
        # A held renewal's record is unlinked after its home is gone. A stop between the two leaves the
        # record with no home, so that name stays taken until the record itself is gone.
        if (spec.name in ship.seats or spec.session in ship.sessions or tmux.has_session(session.title)
                or seat.folder.exists() or seat.trusted.exists() or (ship.runtime / "post" / "pending" / seat.name).exists()
                or store.for_seat(seat.name)
                or any(a.seat == seat.name for a in post.Attempts(ship.runtime).all())
                or _held_for_renewal(ship, spec.name)):
            raise ShipError(f"{seat.name}: temporary seat/session/history already exists; retry {purpose}")
        seat.folder.mkdir(parents=True, exist_ok=False)
        trusted_files.make(ship.runtime, seat.name)
        # Written before anything of it runs: a seat this run leaves behind is named, and qd down takes it, by this.
        durable.replace_file(seat.trusted / TEMPORARY, json.dumps(
            {"purpose": purpose, "harness": spec.harness, "pool": spec.pools[0], "human": human}) + "\n")
        session_id = None
        # Cleanup is registered before publication can fail, and runs before the
        # outer lease retires. The transient lock remains held around both.
        with ExitStack() as lifetime, ExitStack() as cleanup:
            _teardown(cleanup, treated, seat, tmux, orders, purpose,
                      lambda: tmux.kill_session_id(session_id) if session_id is not None else None)
            lifetime.enter_context(pending.check_inbox_registration(
                ship.runtime, seat.name, human, enabled=check_inbox))
            session_id = tmux.new_session(session.title, env={"QD_SHIP": str(ship.folder)})
            status = shlex.join([str(qd_bin() / "qd"), "--ship", str(ship.folder), "status", "--line"])
            tmux.status_line(session.title, status)
            yield seat


def _teardown(cleanup: ExitStack, treated: Ship, seat: Seat, tmux: Tmux, orders: list[post.Message], purpose: str,
              end_session, down=None) -> None:
    """Register a temporary seat's teardown, run last-first: retire its transport, take it down, end its
    session, close the orders it was given, then remove its home, only once its program is proved ended."""
    store = post.Store(treated.runtime)
    retired = False
    launched = None

    def retire() -> None:
        nonlocal retired, launched
        try:
            launched = seat.launched()  # the program to prove ended before the home goes
        except (LaunchUnreadable, OSError):
            launched = None  # its launch.json stands, so the home stays; the status line names the seat
        pending.abandon(treated.runtime, seat.name, f"{purpose} ended; submission unconfirmed and never resent")
        retired = True

    def close_orders() -> None:
        with ExitStack() as closing:
            for order in orders:
                if order.kind == "order" and order.to == seat.name and store.is_open(order):
                    closing.callback(store.new, "report", seat.name, order.sender,
                                     f"{purpose} ended; closed by the temporary operation", re=order.id)

    def remove_home() -> None:
        # A launch still standing means down did not prove its program ended: the home stays, the status
        # line names the seat, and qd down <seat> finishes it (law: nothing-waits-silently).
        running = launched is not None and launched.start and process_alive(launched.pid, launched.start)
        if retired and not running and not (seat.trusted / trusted_files.LAUNCH).exists():
            from . import credential

            lines = credential.reseed(treated, [seat], wait=credential.DOWN_WAIT)  # its one pool: a refresh left in the home
            if any(line.startswith(credential.SKIPPED) for line in lines) and credential.hold_home(treated, seat):
                return  # not reseeded, so never deleted with its home: kept and named until the service's pass settles it
            shutil.rmtree(seat.folder)
            if seat.trusted.is_dir():
                shutil.rmtree(seat.trusted)  # its trusted files go with it

    held: list[int] = []
    previous: dict = {}

    def hold() -> None:
        """A signal that arrives during teardown waits for it: a second Ctrl-C or TERM never cuts it short."""
        if threading.current_thread() is threading.main_thread():
            for number in TEARDOWN_SIGNALS:
                previous[number] = signal.signal(number, lambda signum, frame: held.append(signum))

    def release() -> None:
        for number, handler in previous.items():
            signal.signal(number, handler)
        if held:
            signal.raise_signal(held[0])  # acts now, through whatever handler stood before the teardown

    cleanup.callback(release)
    cleanup.callback(remove_home)
    cleanup.callback(close_orders)
    cleanup.callback(end_session)
    cleanup.callback(down or seat.down)
    cleanup.callback(retire)
    cleanup.callback(hold)


TEARDOWN_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


# -- a temporary seat that outlived its run --------------------------------------------------

# A check's seat raised before temporary seats recorded their raising: read from its name alone.
CHECK_SEAT = re.compile(r"check-(?:wake-)?[a-z][a-z0-9]*(?:-[a-z0-9]+)*-[0-9a-f]{4}")
UNREADABLE = "unreadable"


def _temporary(ship: Ship, name: str) -> dict | str | None:
    """What temporary_seat recorded of a seat it raised whose home is still here: purpose, harness, pool and human;
    UNREADABLE when that record cannot be used; None when the name is no temporary seat's home."""
    folder = ship.runtime / "seats" / name
    if name in ship.seats or not shipmod.valid_name(name) or not folder.is_dir():
        return None
    try:
        record = json.loads(trusted_files.path(ship.runtime, name, TEMPORARY).read_text())
    except FileNotFoundError:
        if not CHECK_SEAT.fullmatch(name):
            return None
        return {"purpose": "qd check", "harness": name.removeprefix("check-").removeprefix("wake-").rsplit("-", 1)[0],
                "pool": None, "human": name.startswith("check-wake-")}
    except (OSError, ValueError):
        return UNREADABLE
    valid = (isinstance(record, dict) and isinstance(record.get("purpose"), str) and record["purpose"].isprintable()
             and record.get("harness") in harnesses.NAMES and isinstance(record.get("pool"), str)
             and isinstance(record.get("human"), bool))
    return record if valid else UNREADABLE


def _held_for_renewal(ship: Ship, name: str) -> bool:
    """A home its teardown kept for a renewal: the service's pass settles it, and the status line names it so."""
    from . import credential
    return (ship.runtime / credential.HELD / f"{name}.json").exists()


def _unreadable_launch(name: str) -> str:
    return (f"its launch is unreadable: end its program, move runtime/trusted/{name}/{trusted_files.LAUNCH} "
            f"aside, then qd down {name}")


def _temporary_launch(ship: Ship, name: str) -> dict | str | None:
    """Read launch evidence, distinguishing an absent file from one that cannot be used."""
    path = trusted_files.path(ship.runtime, name, trusted_files.LAUNCH)
    try:
        return parse_launch(path.read_text(), name)
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, LaunchUnreadable):
        return UNREADABLE


def _leftover(ship: Ship, name: str) -> tuple[dict | str, dict | str | None] | None:
    """A temporary seat's record and the launch it left, when no live run owns the seat; None otherwise."""
    record = _temporary(ship, name)
    if record is None or _held_for_renewal(ship, name):
        return None
    launch = _temporary_launch(ship, name)
    with pending.transient_lock(ship.runtime, name) as unowned:
        if not unowned:
            return None  # its run is live and owns it; it takes its own seat down
    return record, launch


def leftover_seats(ship: Ship) -> list[str]:
    """Plain words for every temporary seat that outlived its run, for the status line, until it is gone; and for
    every retired seat's home kept for a renewal its reseed did not take, until the service's pass settles it."""
    from . import credential
    folder = ship.runtime / "seats"
    words = credential.held_words(ship)
    for path in sorted(folder.iterdir()) if folder.is_dir() else []:
        found = _leftover(ship, path.name)
        if found is None:
            continue
        record, launch = found
        if record == UNREADABLE:
            words.append(f"{path.name}: a temporary seat's record is unreadable; inspect "
                         f"runtime/trusted/{path.name}/{TEMPORARY}")  # law: nothing-waits-silently
            continue
        if launch == UNREADABLE:
            state = _unreadable_launch(path.name)
        elif (pool := (launch or {}).get("pool") or record["pool"]) is not None and pool not in ship.pools:
            state = f"its pool {pool} is not declared: declare it again, then qd down {path.name}"
        else:
            running = launch is not None and process_alive(launch["pid"], launch.get("start"))
            state = f"its program {'still runs' if running else 'has ended'}; qd down {path.name}"
        words.append(f"{path.name} outlived its {record['purpose']}: {state}")  # law: nothing-waits-silently
    return words


def is_leftover(ship: Ship, name: str) -> bool:
    """qd down takes this name as a temporary seat left behind (refused while its run is live), not a declared seat."""
    return _temporary(ship, name) is not None


def down_leftover(ship: Ship, tmux: Tmux, name: str) -> str:
    """qd down <temporary seat>: take down a seat its run left, by that run's own teardown."""
    folder = ship.runtime / "seats" / name
    record = _temporary(ship, name)
    if record is None:
        raise ShipError(f"{name}: no seat of this ship, and no temporary seat left behind by that name")
    if record == UNREADABLE:
        raise ShipError(f"{name}: its record is unreadable; inspect runtime/trusted/{name}/{TEMPORARY}")
    if _held_for_renewal(ship, name):
        raise ShipError(f"{name}: its home is held for a renewal; the service reseeds its pool from it, then removes it")
    with pending.transient_lock(ship.runtime, name) as owned:
        if not owned:
            raise ShipError(f"{name}: its {record['purpose']} is still running and takes its own seat down; nothing done")
        launch = _temporary_launch(ship, name)
        if launch == UNREADABLE:
            raise ShipError(f"{name}: {_unreadable_launch(name)}")
        harness_name = (launch or {}).get("harness") or record["harness"]
        pool = (launch or {}).get("pool") or record["pool"] or next(
            (p.name for p in ship.pools.values() if harnesses.get(harness_name).accepts(p) is None), None)
        if pool not in ship.pools:
            raise ShipError(f"{name}: its pool {pool!r} is not declared: declare it again, then qd down {name}")
        title = "qd-" + name
        spec = SeatSpec(name, title, "worker", harness_name, (pool,), cwd=ship.workspace)
        session = shipmod.Session(title, title, human=record["human"])
        treated = replace(ship, sessions={**ship.sessions, title: session}, seats={**ship.seats, name: spec})
        seat = Seat(treated, spec, tmux)
        orders = post.Store(ship.runtime).open_for(name)
        downed: list[str] = []
        with ExitStack() as cleanup:
            _teardown(cleanup, treated, seat, tmux, orders, "qd down of a temporary seat left behind",
                      lambda: tmux.kill_session(title) if tmux.has_session(title) else None,
                      down=lambda: downed.append(seat.down()))
        _, marker = pending._check_paths(ship.runtime, name)
        if not folder.exists():
            try:
                descriptor = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            except FileNotFoundError:
                descriptor = None
            if descriptor is not None:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)  # its check's lease is gone with it
                    marker.unlink()
                except BlockingIOError:
                    pass
                finally:
                    os.close(descriptor)
            return f"{name} down: " + (downed[0] if downed else "taken down") + " · its session ended, its home removed"
        return (f"{name}: " + (downed[0] if downed else "not taken down")
                + f" · its home is kept until its program has ended; qd down {name} again")


# -- the ship ------------------------------------------------------------------------------


def read_seat(ship: Ship, spec: SeatSpec, tmux: Tmux) -> Seat | UnresolvedSeat:
    try:
        return Seat(ship, spec, tmux)
    except SelectionError as error:
        return UnresolvedSeat(ship, spec, tmux, error)


def seats(ship: Ship, tmux: Tmux, strict: bool = True, *, launching: bool = False) -> list[Seat | UnresolvedSeat]:
    """Lifecycle callers refuse unknown selection; readings retain its named failure."""
    if strict:
        return [Seat(ship, spec, tmux, launching=launching) for spec in ship.seats.values()]
    return [read_seat(ship, spec, tmux) for spec in ship.seats.values()]


def bring_up(ship: Ship, tmux: Tmux) -> list[str]:
    """Every seat marked up, resuming what has a record; the rest are left as declared."""
    proofs = [shipmod.fence(ship)]
    if not ship.is_repository:
        proofs.append(f"{ship.folder.name} is not its own repository: its declaration travels with the folder above")
    from . import write_bound
    for seat in seats(ship, tmux, launching=True):
        if seat.spec.up and not seat.is_up and (held := write_bound.reason(ship.runtime, seat.name)):
            proofs.append(f"{seat.name} left down: {held}")  # only a word for this seat lifts it
        elif seat.spec.up and not seat.is_up:
            try:
                proofs.append(seat.up(resume=True))
            except shipmod.KeyRefused as refused:
                proofs.append(str(refused))  # that seat alone stays down; the rest of the ship comes up
        elif seat.is_up:
            proofs.append(f"{seat.name} already up in {seat.pane().id}")
    return proofs


def bring_down(ship: Ship, tmux: Tmux) -> list[str]:
    proofs = [seat.down() for seat in seats(ship, tmux)]
    for session in ship.sessions.values():
        if (tmux.has_session(session.title)
                and tmux.session_environment(session.title, "QD_SHIP") == str(ship.folder)
                and not tmux.panes(session.title)):
            tmux.kill_session(session.title)
    return proofs
