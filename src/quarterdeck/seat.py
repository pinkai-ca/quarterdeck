"""The Seat: one running harness in one tmux pane under one role, and its lifecycle.

Up, down, restart and fresh are one relaunch path with one difference, whether the program
resumes its record. A seat's identity is its pane id, its process id and its record; the
persona and the window name are display (law: identity).
"""

from __future__ import annotations

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
from .provenance import remember_launch
from .pricing import save_price, saved_price
from .ship import Pool, SeatSpec, Ship, ShipError
from .tmux import Pane, Tmux, check_private_folder


PROCESS_END_SECONDS = 10.0  # how long down watches a closed pane's program end before it leaves the rest to the tick
RELAUNCH_END_SECONDS = 60.0  # a relaunch (restart, fresh, a pool move) watches a program end this long in all
RETIRING = "retiring.json"
REFUSED_RELAUNCH = "relaunch-refused.json"  # a relaunch refused while the old program was ending, until up succeeds


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
    another program left in the server's environment, removed by name.
    """
    reach = ["QD_TMUX_SOCKET=" + socket] if socket else ["-u", "QD_TMUX_SOCKET"]
    unset = [word for name in markers for word in ("-u", name)]
    # Add this after launch.sh's sh: a shell may discard imported function variables. Child Bash
    # shells import it before reading their rc files, so a later PATH prepend cannot shadow qd.
    function = ([f'BASH_FUNC_qd%%=() {{ {shlex.quote(str(qd_command))} "$@"; }}']
                if qd_command is not None else [])
    return ["env", "-u", "TMUX", "-u", "TMUX_PANE", *unset, *reach, f"TMUX_TMPDIR={tmux_tmpdir}", f"PATH={path}",
            *function, *argv]


def launch_script(seat: str, words: list[str]) -> str:
    """The seat's command as a script for tmux to run: tmux refuses a window command over 16 KiB, and a
    brief or a first message can be longer. The PATH travels inside it, since the login shell resets the
    one tmux passes."""
    return (f"#!/bin/sh\n# {seat}: the command qd up ran, kept in a file because tmux's command line is short\n"
            f"exec {shlex.join(words)}\n")


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


EXPLICIT = 'pool-explicit'  # beside ``pool``: a person chose that pool for the next launch, which takes it


class Seat:
    def __init__(self, ship: Ship, spec: SeatSpec, tmux: Tmux, *, launching: bool = False):
        self.ship, self.spec, self.tmux = ship, spec, tmux
        # A launch of a seat with no launch on file reads a saved choice the ship file no longer declares as
        # absent, so the declared order chooses (``launch_choice``); every other reader still refuses it.
        self._launching = launching and not (ship.runtime / "seats" / spec.name / "launch.json").exists()
        self.folder = ship.runtime / "seats" / spec.name
        self.home = self.folder / "home"
        self._prepared_observation: tuple[str | None, str | None] | None = None
        try:
            self.spec = replace(spec.for_pool(self.pool.name), human_session=self.session.human)
            self.harness: Harness = harnesses.get(self.spec.harness)
            if self.spec.pool_choices and (launched := self.launched()) is not None:
                if launched.pool != self.pool.name or record_owner.launch_reader(launched) != self.spec.harness:
                    raise ShipError(f"{self.name}: saved launch and selected program disagree; restore its pool declaration/selection")
        except (ShipError, OSError, ValueError, TypeError) as error:
            reason = str(error).removeprefix(f'{spec.name}: ')
            raise SelectionError(spec.name, f"{spec.name}: selection unknown: {reason}") from error

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def session(self) -> shipmod.Session:
        return self.ship.session_of(self.spec)

    # -- state files -------------------------------------------------------------------

    @property
    def pool(self) -> Pool:
        """The pool the seat is on: the first declared, or the one failover moved it to."""
        current = self.folder / "pool"
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
                elif any((self.folder / file).exists() for file in
                         ('record', record_owner.REGISTRY, 'record-launches.jsonl')):
                    raise ShipError(f"{self.name}: saved pool missing with retained record; restore the selected pool")
        return self.ship.pool(name if name in self.spec.pools else self.spec.pools[0])

    def move_to(self, pool: str, *, explicit: bool = False) -> None:
        self.spec.for_pool(pool)
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / 'pool'
        durable.replace_file(path, pool + '\n')
        if explicit:
            durable.replace_file(self.folder / EXPLICIT, pool + '\n')
        else:
            (self.folder / EXPLICIT).unlink(missing_ok=True)

    def _saved_name(self) -> str | None:
        try:
            return (self.folder / 'pool').read_text().strip() or None
        except FileNotFoundError:
            return None

    def launch_choice(self) -> tuple[str, str]:
        """The pool a launch of this down seat takes, and why: the ship file is the Admiral's order.

        A person's explicit choice for this launch (``qd set pool=`` on a down seat) is taken as it is. Otherwise
        the first declared choice with room, one the ledger does not show walled, wins over the saved choice:
        a saved choice later in the list than a choice with room, or one the ship file no longer declares,
        gives way. With no choice that has room, the saved choice stays, or the first declared one.
        """
        pools = self.spec.pools
        current = self.pool.name
        saved = self._saved_name() or current
        try:
            explicit = (self.folder / EXPLICIT).read_text().strip()
        except FileNotFoundError:
            explicit = None
        if explicit is not None and explicit == current:
            return current, 'chosen for this launch by qd set'
        from . import ledger, quota
        book = ledger.Ledger.reading(self.ship.runtime / 'ledger.sqlite')
        history_pending = False
        try:
            for name in pools:
                pool = self.ship.pool(name)
                if book is not None and pool.kind == 'key' and pool.budget is not None and book.catching_up():
                    history_pending = True
                    continue
                if book is None or not book.walled(name, self.ship, scope=quota.model_scope(self, name)):
                    return name, ('its saved choice' if name == saved else
                                  f'the first declared choice with room, before its saved {saved}')
        finally:
            if book is not None:
                book.close()
        if history_pending:
            return current, 'saved choice retained; budget room unknown while accounting catches up'
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
        if same_program:
            self.harness.preflight_resume(selected, self.home, self._resume_id)
        self.record()
        credential.reseed(self.ship, [self], wait=credential.DOWN_WAIT)
        stopped = self._relaunch_down()
        self.move_to(pool)
        self.spec = selected
        if not same_program:
            self.harness = harnesses.get(selected.harness)
        return stopped + '\n' + self._up(resume=same_program, observation=observation)

    def launched(self) -> Launched | None:
        path = self.folder / "launch.json"
        try:
            text = path.read_text()
        except FileNotFoundError:
            return None
        data = json.loads(text)
        if data.get('recovery') is not None:
            from .harness.thinking import validate_launch
            validate_launch(data['recovery'])
        return Launched(**data, price=saved_price(path))

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
            written = (self.folder / "launch.json").stat().st_mtime
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
        updated.write(self.folder / "launch.json")
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

    def join(self) -> Join:
        """The seat's record: pinned once found, the first written after its launch."""
        pinned = self.folder / "record"
        if pinned.is_file():
            try:
                named = pinned.read_text().strip()
            except (OSError, UnicodeDecodeError) as error:
                return Join(None, f"record pointer unreadable: {error}; inspect with qd watch {self.name}")
            if not named:  # an emptied pointer names no record; Path('') would be the working folder
                return Join(None, f"record pointer empty: {pinned}; restore the owned record if available; inspect with qd watch {self.name}")  # law: one-record
            record = Path(named)
            try:
                owner = record_owner.pinned_reader(self, record)
            except OSError as error:
                return Join(None, f"record reader unknown: {error}; inspect with qd watch {self.name}")
            if owner != self.spec.harness:
                return Join(None, f"record reader {'unknown' if owner is None else 'changed'}: retained {owner or 'untyped'} record {record.name}; "
                            f"historical reader evidence is unchanged by a new launch; inspect with qd watch {self.name}")
            if self.launched() is not None and record_owner.launch_reader(self.launched()) != owner:
                return Join(None, f"record reader changed: {record.name} and saved launch disagree; inspect with qd watch {self.name}")
            if record.exists():
                self._note_owned(record)
                return Join(record)
            return Join(None, f"record missing: {record.name}; restore the owned record if available; inspect with qd watch {self.name}")
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
        if found.record is not None:
            durable.replace_file(pinned, str(found.record) + "\n")  # law: one-record
            self._note_owned(found.record)
        return found

    def _note_owned(self, record: Path) -> None:
        record_owner.register(self.folder, record, self.spec.harness)
        launched = self.launched()
        remember_launch(self.folder, record, launched if record_owner.launch_reader(launched) == self.spec.harness else None)
        owned = self.home / "conversations.owned"
        self.home.mkdir(parents=True, exist_ok=True)
        existing = owned.read_text().splitlines() if owned.is_file() else []
        if str(record) not in existing:
            owned.write_text("".join(f"{line}\n" for line in [*existing, str(record)]))

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
                    pinned = sibling.folder / "record"
                    if not pinned.is_file():
                        return True
                    try:
                        record = Path(pinned.read_text().strip())
                        if not record.exists() or record_owner.pinned_reader(sibling, record) != sibling.spec.harness:
                            return True
                    except OSError:
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
        return self.join().record

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
            if pool is not None and (not self.spec.pool_choices or pool not in self.spec.pools):
                raise ShipError(f"{self.name}: --pool {pool!r} is not one of its declared choices"
                                + (f" ({', '.join(self.spec.pools)})" if self.spec.pool_choices else ""))
            reported = (resume and self.spec.fresh == 'after-order' and not self.is_up
                        and self._last_delivered_order_reported())
            if reported:
                resume = False
            def proof(text):
                return text + (' · fresh: its last order is reported' if reported else '')
            if not self.spec.pool_choices or self.is_up:
                return proof(self._up(resume))
            from .recovery import require_clear
            require_clear(self)
            target, why = (pool, 'chosen for this launch by qd up --pool') if pool is not None else self.launch_choice()
            current = self.pool.name
            saved = self._saved_name()
            if target == current and pool is None and why == 'its saved choice':
                (self.folder / EXPLICIT).unlink(missing_ok=True)
                return proof(self._up(resume))  # nothing chosen: the launch is as it always was
            selected = replace(self.spec.for_pool(target), human_session=self.session.human)
            observation = self._observe_selection(selected, self.ship.pool(target))
            same_program = selected.harness == self.spec.harness
            if same_program and resume:
                self.harness.preflight_resume(selected, self.home, self._resume_id)
            if self.launched() is not None:
                self._relaunch_down()  # retain the old record and reseed its source pool before changing selection
            self.move_to(target)
            self.spec = selected
            if not same_program:
                self.harness = harnesses.get(selected.harness)
            started = proof(self._up(resume=resume and same_program, observation=observation))
            moved = f"{self.name}: launched on {target} ({why})" + (
                f"; its saved choice was {saved}" if saved is not None and target != saved else "")
            return moved + '\n' + started

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
        reader = harnesses.get(selected.harness)
        if reason := reader.accepts(paid):
            raise ShipError(f"{self.name}: {reason}")
        validate_selection(selected, reader)
        reader.check_defaults(selected, self.ship.folder / 'homes' / reader.name)

    def _observe_selection(self, spec: SeatSpec | None = None, pool: Pool | None = None):
        """Reuse the normal version observation before any retirement or startup."""
        selected = spec or self.spec
        self._accept_pool(selected, pool)
        reader = harnesses.get(selected.harness)
        if reader.private_version_home:
            shipmod.fence(self.ship)
        if self._prepared_observation is not None and spec is None:
            observation, self._prepared_observation = self._prepared_observation, None
            return observation
        version = _version_in_runtime(reader, self.ship.runtime)
        note = validate_selection(selected, reader, version=version, installed=True)
        return version, note

    def _up(self, resume: bool = True, *, observation=None) -> str:
        self._accept_pool()
        if launchable := getattr(self.tmux, "launchable", None):
            launchable()  # a socket too long or not this user's refuses before anything of the launch is made
        from .recovery import require_clear
        require_clear(self)
        if (held := self.retiring()) is not None:
            if not isinstance(held.get("pid"), int) or process_alive(held["pid"], held.get("start")):
                raise ShipError(f"{self.name}: the program its last launch ran is still ending (pid {held.get('pid')}); "
                                "up waits for it, so nothing it writes meets the new launch")
            self._finish_retirement(self.launched())
        previous = self.launched()
        left = self._lifecycle_pane(previous)
        if left is not None and not left.dead:
            raise ShipError(f"{self.name} is up in {left.id}; qd restart {self.name} relaunches it")
        if self.spec.remote_control and not self.harness.remote_control:
            raise ShipError(f"{self.name}: remote_control = true, and {self.harness.name} has no Remote Control; "
                            "select a harness that offers it or drop the key")
        if self.ship.jailed(self.spec, self.pool):
            from . import sandbox  # the sandbox reads the ship and the standing orders, so it is imported here, not above

            # A jailed seat's own folder under its ship's work/, as declared, never through a link, before anything
            # of the launch is made (law: one-folder).
            sandbox.check_own_work(self.ship, self.name, self.spec)
        version, effort_note = observation if observation is not None else self._observe_selection()
        shipmod.fence(self.ship)
        self.folder.mkdir(parents=True, exist_ok=True)
        if left is not None and left.dead:
            self._retire_window(previous)  # the last launch's failed pane goes only with matching identity
        if self.launched() is not None:
            from . import credential  # the credential module reads seats, so it is imported here, not above

            credential.reseed(self.ship, [self], wait=credential.DOWN_WAIT)  # a refresh left in this home reseeds the pool it came from, before any move
        prior = {item.path for item in record_owner.records(self)}
        prior.update(self.harness.records(self.home))
        for record in prior:
            record_owner.register(self.folder, record, None)  # retain even untyped history before clearing a fresh pin
        brief = standing.render(self.ship, self.spec)
        pool = self.pool
        ship_home = self.ship.folder / "homes" / self.harness.name
        spec = replace(self.spec, cwd=self.ship.cwd_of(self.spec))  # the adapter sees the seat's real working directory
        self.harness.build_home(self.home, spec, pool, ship_home if ship_home.is_dir() else None)
        resume_id = self._resume_id() if resume else None
        if resume and resume_id is None and prior:
            self.harness.preflight_resume(spec, self.home, lambda: None)
        carriage = self.carriage(resume_id=resume_id)  # read while the last record is still pinned
        carried = carriage.carried
        if not resume_id:
            (self.folder / "record").unlink(missing_ok=True)
        path = self._path()
        jailed = self.ship.jailed(self.spec, pool)

        def command(opening: str | None) -> tuple[Launch, list[str]]:
            launch = self.harness.launch(spec, pool, self.home, brief, resume_id, opening)
            if shutil.which(launch.argv[0], path=path) is None:
                raise ShipError(f"{self.name}: {launch.argv[0]} is not on the PATH qd runs with; install it, or put it on that PATH")
            argv = list(launch.argv)
            if jailed:
                from . import sandbox  # the sandbox reads the ship and the standing orders, so it is imported here, not above

                endpoint = pool.endpoint if pool.kind == 'endpoint' else None  # its one way out, relayed in
                jail = sandbox.plan(self.ship, self.name, self.folder, self.home, self.ship.cwd_of(self.spec), launch.argv[0], path,
                                    endpoint=endpoint, pool=pool, synthetic=self.harness.synthetic)
                argv = sandbox.command(jail, argv)
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
                # law: context-is-cache -- only admitted self-orders are full, and only on an actual fresh conversation
                return carriage_plan.build(carriage.rows, handoff=carriage.text or '', header=carriage.header,
                                           directory=generation, limit=getattr(self.ship, 'carry_bytes', shipmod.CARRY_BYTES),
                                           inline_limit=carriagemod.INLINE_BYTES, force_files=force_files)
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
        launched.write(self.folder / "launch.json")
        (self.folder / REFUSED_RELAUNCH).unlink(missing_ok=True)  # up again: a refused relaunch is over
        if self.spec.pool_choices:
            self.move_to(pool.name)
        if resume_id:
            self.record()  # retain the resumed record's new pool boundary immediately
        self._survives(pane.id, launch.argv[0])
        attempts = post.Attempts(self.ship.runtime)
        for message in carried:
            attempt = attempts.open(message, self.name, pane.id, pane.pid, launched.at)
            attempts.close(attempt, "launch", post_threads.INDEXED_LAUNCH_DETAIL)
        how = f"resumed {resume_id[:8]}" if resume_id else "fresh"
        carried_note = f" · {len(carriage.rows)} thread{'s' if len(carriage.rows) != 1 else ''} indexed"
        if carried_file:
            carried_note += f" in {carried_file.relative_to(self.ship.runtime)}"
        jail_note = " · jailed: the operator's home hidden, the workspace writable" if jailed else ""
        notice_note = f" · {launched.notice}" if launched.notice else ""
        unchecked_note = f" · {effort_note}" if effort_note else ""
        return f"{self.name} up in {pane.id} · {self.harness.name} {launched.version or ''} · provider {providers.display(pool.provider)} · pool {pool.name} · {how}{carried_note}{jail_note}{notice_note}{unchecked_note}"

    def _resume_id(self) -> str | None:
        record = self.record()
        try:
            return self.harness.session_id(record) if record else None
        except OSError:
            return None

    def first_message(self, *, resume_id: str | None = None) -> tuple[str | None, list[post.Message]]:
        """Preview the policy-bounded first message without publishing its files."""
        from . import carriage_plan
        carriage = self.carriage(resume_id=resume_id)
        planned = carriage_plan.build(carriage.rows, handoff=carriage.text or '', header=carriage.header,
                                      directory=self.folder / carriagemod.FOLDER / 'preview',
                                      limit=getattr(self.ship, 'carry_bytes', shipmod.CARRY_BYTES))
        return planned.opening, carriage.carried

    def carriage(self, *, resume_id: str | None = None) -> carriagemod.Carriage:
        """An index of incoming work and outgoing waits; fresh self-handoffs alone are full."""
        from . import refusal, thread_context, post_threads

        store, attempts = post.Store(self.ship.runtime), post.Attempts(self.ship.runtime)
        with post.admission(self.ship.runtime, self.name):
            try:
                refused = refusal.excluded(self.ship.runtime, self.name)
            except refusal.JournalError as error:
                raise ShipError(f"{self.name}: {error}; inspect it before a launch carries anything") from None
            history = [attempt for attempt in attempts.all() if attempt.seat == self.name]
            latest = {attempt.message: attempt for attempt in history}
            unread = attempts.undelivered(store, self.name, results={key: value.result for key, value in latest.items()}, for_launch=True)
            queued = self._queued_unread(store, attempts)
            extra = unread + queued
            consumed = self._consumed_messages(store, attempts, resume_id, refused)
            selection = thread_context.select(store, self.name, extra, consumed=consumed, excluded=refused)
            messages = store.all()
            sent = tuple(message for message in messages
                         if message.sender == self.name and message.to != self.name and store.is_open(message))
            pending_messages = tuple(message for message in extra if message.id not in consumed)
            delivered = {attempt.message for attempt in history if attempt.result in ('delivered', 'watch')}
            queued_ids = {message.id for message in queued}
            unconfirmed = tuple(message for message in store.for_seat(self.name)
                                if message.kind != 'ack' and message.id not in consumed and message.id not in delivered
                                and not ((attempt := latest.get(message.id)) is not None
                                         and attempt.result == 'queued' and message.id not in queued_ids))
            rows = post_threads.index_rows(store, selection.roots, (*pending_messages, *unconfirmed), refused, sent,
                                           seat=self.name)
        handoffs = [entry.message for entry in selection.entries
                    if not resume_id and not entry.context_only and entry.message.kind == 'order'
                    and entry.message.sender == self.name and entry.message.to == self.name]
        carried = list({message.id: message for message in (*selection.roots, *pending_messages)
                        if message.id not in refused}.values())
        header = post.briefed_header(self.name, self.spec.role, list(selection.roots))
        return carriagemod.Carriage(post.typed(handoffs, whole=True) if handoffs else None, carried, header, rows)

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
            self.tmux.new_session(session.title, env=ship_env)
            status = shlex.join([str(qd_bin() / "qd"), "--ship", str(self.ship.folder), "status", "--line"])
            self.tmux.status_line(session.title, status)
        env = {**env, **ship_env, "QD_SEAT": self.name}
        cwd = self.ship.cwd_of(self.spec)
        if not cwd.is_dir():
            raise ShipError(f"{self.name}: working directory {cwd} does not exist; qd creates nothing outside its folder")  # law: one-folder
        script = self.folder / "launch.sh"
        script.write_text(launch_script(self.name, self._launch_words(argv, env, path)))
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
        self._retire_window(launched)
        if self.launched() != launched:
            raise ShipError(f"{self.name}: launch identity changed during retirement; launch retained")
        waited = ""
        if program is not None and not wait_for_end(*program, PROCESS_END_SECONDS):
            started = time.monotonic() - PROCESS_END_SECONDS
            if not (relaunch and wait_for_end(*program, RELAUNCH_END_SECONDS - PROCESS_END_SECONDS)):
                # The pane is closed but the program still runs: it may still write its credential or record.
                # Nothing is reseeded and the launch stays, for the service's tick to finish once it has ended.
                durable.replace_file(self.folder / RETIRING, json.dumps(dict(pid=program[0], start=program[1])) + "\n")
                bound = RELAUNCH_END_SECONDS if relaunch else PROCESS_END_SECONDS
                if relaunch:
                    durable.replace_file(self.folder / REFUSED_RELAUNCH, json.dumps(dict(
                        at=datetime.now(timezone.utc).isoformat(), pid=program[0])) + "\n")
                return (f"{self.name} down · pane {pane.id} closed · process still ending after {bound:g}s; "
                        + ("relaunch refused; " if relaunch else "") + "credential and launch left for the next tick")
            waited = (f" · still ending after {PROCESS_END_SECONDS:g}s, ended after "
                      f"{time.monotonic() - started:.0f}s; the relaunch follows")
        self._finish_retirement(launched)
        if pane is None:
            return f"{self.name} was not up" + (f" · pane {launched.pane} is gone" if launched else "")
        return f"{self.name} down · pane {pane.id} closed · process ended" + waited

    def _finish_retirement(self, launched: Launched | None) -> None:
        if launched is not None:
            from .pending_lifecycle import link_launch_record
            link_launch_record(self, launched)  # include a last write before the program ended
            from . import credential  # the credential module reads seats, so it is imported here, not above

            credential.reseed(self.ship, [self], wait=credential.DOWN_WAIT)  # the program has ended: a refresh it left reseeds the pool its launch names, while it is named
        (self.folder / "launch.json").unlink(missing_ok=True)
        (self.folder / RETIRING).unlink(missing_ok=True)

    def relaunch_refused(self) -> str | None:
        """Plain words for the status line while the seat is down because its relaunch met a program still ending."""
        held = self.retiring()
        if held is not None:
            return f"{self.name}: its program is still ending; " + (
                "its relaunch was refused" if (self.folder / REFUSED_RELAUNCH).exists() else "it is not retired yet")
        if (self.folder / REFUSED_RELAUNCH).exists():
            return f"{self.name}: its relaunch was refused while its program was ending; qd up {self.name}"
        return None

    def retiring(self) -> dict | None:
        """The program a closed pane left still running at down: its pid and start, until it has ended."""
        try:
            data = json.loads((self.folder / RETIRING).read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return {"pid": None, "start": None}  # unreadable: held as still ending, never finished on a guess
        return data if isinstance(data, dict) else {"pid": None, "start": None}

    def finish_retirement(self) -> str | None:
        """The service's tick: reseed and remove the launch once the program down left running has ended."""
        with self._lifecycle():
            held = self.retiring()
            if held is None:
                return None
            if not isinstance(held.get("pid"), int) or process_alive(held["pid"], held.get("start")):
                return None
            self._finish_retirement(self.launched())
            return f"{self.name} retired · process {held['pid']} ended · credential reseeded, launch removed"

    def restart(self) -> str:
        """Relaunch with resume: the conversation continues in a new process."""
        with self._lifecycle():
            self._accept_pool()
            from .recovery import require_clear
            require_clear(self)
            if moved := self._relaunch_off_wall(resume=True):
                return moved
            self.harness.preflight_resume(self.spec, self.home, self._resume_id)
            observation = self._observe_selection()
            return self._relaunch_down() + "\n" + self._up(resume=True, observation=observation)

    def fresh(self) -> str:
        """Relaunch without resume: a new conversation that begins with the brief and the open messages."""
        with self._lifecycle():
            self._accept_pool()
            from .recovery import require_clear
            require_clear(self)
            if moved := self._relaunch_off_wall(resume=False):
                return moved
            observation = self._observe_selection()
            return self._relaunch_down() + "\n" + self._up(resume=False, observation=observation)

    def _walled_now(self) -> bool:
        """Whether the ledger shows the seat's current pool walled, under its running model's scope."""
        from . import ledger, quota
        book = ledger.Ledger.reading(self.ship.runtime / 'ledger.sqlite')
        if book is None:
            return False
        try:
            return quota.current_walled(self, book)
        finally:
            book.close()

    def _relaunch_off_wall(self, *, resume: bool) -> str | None:
        """A relaunch never lands on a walled pool while a declared choice has room: it takes the launch's choice.

        Only a seat whose current pool is walled moves; one with room relaunches where it is. None: nothing moved.
        """
        if not self.spec.pool_choices or not self._walled_now():
            return None
        target, why = self.launch_choice()
        came_from = self.pool.name
        if target == came_from:
            return None  # every declared choice is walled, or this one was chosen by qd set: it stays
        selected = replace(self.spec.for_pool(target), human_session=self.session.human)
        observation = self._observe_selection(selected, self.ship.pool(target))
        same_program = selected.harness == self.spec.harness
        if same_program and resume:
            self.harness.preflight_resume(selected, self.home, self._resume_id)
        stopped = self._relaunch_down()
        self.move_to(target)
        self.spec = selected
        if not same_program:
            self.harness = harnesses.get(selected.harness)
        proof = self._up(resume=resume and same_program, observation=observation)
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
        if (spec.name in ship.seats or spec.session in ship.sessions or tmux.has_session(session.title)
                or seat.folder.exists() or (ship.runtime / "post" / "pending" / seat.name).exists()
                or store.for_seat(seat.name)
                or any(a.seat == seat.name for a in post.Attempts(ship.runtime).all())):
            raise ShipError(f"{seat.name}: temporary seat/session/history already exists; retry {purpose}")
        seat.folder.mkdir(parents=True, exist_ok=False)
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
        launched = seat.launched()  # the program to prove ended before the home goes
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
        if retired and not running and not (seat.folder / "launch.json").exists():
            from . import credential

            credential.reseed(treated, [seat], wait=credential.DOWN_WAIT)  # its one pool: a refresh left in the home is not deleted with it
            shutil.rmtree(seat.folder)

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


# -- a check's seat that outlived its check ----------------------------------------------------

CHECK_SEAT = re.compile(r"check-(?:wake-)?[a-z][a-z0-9]*(?:-[a-z0-9]+)*-[0-9a-f]{4}")


def _leftover(ship: Ship, name: str) -> dict | None:
    """The launch a check's seat left, when no running check owns the seat; None otherwise."""
    if name in ship.seats or not CHECK_SEAT.fullmatch(name):
        return None
    try:
        launch = json.loads((ship.runtime / "seats" / name / "launch.json").read_text())
    except FileNotFoundError:
        return None  # no launch: nothing of it can be running
    except (OSError, ValueError):
        launch = {}
    with pending.transient_lock(ship.runtime, name) as unowned:
        if not unowned:
            return None  # its check is running and owns it; it takes its own seat down
    return launch if isinstance(launch, dict) else {}


def leftover_checks(ship: Ship) -> list[str]:
    """Plain words for every check's seat that outlived its check, for the status line, until it is gone."""
    folder = ship.runtime / "seats"
    words = []
    for path in sorted(folder.glob("check-*")) if folder.is_dir() else []:
        launch = _leftover(ship, path.name)
        if launch is None:
            continue
        pid, start = launch.get("pid"), launch.get("start")
        state = ("its program still runs" if isinstance(pid, int) and process_alive(pid, start)
                 else "its program has ended" if isinstance(pid, int) else "its launch is unreadable")
        words.append(f"{path.name} outlived its check: {state}; qd down {path.name}")  # law: nothing-waits-silently
    return words


def down_leftover_check(ship: Ship, tmux: Tmux, name: str) -> str:
    """qd down check-<program>-<run>: take down a check's seat that its check left, by the check's own teardown."""
    folder = ship.runtime / "seats" / name
    if not CHECK_SEAT.fullmatch(name) or name in ship.seats or not folder.is_dir():
        raise ShipError(f"{name}: no seat of this ship, and no check's seat left behind by that name")
    with pending.transient_lock(ship.runtime, name) as owned:
        if not owned:
            raise ShipError(f"{name}: its check is still running and takes its own seat down; nothing done")
        try:
            launch = json.loads((folder / "launch.json").read_text())
        except FileNotFoundError:
            launch = None
        except (OSError, ValueError) as error:
            raise ShipError(f"{name}: its launch is unreadable ({error}); retained for inspection") from None
        harness_name = (launch or {}).get("harness") or name.removeprefix("check-").removeprefix("wake-").rsplit("-", 1)[0]
        pool = (launch or {}).get("pool") or next(
            (p.name for p in ship.pools.values() if harnesses.get(harness_name).accepts(p) is None), None)
        if pool not in ship.pools:
            raise ShipError(f"{name}: its pool {pool!r} is not declared; retained for inspection")
        human = name.startswith("check-wake-")
        title = "qd-" + name
        spec = SeatSpec(name, title, "worker", harness_name, (pool,), cwd=ship.workspace)
        session = shipmod.Session(title, title, human=human)
        treated = replace(ship, sessions={**ship.sessions, title: session}, seats={**ship.seats, name: spec})
        seat = Seat(treated, spec, tmux)
        orders = post.Store(ship.runtime).open_for(name)
        downed: list[str] = []
        with ExitStack() as cleanup:
            _teardown(cleanup, treated, seat, tmux, orders, "qd down of a check's seat left behind",
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
    for seat in seats(ship, tmux, launching=True):
        if seat.spec.up and not seat.is_up:
            proofs.append(seat.up(resume=True))
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
