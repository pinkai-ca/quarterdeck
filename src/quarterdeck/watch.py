"""The Watch: reading a seat, and the one line every ship session shows.

Two readings, taken together: activity from the record answers "what is it doing", and the
door from the screen answers "can the typing path deliver" (law: record-first). A reading
the Watch could not take is unknown, never dressed as a state. The status line is
refreshed by tmux itself, so a dead service never looks like a healthy idle ship
(law: nothing-waits-silently).
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from . import post, service
from .door import Door, Reading
from .seat import Seat, seats
from .ship import Ship
from .tmux import Tmux

STALL_SECONDS = 600
DISK_WARN = 0.90


@dataclass(frozen=True)
class Activity:
    state: str
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.state}, {self.detail}" if self.detail else self.state


def _ago(moment: datetime | None, now: datetime) -> str:
    if moment is None:
        return ""
    seconds = max((now - moment).total_seconds(), 0)
    return f"{int(seconds // 60)}m" if seconds >= 60 else f"{int(seconds)}s"


def activity(seat: Seat, now: datetime | None = None) -> Activity:
    """idle, working (with the open tool), stalled (no step in ten minutes), off, or unknown."""
    now = now or datetime.now(timezone.utc)
    pane = seat.pane()
    if pane is None:
        return Activity("off", "no pane" if seat.launched() else "down")
    if pane.dead:
        return Activity("off", f"exited {pane.exit_status}")
    join = seat.join()
    if join.record is None:
        return Activity("idle", join.reason or "")  # a seat that has never moved is idle, not stalled
    try:
        state = seat.harness.state(join.record)
    except OSError as error:
        return Activity("unknown", f"record unreadable: {error}")
    if not state.turn_open:
        return Activity("idle", f"{len(state.queued)} queued" if state.queued else "")
    where = f"in {state.open_tool} {_ago(state.open_tool_since, now)}".rstrip() if state.open_tool else ""
    quiet = (now - state.last_write).total_seconds() if state.last_write else 0
    if quiet >= STALL_SECONDS:
        return Activity("stalled", where or f"{int(quiet // 60)}m without a step")
    return Activity("working", where)


@dataclass(frozen=True)
class SeatReading:
    name: str
    activity: Activity
    door: Reading
    order: str | None
    today: int
    pool: str
    model: str
    harness: str

    @property
    def needs_human(self) -> bool:
        return not self.door.open and self.door.clears == "needs a human"


def reading(seat: Seat, store: post.Store | None = None) -> SeatReading:
    store = store or post.Store(seat.ship.runtime)
    act = activity(seat)
    door = Door(seat).reading() if act.state not in ("off",) else Reading(False, act.detail or "off", "qd up")
    record = seat.record()
    turns = seat.harness.turns(record) if record else []
    today = sum(t.tokens.total for t in turns if t.ended.astimezone().date() == date.today())
    state = seat.harness.state(record) if record else None
    running = (state.model if state and state.model else None) or seat.spec.model or "-"
    model = running if running == (seat.spec.model or running) else f"{running} (ship file: {seat.spec.model})"
    open_orders = [m for m in store.open_for(seat.name) if m.kind == "order"]
    claimed = store.claimed(seat.name, post.now())
    order = claimed or (open_orders[0].id if len(open_orders) == 1 else None)
    short = post.short_of(order) if order else None
    version = seat.launched().version if seat.launched() else None
    return SeatReading(seat.name, act, door, short, today, seat.pool.name, model,
                       f"{seat.harness.name} {version}".strip() if version else seat.harness.name)


def table(ship: Ship, tmux: Tmux) -> list[str]:
    """``qd status``: every seat on one screen, then the open orders."""
    store = post.Store(ship.runtime)
    beat = service.read_heartbeat(ship.runtime)
    head = "service " + ("ok" if service.alive(ship.runtime) else
                         "down" if beat is None else f"down, last tick {int(beat[1])}s ago; qd up restarts it")
    rows = [head, f"{'seat':<10} {'activity':<24} {'door':<40} {'order':<10} {'today':>8}  pool · model"]
    for seat in seats(ship, tmux):
        r = reading(seat, store)
        rows.append(f"{r.name:<10} {str(r.activity):<24} {str(r.door):<40} {r.order or '-':<10} {r.today:>8}  {r.pool} · {r.model}")
    open_orders = [m for m in store.all() if m.kind == "order" and store.is_open(m)]
    rows.append(f"open orders: {len(open_orders)}")
    for m in open_orders:
        rows.append(f"  {m.short} {m.sender} → {m.to} · {m.at.strftime('%m-%d %H:%M')}" + (f" · {m.subject}" if m.subject else ""))
    return rows


def full(seat: Seat) -> list[str]:
    """``qd watch <seat>``: the whole reading, including the screen as the adapter parsed it."""
    r = reading(seat)
    lines = [f"{r.name}: {r.activity} · door {r.door} · order {r.order or '-'} · {r.today} tokens today",
             f"  {r.harness} · pool {r.pool} · model {r.model}"]
    record = seat.record()
    if record:
        state = seat.harness.state(record)
        lines.append(f"  record {record} · {state.turns} turns · last input: {(state.last_input or '')[:60]!r}")
    screen = Door(seat).screen()
    if screen:
        lines.append(f"  screen: box {screen.box!r} · working {screen.working} · queued {screen.queued} · shows {screen.shows}")
        if screen.interruption or screen.unknown:
            lines.append(f"  notice: {screen.interruption.name if screen.interruption else 'unknown: ' + screen.unknown}")
    return lines


def save_screen(seat: Seat) -> Path:
    """Record the screen for the adapter's next contract fixture."""
    folder = seat.ship.runtime / "screens"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{seat.name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
    pane = seat.pane()
    path.write_text(seat.tmux.capture(pane.id) if pane else "(no pane)\n")
    return path


# -- the status line -----------------------------------------------------------------------


@dataclass(frozen=True)
class Facts:
    service: bool
    waiting: int
    oldest: int
    blind: int
    needs_human: list[str] = field(default_factory=list)
    unarmed: list[str] = field(default_factory=list)
    walls: list[str] = field(default_factory=list)
    disks: list[tuple[str, int]] = field(default_factory=list)
    login: list[str] = field(default_factory=list)

    def line(self) -> str:
        parts = ["service ok" if self.service else "SERVICE DOWN"]
        parts.append(f"{self.waiting} waiting" + (f", oldest {self.oldest}m" if self.waiting else ""))
        if self.blind:
            parts.append(f"{self.blind} blind")
        parts += [f"{name} needs a human" for name in self.needs_human]
        parts += [f"{name}: no watch armed" for name in self.unarmed]
        parts += [f"wall: {wall}" for wall in self.walls]
        parts += [f"disk: {name} {pct}%" for name, pct in self.disks]
        parts += [f"{pool} · login needed by {when}" for pool, when in self.login]
        return " · ".join(parts)


def disks(paths: list[Path]) -> list[tuple[str, int]]:
    """Filesystems the ship writes to that are nearly full, by mount and percent used."""
    seen, found = set(), []
    for path in paths:
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            continue
        used = usage.used / usage.total if usage.total else 0
        mount = _mount(path)
        if mount not in seen and used >= DISK_WARN:
            found.append((mount, int(used * 100)))
        seen.add(mount)
    return found


def _mount(path: Path) -> str:
    path = path.resolve()
    while not path.is_mount() and path != path.parent:
        path = path.parent
    return str(path) if str(path) != "/" else "root"


def facts(ship: Ship, tmux: Tmux, walls: list[str] | None = None, login: list[tuple[str, str]] | None = None) -> Facts:
    """Everything the status line says; cheap enough for tmux to ask every few seconds."""
    store, attempts = post.Store(ship.runtime), post.Attempts(ship.runtime)
    waiting, needs_human, unarmed = [], [], []
    for seat in seats(ship, tmux):
        pending = attempts.undelivered(store, seat.name)
        waiting += pending
        if seat.session.human:
            if pending and not post.watch_armed(ship.runtime, seat.name):
                unarmed.append(seat.name)
        elif seat.is_up:
            door = Door(seat).reading()
            if not door.open and door.clears == "needs a human":
                needs_human.append(seat.name)
    now = post.now()
    oldest = int(max(((now - m.at).total_seconds() for m in waiting), default=0) // 60)
    return Facts(
        service=service.alive(ship.runtime),
        waiting=len(waiting),
        oldest=oldest,
        blind=len(attempts.blind_ids()),
        needs_human=needs_human,
        unarmed=unarmed,
        walls=walls or [],
        disks=disks([ship.runtime, ship.workspace, Path(tempfile.gettempdir())]),
        login=login or [],
    )


def line(ship: Ship, tmux: Tmux) -> str:
    return facts(ship, tmux).line()
