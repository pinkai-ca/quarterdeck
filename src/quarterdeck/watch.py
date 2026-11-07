"""The Watch: reading a seat, and the one line every ship session shows.

Two readings, taken together: activity from the record answers "what is it doing", and the
door from the screen answers "can the typing path deliver" (law: record-first). A reading
the Watch could not take is unknown, never dressed as a state. The status line is
refreshed by tmux itself, so a dead service never looks like a healthy idle ship
(law: nothing-waits-silently).
"""

from __future__ import annotations

from . import pending as pending_delivery

import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from . import credential, ledger, post, record_owner, service
from .door import Door, Reading
from .harness.base import same_name
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
    if not seat.harness.record_confirms:  # an opaque record: the screen is all there is, and the reading says so
        screen = Door(seat).screen()
        return Activity("working", "by the screen") if screen and screen.working else Activity("idle", "by the screen")  # law: record-first
    join = seat.join()
    if join.record is None:
        if join.reason and join.reason.split(":")[0] in ("record missing", "ambiguous", "record reader unknown", "record reader changed", "record unreadable"):
            return Activity("unknown", join.reason)  # what the Watch cannot see, it says, never a default
        return Activity("idle", join.reason or "")  # a seat that has never moved is idle, not stalled
    try:
        state = seat.harness.state(join.record)
    except OSError as error:
        return Activity("unknown", f"record unreadable: {error}")
    if state.turn_open is None:
        return Activity("unknown", "turn state unknown in record")
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
    today: int | None
    pool: str
    model: str
    harness: str

    @property
    def needs_human(self) -> bool:
        return not self.door.open and self.door.clears == "needs a human"


def reading(seat: Seat, store: post.Store | None = None) -> SeatReading:
    """One seat's row: activity, door, order, tokens today, pool and the model it runs.

    What the seat runs is read from its record first and its screen second; the ship file's
    model is shown beside it only where they disagree, and shown as declared, not as
    observed, where nothing observed it (law: record-first).
    """
    store = store or post.Store(seat.ship.runtime)
    act = activity(seat)
    door = Door(seat).reading() if act.state not in ("off",) else Reading(False, act.detail or "off", "qd up")
    observed = running_settings(seat) if act.state != "off" else {}
    running = observed.get("model")
    if running is None:
        model = f"- (ship file: {seat.spec.model})" if seat.spec.model else "-"  # law: record-first
    elif seat.spec.model is None or same_name(running, seat.spec.model):
        model = running
    else:
        model = f"{running} (ship file: {seat.spec.model})"
    open_orders = [m for m in store.open_for(seat.name) if m.kind == "order"]
    claimed = store.claimed(seat.name, post.now())
    order = claimed or (open_orders[0].id if len(open_orders) == 1 else None)
    short = post.short_of(order) if order else None
    launched = seat.launched()
    version = launched.version if launched else None
    pool = launched.pool if launched and launched.pool in seat.ship.pools else seat.pool.name
    return SeatReading(seat.name, act, door, short, tokens_today(seat), pool, model,
                       f"{seat.harness.name} {version}".strip() if version else seat.harness.name)


def tokens_today(seat: Seat) -> int | None:
    """Today's tokens from the Ledger, which outlives a fresh start; from the record while the Ledger has nothing; None when unknown."""
    book_path = seat.ship.runtime / "ledger.sqlite"
    day = date.today().isoformat()
    counted: int | None = 0
    if book_path.exists():
        book = ledger.Ledger(book_path)
        try:
            counted = book.today(seat.name, day)
        finally:
            book.close()
        if counted is None:
            return None
    if not seat.harness.record_confirms:
        return counted or None
    join = seat.join()
    if join.record is None and join.reason and join.reason.split(":")[0] in ("record missing", "ambiguous", "record reader unknown", "record reader changed", "record unreadable"):
        return None
    if join.record is None and seat.launched() is not None:
        return None  # the new program has not established a readable record yet
    owned = record_owner.records(seat)
    if not owned:
        return counted or 0
    uncounted = 0
    for item in owned:
        try:
            state, turns = item.read()
        except OSError:
            return None
        if state.turns is None or state.turn_open is None:
            return None
        today_turns = [t for t in turns if t.ended is None or t.ended.astimezone().date().isoformat() == day]
        if any(t.ended is None or t.tokens.total is None for t in today_turns):
            return None
        uncounted += sum(t.tokens.total for t in today_turns)
    return counted if counted else uncounted


def running_settings(seat: Seat) -> dict[str, str]:
    """The model and effort the program reports, from its record first and its screen second (law: record-first)."""
    found: dict[str, str] = {}
    record = seat.record()
    try:
        state = seat.harness.state(record) if record else None
    except OSError:
        state = None
    screen = Door(seat).screen()
    for key in ("model", "effort"):
        value = (getattr(state, key, None) if state else None) or (screen.shows.get(key) if screen else None)  # law: record-first
        if value:
            found[key] = value
    return found


def service_state(runtime: Path) -> str:
    """``ok``, ``busy <age>`` while a service holds its lock but has not ticked lately, or ``down``."""
    if not service.alive(runtime):
        return "down"
    late = service.stale(runtime)
    return f"busy {int(late)}s" if late else "ok"


def table(ship: Ship, tmux: Tmux) -> list[str]:
    """``qd status``: every seat on one screen, then the open orders."""
    store = post.Store(ship.runtime)
    state = service_state(ship.runtime)
    head = f"service {state}" + ("; qd up restarts it" if state == "down" else "")
    rows = [head, f"{'seat':<10} {'activity':<24} {'door':<40} {'order':<10} {'today':>8}  pool · model"]
    for seat in seats(ship, tmux):
        r = reading(seat, store)
        today = str(r.today) if r.today is not None else "unknown"
        rows.append(f"{r.name:<10} {str(r.activity):<24} {str(r.door):<40} {r.order or '-':<10} {today:>8}  {r.pool} · {r.model}")
    open_orders = [m for m in store.all() if m.kind == "order" and store.is_open(m)]
    rows.append(f"open orders: {len(open_orders)}")
    for m in open_orders:
        rows.append(f"  {m.short} {m.sender} → {m.to} · {m.at.strftime('%m-%d %H:%M')}" + (f" · {m.subject}" if m.subject else ""))
    rows.extend(ledger_usage(ship))
    return rows


def full(seat: Seat) -> list[str]:
    """``qd watch <seat>``: the whole reading, including the screen as the adapter parsed it."""
    r = reading(seat)
    today = f"{r.today} tokens today" if r.today is not None else "unknown tokens today"
    lines = [f"{r.name}: {r.activity} · door {r.door} · order {r.order or '-'} · {today}",
             f"  {r.harness} · pool {r.pool} · model {r.model}"]
    record = seat.record()
    if record:
        try:
            state = seat.harness.state(record)
        except OSError as error:
            lines.append(f"  record {record} · unreadable: {error}")
        else:
            turns_shown = state.turns if state.turns is not None else "unknown"
            lines.append(f"  record {record} · {turns_shown} turns · last input: {(state.last_input or '')[:60]!r}")
    elif seat.join().reason:
        lines.append(f"  record absent: {seat.join().reason}")
    if note := getattr(seat.harness, "record_limits", None):
        lines.append(f"  record limits: {note}")
    screen = Door(seat).screen()
    if screen:
        lines.append(f"  screen: box {screen.box!r} · working {screen.working} · queued {screen.queued} · shows {screen.shows}")
        if screen.interruption or screen.unknown:
            lines.append(f"  notice: {screen.interruption.name if screen.interruption else 'unknown: ' + screen.unknown}")
    lines.extend(ledger_usage(seat.ship))
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
    service: str  # ok, busy <age>, or down
    waiting: int
    oldest: int
    blind: int
    needs_human: list[str] = field(default_factory=list)
    unarmed: list[str] = field(default_factory=list)
    walls: list[str] = field(default_factory=list)
    disks: list[tuple[str, int]] = field(default_factory=list)
    login: list[tuple[str, str]] = field(default_factory=list)
    queued: int = 0
    oldest_queued: int = 0
    usage: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)

    def line(self) -> str:  # law: nothing-waits-silently
        parts = ["SERVICE DOWN" if self.service == "down" else f"service {self.service}"]
        parts.append(f"{self.waiting} waiting" + (f", oldest {self.oldest}m" if self.waiting else ""))
        if self.queued:
            parts.append(f"{self.queued} queued behind a turn, oldest {self.oldest_queued}m")
        if self.blind:
            parts.append(f"{self.blind} blind")
        parts += [f"{name} needs a human" for name in self.needs_human]
        parts += [f"{name}: no watch armed" for name in self.unarmed]
        parts += [f"wall: {wall}" for wall in self.walls]
        parts += self.pending
        parts += self.usage
        parts += [f"disk: {name} {pct}%" for name, pct in self.disks]
        parts += [f"{pool} · login needed by {when}" for pool, when in self.login]
        return " · ".join(parts)


def ledger_usage(ship: Ship) -> list[str]:
    """The Google decision window already read by the service, never a provider request."""
    from .quota import reset_text

    path = ship.runtime / "ledger.sqlite"
    google = {p.name for p in ship.pools.values() if p.provider == "antigravity"}
    if not google:
        return []
    found = {}
    if path.exists():
        book = ledger.Ledger(path)
        try:
            for reading in book.usage.rows():
                if reading.group == 'Gemini 4 Argon' and reading.blocks:
                    found[reading.pool] = reading.describe(post.now())
        finally:
            book.close()
    return [found.get(pool, f"{pool} usage unknown") for pool in sorted(google)]


def ledger_walls(ship: Ship) -> list[str]:
    """The walls the Ledger knows of, read without opening a book the service has not written yet."""
    path = ship.runtime / "ledger.sqlite"
    if not path.exists():
        return []
    book = ledger.Ledger(path)
    try:
        return book.walls(ship)
    finally:
        book.close()


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
    waiting, needs_human, unarmed, pending_lines = [], [], [], []
    for seat in seats(ship, tmux):
        pending = attempts.undelivered(store, seat.name)
        waiting += pending
        if seat.session.human:
            if pending and not post.watch_armed(ship.runtime, seat.name):
                unarmed.append(seat.name)
        else:
            door = Door(seat).reading()
            if not door.open and door.clears == "needs a human":
                needs_human.append(seat.name)
            if door.waiting or "pending" in door.reason:
                pending_lines.append(f"{seat.name}: {door.reason}")
            for reason in pending_delivery.detached(seat):
                if seat.name not in needs_human:
                    needs_human.append(seat.name)
                pending_lines.append(f"{seat.name}: {reason}")
    now = post.now()
    oldest = int(max(((now - m.at).total_seconds() for m in waiting), default=0) // 60)
    queued = [a for a in attempts.all() if a.result == "queued" and a.message in attempts.queued_ids()]
    return Facts(
        service=service_state(ship.runtime),
        waiting=len(waiting),
        oldest=oldest,
        blind=len(attempts.blind_ids()),
        queued=len(queued),
        oldest_queued=int(max(((now - a.when).total_seconds() for a in queued), default=0) // 60),
        needs_human=needs_human,
        pending=pending_lines,
        unarmed=unarmed,
        walls=walls if walls is not None else ledger_walls(ship),
        usage=ledger_usage(ship),
        disks=disks([ship.runtime, ship.workspace, Path(tempfile.gettempdir())]),
        login=login if login is not None else credential.login_needed(ship),
    )


def line(ship: Ship, tmux: Tmux) -> str:
    return facts(ship, tmux).line()
