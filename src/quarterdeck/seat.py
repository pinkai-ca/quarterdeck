"""The Seat: one running harness in one tmux pane under one role, and its lifecycle.

Up, down, restart and fresh are one relaunch path with one difference, whether the program
resumes its record. A seat's identity is its pane id, its process id and its record; the
persona and the window name are display (law: identity).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import asdict, dataclass
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
            return Join(Path(pinned.read_text().strip()))
        launched = self.launched()
        if launched is None:
            return Join(None, "not up")
        found = self.harness.join(self.home, launched.when)
        if found.record is not None:
            pinned.write_text(str(found.record) + "\n")  # law: one-record
        return found

    def record(self) -> Path | None:
        return self.join().record

    # -- the one relaunch path -----------------------------------------------------------

    def up(self, resume: bool = True) -> str:
        if self.is_up:
            raise ShipError(f"{self.name} is up in {self.pane().id}; qd restart {self.name} relaunches it")
        shipmod.fence(self.ship)
        self.folder.mkdir(parents=True, exist_ok=True)
        brief = standing.render(self.ship, self.spec)
        pool = self.pool
        ship_home = self.ship.folder / "homes" / self.harness.name
        self.harness.build_home(self.home, self.spec, pool, ship_home if ship_home.is_dir() else None)
        resume_id = self._resume_id() if resume else None
        if not resume_id:
            (self.folder / "record").unlink(missing_ok=True)
        first = self.first_message()
        launch = self.harness.launch(self.spec, pool, self.home, brief, resume_id, first)
        pane = self._open_window(list(launch.argv), launch.env)
        launched = Launched(pane.id, pane.pid, datetime.now(timezone.utc).isoformat(), pool.name,
                            resume_id, list(launch.argv), self.harness.version())
        (self.folder / "launch.json").write_text(json.dumps(asdict(launched), indent=2) + "\n")
        how = f"resumed {resume_id[:8]}" if resume_id else "fresh"
        return f"{self.name} up in {pane.id} · {self.harness.name} {launched.version or ''} · pool {pool.name} · {how}"

    def _resume_id(self) -> str | None:
        record = self.record()
        return self.harness.session_id(record) if record else None

    def first_message(self) -> str | None:
        """The seat's open messages, in full, under a header saying it was briefed (law: context-is-cache)."""
        store = post.Store(self.ship.runtime)
        open_messages = store.open_for(self.name)
        if not open_messages:
            return None
        return post.briefed_header(self.name, self.spec.role, open_messages) + "\n" + post.typed(open_messages)

    def _open_window(self, argv: list[str], env: dict[str, str]) -> Pane:
        session = self.session
        ship_env = {"QD_SHIP": str(self.ship.folder)}
        if not self.tmux.has_session(session.title):
            self.tmux.new_session(session.title, env=ship_env)
            self.tmux.status_line(session.title, f"{qd_bin() / 'qd'} status --line --ship {self.ship.folder}")
        path = os.pathsep.join(filter(None, [str(qd_bin()), os.environ.get("PATH", "")]))
        env = {**env, **ship_env, "QD_SEAT": self.name}
        cwd = self.ship.cwd_of(self.spec)
        cwd.mkdir(parents=True, exist_ok=True)
        return self.tmux.new_window(session.title, self.spec.display, cwd, ["env", f"PATH={path}", *argv], env)

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
