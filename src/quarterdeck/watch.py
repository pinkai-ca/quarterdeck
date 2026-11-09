"""The Watch: reading a seat, and the one line every ship session shows.

Two readings, taken together: activity from the record answers "what is it doing", and the
door from the screen answers "can the typing path deliver" (law: record-first). A reading
the Watch could not take is unknown, never dressed as a state. The status line is
refreshed by tmux itself, so a dead service never looks like a healthy idle ship
(law: nothing-waits-silently).
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from . import credential, ledger, pending as pending_delivery, post, record_owner, servicectl as service
from .door import Door, Reading
from .harness.base import same_name
from .seat import Seat, UnresolvedSeat, seats
from .ship import Ship
from .servicectl import STALE_SECONDS
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


def activity(seat: Seat | UnresolvedSeat, now: datetime | None = None) -> Activity:
    """idle, working, question-only human wait, stalled, off, or an explicitly unknown reading."""
    if isinstance(seat, UnresolvedSeat):
        return Activity('unknown', str(seat.error))
    now = now or datetime.now(timezone.utc)
    pane = seat.pane()
    if pane is None:
        return Activity("off", "no pane" if seat.launched() else "down")
    if pane.dead:
        return Activity("off", f"exited {pane.exit_status}")
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
    if seat.harness.waiting_for_human(state):
        return Activity('needs a human', 'native question tool; ask through qd post question')
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
    today: ledger.Count
    pool: str
    model: str | None
    harness: str
    declared_model: str | None = None
    version: str | None = None

    @property
    def model_text(self) -> str:
        if self.model is None:
            return f"- (ship file: {self.declared_model})" if self.declared_model else "-"
        if self.declared_model is None or same_name(self.model, self.declared_model):
            return self.model
        return f"{self.model} (ship file: {self.declared_model})"

    @property
    def harness_text(self) -> str:
        return f"{self.harness} {self.version}".strip() if self.version else self.harness

    @property
    def order_text(self) -> str:
        return post.short_of(self.order) if self.order else "-"

    @property
    def needs_human(self) -> bool:
        return self.activity.state == 'needs a human' or (not self.door.open and self.door.clears == "needs a human")


def reading(seat: Seat | UnresolvedSeat, store: post.Store | None = None) -> SeatReading:
    """One seat's row: activity, door, order, tokens today, pool and the model it runs.

    What the seat runs is read from its record first and its screen second; the ship file's
    model is shown beside it only where they disagree, and shown as declared, not as
    observed, where nothing observed it (law: record-first).
    """
    store = store or post.Store(seat.ship.runtime)
    act = activity(seat)
    open_orders = [m for m in store.open_for(seat.name) if m.kind == "order"]
    claimed = store.claimed(seat.name, post.now())
    order = claimed or (open_orders[0].id if len(open_orders) == 1 else None)
    if isinstance(seat, UnresolvedSeat):
        return SeatReading(seat.name, act, Reading(False, str(seat.error), 'needs a human'),
                           order, tokens_today(seat), 'unknown', None, 'unknown')
    door = Door(seat).reading() if act.state not in ("off",) else Reading(False, act.detail or "off", "qd up")
    observed = running_settings(seat) if act.state != "off" else {}
    running = observed.get("model")
    launched = seat.launched()
    version = launched.version if launched else None
    pool = launched.pool if launched and launched.pool in seat.ship.pools else seat.pool.name
    return SeatReading(seat.name, act, door, order, tokens_today(seat), pool, running,
                       seat.harness.name, seat.spec.model, version)


def tokens_today(seat: Seat | UnresolvedSeat) -> ledger.Count:
    """Today's tokens: the Ledger's count, which outlives a fresh start; the records' while the Ledger has nothing.

    A ``+`` on the figure says some of today's turns could not be measured: a record that would not
    read, a turn whose usage the record does not hold, a turn count the record leaves uncertain.
    """
    day = date.today().isoformat()
    book = ledger.Ledger.reading(seat.ship.runtime / "ledger.sqlite")
    if book is not None:
        with book:
            try:
                counted = book.today(seat.name, day)
            except sqlite3.OperationalError:  # a book written by an older service: read the records instead
                counted = ledger.Count()
        if counted.known or counted.unmeasured:
            return counted
    if isinstance(seat, UnresolvedSeat):
        return ledger.Count(0, 1)  # retained totals above are usable; guessing a record reader is not
    found = ledger.Count()
    for item in record_owner.records(seat):
        try:
            state, turns = item.read()
        except OSError:
            found += ledger.Count(0, 1)  # a record that would not read: whatever it holds is not in the figure
            continue
        for turn in turns:
            when = turn.ended or turn.started
            if when is not None and when.astimezone().date().isoformat() == day:
                found += ledger.Count(turn.tokens.total, turn.tokens.unmeasured)
        if state.turns is None or state.turn_open is None:
            found += ledger.Count(0, 1)  # the record leaves its own turn count uncertain
    return found


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
    beat = service.read_heartbeat(runtime)
    if beat is None or beat[1] < 0:
        return 'busy (heartbeat unknown)'
    late = beat[1] if beat[1] > STALE_SECONDS else None
    return f"busy {int(late)}s" if late else "ok"


def status_reading(ship: Ship, tmux: Tmux) -> dict:
    store = post.Store(ship.runtime)
    return {"service": service_state(ship.runtime), "seats": [reading(seat, store) for seat in seats(ship, tmux, False)],
            "open_orders": [m for m in store.all() if m.kind == "order" and store.is_open(m)],
            "facts": facts(ship, tmux), "usage": headline_readings(ship)}


def render_table(data: dict) -> list[str]:
    state = data["service"]
    head = f"service {state}" + ("; qd up restarts it" if state == "down" else "")
    rows = [head, f"{'seat':<10} {'activity':<24} {'door':<40} {'order':<10} {'today':>8}  pool · model"]
    for r in data["seats"]:
        rows.append(f"{r.name:<10} {str(r.activity):<24} {str(r.door):<40} {r.order_text:<10} {str(r.today):>8}  {r.pool} · {r.model_text}")
    rows.append(f"open orders: {len(data['open_orders'])}")
    for m in data["open_orders"]:
        rows.append(f"  {m.short} {m.sender} → {m.to} · {m.at.strftime('%m-%d %H:%M')}" + (f" · {m.subject}" if m.subject else ""))
    return rows + render_headlines(data["usage"])


def table(ship: Ship, tmux: Tmux) -> list[str]:
    return render_table(status_reading(ship, tmux))


def seat_reading(seat: Seat | UnresolvedSeat) -> dict:
    r = reading(seat)
    if isinstance(seat, UnresolvedSeat):
        return dict(seat=r, record=None, state=None, record_error=str(seat.error),
                    receipt_skips=dict(total=0, counts=[], examples=[], omitted=0),
                    receipt_error=str(seat.error), record_absent=str(seat.error),
                    record_limits=None, screen=None, usage=headline_readings(seat.ship))
    record = seat.record()
    state, error = None, None
    skips, receipt_error = dict(total=0, counts=[], examples=[], omitted=0), None
    if record:
        try:
            state = seat.harness.state(record)
        except OSError as failure:
            error = str(failure)
        try:
            evidence = seat.harness.inputs(record)
            if hasattr(evidence, 'diagnostics'):
                skips = evidence.diagnostics()
        except OSError as failure:
            receipt_error = str(failure)
    return {"seat": r, "record": record, "state": state, "record_error": error,
            "receipt_skips": skips, "receipt_error": receipt_error,
            "record_absent": seat.join().reason if record is None else None,
            "record_limits": getattr(seat.harness, "record_limits", None),
            "screen": Door(seat).screen(), "usage": headline_readings(seat.ship)}


def render_full(data: dict) -> list[str]:
    r = data["seat"]
    today = f"{r.today} tokens today" + (" (+: some turns were not measured)" if r.today.unmeasured else "")
    lines = [f"{r.name}: {r.activity} · door {r.door} · order {r.order_text} · {today}",
             f"  {r.harness_text} · pool {r.pool} · model {r.model_text}"]
    record, state = data["record"], data["state"]
    if record:
        if data["record_error"] is not None:
            lines.append(f"  record {record} · unreadable: {data['record_error']}")
        else:
            turns_shown = state.turns if state.turns is not None else "unknown"
            lines.append(f"  record {record} · {turns_shown} turns · last input: {(state.last_input or '')[:60]!r}")
    elif data["record_absent"]:
        lines.append(f"  record absent: {data['record_absent']}")
    if data["record_limits"]:
        lines.append(f"  record limits: {data['record_limits']}")
    if data.get('receipt_error'):
        lines.append(f"  receipt evidence unreadable: {data['receipt_error']}")
    skips = data.get('receipt_skips')
    if skips and skips['total']:
        groups = '; '.join(f"{item['count']} {item['kind']}: {item['reason']}" for item in skips['counts'])
        examples = ','.join(str(item['line']) for item in skips['examples'])
        lines.append(f"  receipt skipped {skips['total']} rows · {groups} · example lines {examples} · {skips['omitted']} omitted")
    screen = data["screen"]
    if screen:
        lines.append(f"  screen: box {screen.box!r} · working {screen.working} · queued {screen.queued} · shows {screen.shows}")
        if screen.interruption or screen.unknown:
            lines.append(f"  notice: {screen.interruption.name if screen.interruption else 'unknown: ' + screen.unknown}")
    return lines + render_headlines(data["usage"])


def full(seat: Seat) -> list[str]:
    return render_full(seat_reading(seat))


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
    usage: list[dict] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    routes: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)

    def line(self) -> str:  # law: nothing-waits-silently
        parts = ["SERVICE DOWN" if self.service == "down" else f"service {self.service}"]
        parts.append(f"{self.waiting} waiting" + (f", oldest {self.oldest}m" if self.waiting else ""))
        if self.queued:
            parts.append(f"{self.queued} queued behind a turn, oldest {self.oldest_queued}m")
        if self.blind:
            parts.append(f"{self.blind} blind")
        parts += self.unknown
        parts += [f"{name} needs a human" for name in self.needs_human]
        parts += [f"{name}: no watch armed" for name in self.unarmed]
        parts += [f"wall: {wall}" for wall in self.walls]
        parts += self.routes
        parts += self.pending
        parts += [f"disk: {name} {pct}%" for name, pct in self.disks]
        parts += [f"{pool} · login needed by {when}" for pool, when in self.login]
        from .headlines import status_line
        return status_line(self, parts)


SCHEMA_BEHIND = "ledger written by an older service; qd up restarts it"


def headline_readings(ship: Ship) -> list[dict]:
    """Every supported subscription headline, from the cache only."""
    from . import harness as harnesses
    from .ledger_reading import window_reading

    readers = {p.name: harnesses.get(p.provider) for p in ship.pools.values() if p.kind == "subscription"}
    selected = {name: reader for name, reader in readers.items() if reader.reads_usage and reader.headline_windows()}
    if not selected:
        return []
    found = {}
    missing = {(pool, group, window): {"pool": pool, "group": group, "window": window,
                                      "reading": None, "reason": "usage unknown", "attempted_at": None, "error": None}
               for pool in sorted(selected) for group, window in selected[pool].headline_windows()}
    book = ledger.Ledger.reading(ship.runtime / "ledger.sqlite")
    if book is not None:
        with book:
            try:
                for row in book.usage.rows():
                    if row.pool in selected and selected[row.pool].is_headline(row.group, row.window):
                        found[(row.pool, row.group, row.window)] = window_reading(row, post.now())
                for pool, at, error in book.db.execute("SELECT pool,at,error FROM usage_reads"):
                    for key, item in missing.items():
                        if key[0] == pool:
                            item.update(attempted_at=at, error=error,
                                        reason=f"usage unknown: {error}" if error else "headline window missing from usage read")
            except sqlite3.OperationalError:
                return [{"pool": None, "reading": None, "reason": SCHEMA_BEHIND}]
    return [found.get(key, item) for key, item in missing.items()]


def render_headlines(rows: list[dict]) -> list[str]:
    return [row["reading"].describe(row["observed_at"]) if row["reading"] else
            (f"{row['pool']} {row.get('window', '')} {row['reason']}" if row["pool"] else row["reason"]) for row in rows]


def ledger_usage(ship: Ship) -> list[str]:
    return render_headlines(headline_readings(ship))


def ledger_walls(ship: Ship) -> list[str]:
    """The walls the Ledger knows of, read without opening a book the service has not written yet."""
    book = ledger.Ledger.reading(ship.runtime / "ledger.sqlite")
    if book is None:
        return []
    with book:
        try:
            return book.walls(ship)
        except sqlite3.OperationalError:
            return [SCHEMA_BEHIND]


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


def facts(ship: Ship, tmux: Tmux, walls: list[str] | None = None, login: list[tuple[str, str]] | None = None,
          *, store=None, attempts=None) -> Facts:
    """Everything the status line says; cheap enough for tmux to ask every few seconds."""
    store = post.Store(ship.runtime) if store is None else store
    attempts = post.Attempts(ship.runtime) if attempts is None else attempts
    records = attempts.all()
    latest = attempts.latest(attempts=records)
    waiting, needs_human, unarmed, pending_lines, routes, unknown = [], [], [], [], [], []
    for seat in seats(ship, tmux, False):
        pending = attempts.undelivered(store, seat.name, results=latest)
        waiting += pending
        if isinstance(seat, UnresolvedSeat):
            needs_human.append(seat.name)
            unknown.append(str(seat.error).partition(';')[0])
            continue
        launched = seat.launched()
        if launched is not None and launched.pool in seat.spec.pools[1:]:
            routes.append(f"{seat.name} on {seat.harness.name}/{launched.pool}")
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
    queued = [a for a in records if a.result == "queued" and latest.get(a.message) == 'queued']
    return Facts(
        service=service_state(ship.runtime),
        waiting=len(waiting),
        oldest=oldest,
        blind=sum(result == 'blind' for result in latest.values()),
        queued=len(queued),
        oldest_queued=int(max(((now - a.when).total_seconds() for a in queued), default=0) // 60),
        needs_human=needs_human,
        pending=pending_lines,
        routes=routes,
        unknown=unknown,
        unarmed=unarmed,
        walls=walls if walls is not None else ledger_walls(ship),
        usage=headline_readings(ship),
        disks=disks([ship.runtime, ship.workspace, Path(tempfile.gettempdir())]),
        login=login if login is not None else credential.login_needed(ship),
    )


def line(ship: Ship, tmux: Tmux) -> str:
    return facts(ship, tmux).line()
