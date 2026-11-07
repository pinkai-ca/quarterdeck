"""The Seat: one running harness in one tmux pane under one role, and its lifecycle.

Up, down, restart and fresh are one relaunch path with one difference, whether the program
resumes its record. A seat's identity is its pane id, its process id and its record; the
persona and the window name are display (law: identity).
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from . import harness as harnesses, post, ship as shipmod, standing
from .harness.base import Harness, Join
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


def launch_script(seat: str, path: str, argv: list[str]) -> str:
    """The seat's command as a script for tmux to run: tmux refuses a window command over 16 KiB, and a
    brief or a first message can be longer. The PATH travels inside it, since the login shell resets the
    one tmux passes."""
    return (f"#!/bin/sh\n# {seat}: the command qd up ran, kept in a file because tmux's command line is short\n"
            f"exec env {shlex.quote(f'PATH={path}')} {shlex.join(argv)}\n")


class Seat:
    def __init__(self, ship: Ship, spec: SeatSpec, tmux: Tmux):
        self.ship, self.spec, self.tmux = ship, spec, tmux
        self.folder = ship.runtime / "seats" / spec.name
        self.home = self.folder / "home"
        self.harness: Harness = harnesses.get(spec.harness)

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
        name = current.read_text().strip() if current.is_file() else ""
        return self.ship.pool(name if name in self.spec.pools else self.spec.pools[0])

    def move_to(self, pool: str) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / "pool").write_text(pool + "\n")

    def launched(self) -> Launched | None:
        path = self.folder / "launch.json"
        if not path.is_file():
            return None
        return Launched(**json.loads(path.read_text()))

    def pane(self) -> Pane | None:
        """The live pane of this seat's launch, by pane id and process id, or None."""
        launched = self.launched()
        if launched is None:
            return None
        pane = self.tmux.pane(launched.pane)
        if pane is None or pane.pid != launched.pid:
            return None  # law: identity
        return pane

    @property
    def is_up(self) -> bool:
        pane = self.pane()
        return pane is not None and not pane.dead

    def join(self) -> Join:
        """The seat's record: pinned once found, the first written after its launch."""
        pinned = self.folder / "record"
        if pinned.is_file():
            record = Path(pinned.read_text().strip())
            return Join(record) if record.exists() else Join(None, f"record missing: {record.name}")
        launched = self.launched()
        if launched is None:
            return Join(None, "not up")
        if self.harness.home_variable is None and self._shared_state_contended():
            return Join(None, f"ambiguous: another {self.harness.name} seat is up without a record of its own")  # law: one-record
        found = self.harness.join(self.home, launched.when)
        if found.record is not None:
            pinned.write_text(str(found.record) + "\n")  # law: one-record
        return found

    def _shared_state_contended(self) -> bool:
        """A program with no home of its own writes every seat's record to one place: two unjoined seats cannot be told apart."""
        for other in self.ship.seats.values():
            if other.name != self.name and other.harness == self.spec.harness:
                sibling = Seat(self.ship, other, self.tmux)
                if sibling.is_up and not (sibling.folder / "record").is_file():
                    return True
        return False

    def is_working(self) -> bool:
        """Whether a turn is open: from the record first, from the screen where the record is opaque."""
        pane = self.pane()
        if pane is None or pane.dead:
            return False
        if not self.harness.record_confirms:
            return self.harness.screen(self.tmux.capture(pane.id)).working
        record = self.record()
        return bool(record and self.harness.state(record).turn_open)

    def record(self) -> Path | None:
        return self.join().record

    # -- the one relaunch path -----------------------------------------------------------

    def up(self, resume: bool = True) -> str:
        if self.is_up:
            raise ShipError(f"{self.name} is up in {self.pane().id}; qd restart {self.name} relaunches it")
        shipmod.fence(self.ship)
        self.folder.mkdir(parents=True, exist_ok=True)
        left = self.pane()
        if left is not None and left.dead:
            self.tmux.kill_window(left.id)  # the last launch's pane, kept for a look, goes when the seat comes back
        if self.launched() is not None:
            from . import credential  # the credential module reads seats, so it is imported here, not above

            credential.reseed(self.ship, [self])  # a refresh left in this home reseeds the pool it came from, before any move
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
        at = datetime.now(timezone.utc).isoformat()  # before the program can write a record (law: one-record)
        pane = self._open_window(list(launch.argv), launch.env, path)
        launched = Launched(pane.id, pane.pid, at, pool.name, resume_id, list(launch.argv), self.harness.version())
        (self.folder / "launch.json").write_text(json.dumps(asdict(launched), indent=2) + "\n")
        self._survives(pane.id, launch.argv[0])
        attempts = post.Attempts(self.ship.runtime)
        for message in carried:
            attempt = attempts.open(message, self.name, pane.id, pane.pid, launched.at)
            attempts.close(attempt, "launch", "carried as the first message of the launch")
        how = f"resumed {resume_id[:8]}" if resume_id else "fresh"
        carried_note = f" · {len(carried)} open message{'s' if len(carried) != 1 else ''} carried" if carried else ""
        return f"{self.name} up in {pane.id} · {self.harness.name} {launched.version or ''} · pool {pool.name} · {how}{carried_note}"

    def _resume_id(self) -> str | None:
        record = self.record()
        return self.harness.session_id(record) if record else None

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
        """Messages whose last attempt says queued and that no record of this seat, taken or typed, ever names."""
        queued = {m for m, result in attempts.last_results(self.name).items() if result == "queued"}
        if not queued or not self.harness.record_confirms:
            return []
        seen: list[str] = []
        for record in self.harness.records(self.home):
            state, turns = self.harness.read(record)
            seen += [" ".join(t.split()) for t in [*(x.input for x in turns), *state.consumed, state.last_input or ""]]
        flat = " ".join(seen)
        return [m for m in store.for_seat(self.name) if m.id in queued and m.short not in flat]

    @staticmethod
    def _path() -> str:
        """The PATH a seat's program runs with: the folder holding ``qd`` first, so the seat can post."""
        return os.pathsep.join(filter(None, [str(qd_bin()), os.environ.get("PATH", "")]))

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
        launched = self.launched()
        pane = self.pane()
        (self.folder / "launch.json").unlink(missing_ok=True)
        if pane is None:
            return f"{self.name} was not up" + (f" · pane {launched.pane} is gone" if launched else "")
        self.tmux.kill_window(pane.id)
        return f"{self.name} down · pane {pane.id} closed"

    def restart(self) -> str:
        """Relaunch with resume: the conversation continues in a new process."""
        return self.down() + "\n" + self.up(resume=True)

    def fresh(self) -> str:
        """Relaunch without resume: a new conversation that begins with the brief and the open messages."""
        return self.down() + "\n" + self.up(resume=False)


# -- the ship ------------------------------------------------------------------------------


def seats(ship: Ship, tmux: Tmux) -> list[Seat]:
    return [Seat(ship, spec, tmux) for spec in ship.seats.values()]


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
