"""The Seat: one running harness in one tmux pane under one role, and its lifecycle.

Up, down, restart and fresh are one relaunch path with one difference, whether the program
resumes its record. A seat's identity is its pane id, its process id and its record; the
persona and the window name are display (law: identity).
"""

from __future__ import annotations

import json
import fcntl
import os
import shlex
import shutil
import sys
import stat
import time
from contextlib import contextmanager, ExitStack
from typing import Iterator
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from . import durable, harness as harnesses, input_receipt, pending, post, record_owner, ship as shipmod, standing
from .harness.base import Harness, Join
from .provenance import remember_launch
from .pricing import save_price, saved_price
from .ship import Pool, SeatSpec, Ship, ShipError
from .tmux import Pane, Tmux


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

    def write(self, path: Path) -> None:
        data = asdict(self)
        price = data.pop("price")
        if data['recovery'] is None:
            data.pop('recovery')  # unchanged launch shape for programs without this optional proof
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
    """The PATH a seat's program runs with, and the one its program is looked for on: the folder holding ``qd`` first, so the seat can post."""
    return os.pathsep.join(filter(None, [str(qd_bin()), os.environ.get("PATH", "")]))


def launch_script(seat: str, path: str, argv: list[str]) -> str:
    """The seat's command as a script for tmux to run: tmux refuses a window command over 16 KiB, and a
    brief or a first message can be longer. The PATH travels inside it, since the login shell resets the
    one tmux passes."""
    return (f"#!/bin/sh\n# {seat}: the command qd up ran, kept in a file because tmux's command line is short\n"
            f"exec env {shlex.quote(f'PATH={path}')} {shlex.join(argv)}\n")


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


class Seat:
    def __init__(self, ship: Ship, spec: SeatSpec, tmux: Tmux):
        self.ship, self.spec, self.tmux = ship, spec, tmux
        self.folder = ship.runtime / "seats" / spec.name
        self.home = self.folder / "home"
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

    def move_to(self, pool: str) -> None:
        self.spec.for_pool(pool)
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / 'pool'
        durable.replace_file(path, pool + '\n')

    def failover_to(self, pool: str) -> str:
        """Preserve the source before selecting another program or paying pool."""
        with self._lifecycle():
            return self._failover_to(pool)

    def _failover_to(self, pool: str) -> str:
        from .recovery import require_clear
        require_clear(self)
        from . import credential

        selected = self.spec.for_pool(pool)
        same_program = selected.harness == self.spec.harness
        self.record()
        credential.reseed(self.ship, [self])
        stopped = self._down()
        self.move_to(pool)
        self.spec = selected
        if not same_program:
            self.harness = harnesses.get(selected.harness)
        return stopped + '\n' + self._up(resume=same_program)

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

    def read_pane(self, history: int = 0) -> tuple[Pane, str] | None:
        """A fresh screen still belongs to the launch whose identity we checked."""
        if not hasattr(self.tmux, 'read_pane'):
            pane = self.pane()
            return (pane, self.tmux.capture(pane.id, history=history)) if pane and not pane.dead else None
        launched = self.launched()
        if launched is None:
            return None
        found = self.tmux.read_pane(launched.pane, history)
        if (found is None or found[0].pid != launched.pid or found[0].dead
                or self.launched() != launched):
            return None  # law: identity
        return found

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
        """Whether a turn is open: from the record first, from the screen when its state is unknown."""
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
        return self.harness.screen(self.tmux.capture(pane.id)).working

    def record(self) -> Path | None:
        return self.join().record

    # -- the one relaunch path -----------------------------------------------------------

    @contextmanager
    def _lifecycle(self) -> Iterator[None]:
        """One caller owns the whole seat transition, including compound relaunches.

        Keep the empty inode outside removable seat homes. No lifecycle path unlinks
        it; old binaries do not participate in this advisory coordination protocol.
        """
        path = self.ship.runtime / 'post' / 'lifecycle' / f'{self.name}.lock'
        descriptor = None
        try:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW, 0o600)
                os.set_inheritable(descriptor, False)
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode):
                    raise ShipError(f'{self.name}: lifecycle lock is not a regular file; retained')
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ShipError(f'{self.name}: another lifecycle transition is in progress; retry when it ends') from None
                current = path.lstat()
                if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                    raise ShipError(f'{self.name}: lifecycle lock identity changed; transition refused')
            except OSError as error:
                raise ShipError(f'{self.name}: lifecycle coordination unavailable: {error}') from error
            yield
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def up(self, resume: bool = True) -> str:
        with self._lifecycle():
            return self._up(resume)

    def _up(self, resume: bool = True) -> str:
        from .recovery import require_clear
        require_clear(self)
        previous = self.launched()
        left = self._lifecycle_pane(previous)
        if left is not None and not left.dead:
            raise ShipError(f"{self.name} is up in {left.id}; qd restart {self.name} relaunches it")
        if self.spec.remote_control and not self.harness.remote_control:
            raise ShipError(f"{self.name}: remote_control = true, and {self.harness.name} has no Remote Control; "
                            "select a harness that offers it or drop the key")
        shipmod.fence(self.ship)
        self.folder.mkdir(parents=True, exist_ok=True)
        if left is not None and left.dead:
            self._retire_window(previous)  # the last launch's failed pane goes only with matching identity
        if self.launched() is not None:
            from . import credential  # the credential module reads seats, so it is imported here, not above

            credential.reseed(self.ship, [self])  # a refresh left in this home reseeds the pool it came from, before any move
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
        first, carried = self.first_message()  # read while the last record is still pinned
        if not resume_id:
            (self.folder / "record").unlink(missing_ok=True)
        launch = self.harness.launch(spec, pool, self.home, brief, resume_id, first)
        path = self._path()
        if shutil.which(launch.argv[0], path=path) is None:
            raise ShipError(f"{self.name}: {launch.argv[0]} is not on the PATH qd runs with; install it, or put it on that PATH")
        argv = list(launch.argv)
        jailed = self.ship.jailed(self.spec, pool)
        if jailed:
            from . import sandbox  # the sandbox reads the ship and the standing orders, so it is imported here, not above

            jail = sandbox.plan(self.ship, self.name, self.folder, self.home, self.ship.cwd_of(self.spec), launch.argv[0], path)
            argv = sandbox.command(jail, argv)
        at = datetime.now(timezone.utc).isoformat()  # before the program can write a record (law: one-record)
        pane = self._open_window(argv, launch.env, path)
        launched = Launched(pane.id, pane.pid, at, pool.name, resume_id, argv, self.harness.version(), self.spec.harness,
                            sorted(map(str, prior)), launch.price, launch.recovery)
        launched.write(self.folder / "launch.json")
        if self.spec.pool_choices:
            self.move_to(pool.name)
        if resume_id:
            self.record()  # retain the resumed record's new pool boundary immediately
        self._survives(pane.id, launch.argv[0])
        attempts = post.Attempts(self.ship.runtime)
        for message in carried:
            attempt = attempts.open(message, self.name, pane.id, pane.pid, launched.at)
            attempts.close(attempt, "launch", "carried as the first message of the launch")
        how = f"resumed {resume_id[:8]}" if resume_id else "fresh"
        carried_note = f" · {len(carried)} open message{'s' if len(carried) != 1 else ''} carried" if carried else ""
        jail_note = " · jailed: the operator's home hidden, the workspace writable" if jailed else ""
        return f"{self.name} up in {pane.id} · {self.harness.name} {launched.version or ''} · pool {pool.name} · {how}{carried_note}{jail_note}"

    def _resume_id(self) -> str | None:
        record = self.record()
        try:
            return self.harness.session_id(record) if record else None
        except OSError:
            return None

    def first_message(self) -> tuple[str | None, list[post.Message]]:
        """What the launch carries as its first message, under a header saying the seat was briefed.

        Every open message, whatever reached the seat before, plus what never reached it and
        what was queued in a turn that never read it (law: context-is-cache).
        """
        store, attempts = post.Store(self.ship.runtime), post.Attempts(self.ship.runtime)
        open_messages = store.open_for(self.name)
        carried = set(open_messages) | set(attempts.undelivered(store, self.name)) | set(self._queued_unread(store, attempts))
        if not carried:
            return None, []
        ordered = sorted(carried, key=lambda m: (m.at, m.id))
        return post.briefed_header(self.name, self.spec.role, open_messages) + "\n" + post.typed(ordered), ordered

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
                    if input_receipt.confirmed(item.reader, item.path, post.typed([message]), latest[message.id].receipt_since) == "delivered":
                        consumed = True
                        break
                except OSError:
                    continue
            if not consumed:
                unread.append(message)
        return unread

    @staticmethod
    def _path() -> str:
        return program_path()

    def _open_window(self, argv: list[str], env: dict[str, str], path: str) -> Pane:
        session = self.session
        ship_env = {"QD_SHIP": str(self.ship.folder)}
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
        script.write_text(launch_script(self.name, path, argv))
        return self.tmux.new_window(session.title, self.spec.display, cwd, ["sh", str(script)], env)

    def _survives(self, pane_id: str, program: str, seconds: float = 0.5) -> None:
        """A program that exits as soon as it starts is a refusal, not an "up"; its pane stays for a look."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            time.sleep(0.05)
            pane = self.tmux.pane(pane_id)
            if pane is not None and pane.dead:
                shown = " ".join(self.tmux.capture(pane_id, history=20).split())[-240:]  # its last words scroll above the death notice
                raise ShipError(f"{self.name}: {program} exited {pane.exit_status} at once; its pane stays for a look, and "
                                f"qd up {self.name} tries again. It printed: {shown}")

    def down(self) -> str:
        with self._lifecycle():
            return self._down()

    def _down(self) -> str:
        launched = self.launched()
        pane = self._lifecycle_pane(launched)
        if launched is not None:
            self.record()  # preserve the last proven join before removing its launch
            if self.spec.pool_choices:
                self.move_to(self.pool.name)  # retain the corroborated selection before its launch disappears
        self._retire_window(launched)
        if self.launched() != launched:
            raise ShipError(f"{self.name}: launch identity changed during retirement; launch retained")
        (self.folder / "launch.json").unlink(missing_ok=True)
        if pane is None:
            return f"{self.name} was not up" + (f" · pane {launched.pane} is gone" if launched else "")
        return f"{self.name} down · pane {pane.id} closed"

    def restart(self) -> str:
        """Relaunch with resume: the conversation continues in a new process."""
        with self._lifecycle():
            from .recovery import require_clear
            require_clear(self)
            return self._down() + "\n" + self._up(resume=True)

    def fresh(self) -> str:
        """Relaunch without resume: a new conversation that begins with the brief and the open messages."""
        with self._lifecycle():
            from .recovery import require_clear
            require_clear(self)
            return self._down() + "\n" + self._up(resume=False)


@contextmanager
def temporary_seat(ship: Ship, tmux: Tmux, spec: SeatSpec, orders: list[post.Message], purpose: str) -> Iterator[Seat]:
    """Own a fresh temporary seat through transport retirement and every teardown stage.

    Callers register only orders this operation opened. Posts and receipts remain;
    a failure to retire transport leaves its home intact for later inspection.
    """
    session = shipmod.Session(spec.session, spec.session)
    treated = replace(ship, sessions={**ship.sessions, spec.session: session}, seats={**ship.seats, spec.name: spec})
    seat = Seat(treated, spec, tmux)
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
        retired = False
        session_id = None

        def retire() -> None:
            nonlocal retired
            pending.abandon(ship.runtime, seat.name, f"{purpose} ended; submission unconfirmed and never resent")
            retired = True

        def close_orders() -> None:
            with ExitStack() as closing:
                for order in orders:
                    if order.kind == "order" and order.to == seat.name and store.is_open(order):
                        closing.callback(store.new, "report", seat.name, order.sender,
                                         f"{purpose} ended; closed by the temporary operation", re=order.id)

        def remove_home() -> None:
            if retired:
                shutil.rmtree(seat.folder)

        with ExitStack() as cleanup:
            cleanup.callback(remove_home)
            cleanup.callback(close_orders)
            cleanup.callback(lambda: tmux.kill_session_id(session_id) if session_id is not None else None)
            cleanup.callback(seat.down)
            cleanup.callback(retire)
            session_id = tmux.new_session(session.title, env={"QD_SHIP": str(ship.folder)})
            status = shlex.join([str(qd_bin() / "qd"), "--ship", str(ship.folder), "status", "--line"])
            tmux.status_line(session.title, status)
            yield seat


# -- the ship ------------------------------------------------------------------------------


def read_seat(ship: Ship, spec: SeatSpec, tmux: Tmux) -> Seat | UnresolvedSeat:
    try:
        return Seat(ship, spec, tmux)
    except SelectionError as error:
        return UnresolvedSeat(ship, spec, tmux, error)


def seats(ship: Ship, tmux: Tmux, strict: bool = True) -> list[Seat | UnresolvedSeat]:
    """Lifecycle callers refuse unknown selection; readings retain its named failure."""
    construct = Seat if strict else read_seat
    return [construct(ship, spec, tmux) for spec in ship.seats.values()]


def bring_up(ship: Ship, tmux: Tmux) -> list[str]:
    """Every seat marked up, resuming what has a record; the rest are left as declared."""
    proofs = [shipmod.fence(ship)]
    if not ship.is_repository:
        proofs.append(f"{ship.folder.name} is not its own repository: its declaration travels with the folder above")
    for seat in seats(ship, tmux):
        if seat.spec.up and not seat.is_up:
            proofs.append(seat.up(resume=True))
        elif seat.is_up:
            proofs.append(f"{seat.name} already up in {seat.pane().id}")
    return proofs


def bring_down(ship: Ship, tmux: Tmux) -> list[str]:
    proofs = [seat.down() for seat in seats(ship, tmux)]
    for session in ship.sessions.values():
        if tmux.has_session(session.title) and not tmux.panes(session.title):
            tmux.kill_session(session.title)
    return proofs
