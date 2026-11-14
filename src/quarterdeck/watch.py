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

from . import accounting_health, acts, check_outcome, credential, idle, ledger, move_hold, server_recovery, pending as pending_delivery, post, program_use, providers, quota, record_owner, recovery, request_queue, servicectl as service
from .door import Door, Reading
from .harness.capabilities import selection_label
from .harness import reading_scope
from .harness.base import record_fingerprint
from .seat import Seat, UnresolvedSeat, leftover_checks, seats
from .ship import Ship, ShipError
from .servicectl import STALE_SECONDS
from .tmux import Tmux
from .usage_cache import MISSING

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
        return Activity("off", "exit status not yet known" if pane.exit_status is None else f"exited {pane.exit_status}")
    join = seat.join()
    if join.record is None:
        if join.reason and join.reason.split(":")[0] in (
                "record missing", "ambiguous", "record reader unknown", "record reader changed",
                "record unreadable", "record pointer unreadable", "record pointer empty"):
            return Activity("unknown", join.reason)  # what the Watch cannot see, it says, never a default
        return Activity("idle", join.reason or "")  # a seat that has never moved is idle, not stalled
    try:
        state = seat.harness.state(join.record)
    except OSError as error:
        return Activity("unknown", f"record unreadable: {error}")
    queue_detail = f"queue unknown: {state.queue_error}" if state.queue_error else ""
    if state.failure is not None and state.turn_open is False:  # law: record-first -- an error is never plain idle
        from .harness.types import AUTHENTICATION
        if state.failure.kind == AUTHENTICATION:
            from .auth_failure import detail
            return Activity(AUTHENTICATION, detail(seat, state.failure))
        from .turn_failure import REFUSAL, describe
        queued = f"{len(state.queued)} queued" if state.queued else ""
        return Activity("refused" if state.failure.kind == REFUSAL else "failed",
                        " · ".join(filter(None, (describe(state.failure), queue_detail or queued))))
    if state.turn_open is None:
        return Activity("unknown", " · ".join(filter(None, ("turn state unknown in record", queue_detail))))
    if seat.harness.waiting_for_human(state):
        return Activity('needs a human', 'native question tool; ask through qd post question' +
                        (f" · {queue_detail}" if queue_detail else ""))
    if not state.turn_open:
        return Activity("idle", queue_detail or (f"{len(state.queued)} queued" if state.queued else ""))
    where = f"in {state.open_tool} {_ago(state.open_tool_since, now)}".rstrip() if state.open_tool else ""
    quiet = (now - state.last_write).total_seconds() if state.last_write else 0
    if quiet >= STALL_SECONDS:
        return Activity("stalled", " · ".join(filter(None, (where or f"{int(quiet // 60)}m without a step", queue_detail))))
    return Activity("working", " · ".join(filter(None, (where, queue_detail))))


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
    compaction: str | None = None
    provider_id: str | None = None
    provider_name: str | None = None
    program: str | None = None
    effort: str | None = None
    declared_effort: str | None = None
    model_mode: str | None = None
    effort_mode: str | None = None
    wake: str | None = None  # all wake states in detailed readings; the line filters actionable ones
    person_quiet: dict | None = None  # human sessions only, from the kept fact alone

    @property
    def model_text(self) -> str:
        if self.model_mode == 'program-default':
            declared = selection_label(self.declared_model, self.model_mode)
            return f"{self.model} (ship file: {declared})" if self.model else declared
        if self.model is None:
            return f"- (ship file: {self.declared_model})" if self.declared_model else "-"
        if self.declared_model is None:
            return self.model
        from .harness import get
        try:
            adapter = get(self.harness)
            actual = adapter.model_identity(self.model, version=self.version)
            expected = adapter.model_identity(self.declared_model, version=self.version)
            if actual is not None and actual == expected:
                return self.model
        except (ShipError, ValueError, TypeError):
            pass
        return f"{self.model} (ship file: {self.declared_model})"

    @property
    def effort_text(self) -> str:
        declared = selection_label(self.declared_effort, self.effort_mode)
        if self.effort is None:
            if self.effort_mode == 'program-default':
                return declared
            return f"- (ship file: {declared})" if declared else '-'
        if declared is None or self.effort == declared:
            return self.effort
        return f"{self.effort} (ship file: {declared})"

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
                           order, tokens_today(seat), 'unknown', None, 'unknown',
                           wake='human wake unknown: seat selection unresolved' if seat.ship.session_of(seat.spec).human else None)
    door = Door(seat).reading() if act.state not in ("off",) else Reading(False, act.detail or "off", "qd up")
    observed = running_settings(seat) if act.state != "off" else {}
    running = observed.get("model")
    launched = seat.launched()
    version = launched.version if launched else None
    pool = launched.pool if launched and launched.pool in seat.ship.pools else seat.pool.name
    from .compaction import status as compaction_status
    from . import human_wake
    from .person_quiet import summary as person_quiet
    wake = human_wake.status(seat) if seat.session.human else None
    return SeatReading(seat.name, act, door, order, tokens_today(seat), pool, running,
                       seat.harness.name, seat.spec.model, version, compaction_status(seat),
                       seat.ship.pool(pool).provider, providers.display(seat.ship.pool(pool).provider), seat.harness.program,
                       effort=observed.get('effort'), declared_effort=seat.spec.effort,
                       model_mode=seat.spec.model_mode, effort_mode=seat.spec.effort_mode, wake=wake,
                       person_quiet=person_quiet(seat) if seat.session.human else None)


def tokens_today(seat: Seat | UnresolvedSeat) -> ledger.Count:
    """Today's tokens: the Ledger's count, which outlives a fresh start; the records' while the Ledger has nothing.

    A ``+`` on the figure says some of today's turns could not be measured: a record that would not
    read, a turn whose usage the record does not hold, a turn count the record leaves uncertain.
    """
    day = date.today().isoformat()
    book = ledger.Ledger.reading(seat.ship.runtime / "ledger.sqlite")
    if book is not None:
        with book:
            if book.catching_up():
                return ledger.Count(None, 1)  # no all-record fallback while history catches up
            try:
                counted = book.today(seat.name, day)
            except sqlite3.OperationalError:  # a book written by an older service: read the records instead
                counted = ledger.Count()
            else:
                if (not counted.known and not counted.unmeasured and not isinstance(seat, UnresolvedSeat)
                        and accounted_history(book, seat)):
                    return counted
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


def accounted_history(book, seat):
    """A zero is measured too, but only matching successful certificates prove it."""
    from .harness import reader_stamp
    try:
        if not book.db.execute('SELECT 1 FROM record_accounting LIMIT 1').fetchone():
            return False
        owned = record_owner.records(seat, publish=False)
        if not owned:
            return False
        try:
            named = (seat.home.parent / 'record').read_text().strip()
            current = Path(named) if named else None
        except FileNotFoundError:
            current = None
        prepared = book.provenance.prepare(seat, current, seat.launched())
        reader_keys = reader_stamp.readers()
        for item in owned:
            if item.reader is None or item.transient:
                return False
            stamp = item.name, record_fingerprint(item.path)
            key = reader_keys.get(item.name)
            if key is None:
                return False
            evidence = book.accounting_evidence(seat.name, stamp, item.reason, prepared[1], key)
            if not book.accounted(item.path, evidence):
                return False
        return True
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        return False  # missing/changed/malformed evidence keeps the ordinary reading path


def running_settings(seat: Seat, *, screen=None) -> dict[str, str]:
    """The model and effort the program reports, from its record first and its screen second (law: record-first)."""
    found: dict[str, str] = {}
    record = seat.record()
    try:
        state = seat.harness.state(record) if record else None
    except OSError:
        state = None
    screen = screen if screen is not None else Door(seat).screen()
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


@post.reading_pass()
@reading_scope.reading_pass()
@record_owner.reader_pass()
def status_reading(ship: Ship, tmux: Tmux) -> dict:
    store = post.Store(ship.runtime)
    requests = request_queue.pending(ship.runtime)
    selected = seats(ship, tmux, False)
    programs, program_error = program_use.collect(ship, selected)
    outcomes = check_outcome.collect(ship.runtime)
    data = {"service": service_state(ship.runtime), "seats": [reading(seat, store) for seat in selected],
            "programs": programs, "programs_error": program_error,
            "check_outcomes": outcomes[0], "check_outcome_errors": outcomes[1],
            "open_orders": [m for m in store.all() if m.kind == "order" and store.is_open(m)],
            "requests": requests, "facts": facts(ship, tmux, check_reading=outcomes),
            "usage": headline_readings(ship), **providers.identities(ship)}
    try:
        book = ledger.Ledger.reading(ship.runtime / 'ledger.sqlite')
        if book is not None:
            with book:
                if book.catching_up():
                    data['accounting_note'] = 'accounting catching up; historical figures are last known'
    except (OSError, sqlite3.Error):
        pass
    return data


def render_table(data: dict) -> list[str]:
    state = data["service"]
    head = f"service {state}" + ("; qd up restarts it" if state == "down" else "")
    recovery_lines = list(getattr(data.get("facts"), "server_recovery", None) or [])  # law: nothing-waits-silently
    recovery_lines += list(getattr(data.get("facts"), "leftover_checks", None) or [])  # a check's seat left running
    rows = [head, *recovery_lines, f"{'seat':<10} {'activity':<24} {'door':<40} {'order':<10} {'today':>8}  harness · provider · pool · model · effort"]
    if accounting := getattr(data.get('facts'), 'accounting', None):
        rows.insert(1, accounting)
    if data.get('accounting_note'):
        rows.insert(1, data['accounting_note'])
    for r in data["seats"]:
        rows.append(f"{r.name:<10} {str(r.activity):<24} {str(r.door):<40} {r.order_text:<10} {str(r.today):>8}  {r.harness_text} · {r.provider_name or 'unknown'} · {r.pool} · {r.model_text} · {r.effort_text}")
        if r.compaction:
            rows.append(f"  {r.compaction}")
        if r.wake:
            rows.append(f"  {r.wake}")
    # Each program's last use and its check outcome: detail, before the open orders.
    for program in data.get('programs', []):
        working = program['working_now']
        activity = 'working' if working is True else 'inactive' if working is False else 'unknown'
        rows.append(f"program {program['harness']}: {activity} · last turn ended {program['last_turn_ended'] or 'unknown'}")
    if data.get('programs_error'):
        rows.append(data['programs_error'])
    rows += check_outcome.alarms(data.get('check_outcomes', []))
    rows += data.get('check_outcome_errors', [])
    for outcome in data.get('check_outcomes', []):
        latest = outcome['latest_attempt']
        if latest['outcome'] == 'unknown':
            rows.append(f"{outcome['harness']}: {check_outcome.REASONS[latest['reason']][1]}")
    rows.append(f"open orders: {len(data['open_orders'])}")
    for m in data["open_orders"]:
        rows.append(f"  {m.short} {m.sender} → {m.to} · {m.at.strftime('%m-%d %H:%M')}" + (f" · {m.subject}" if m.subject else ""))
    rows += ['  ' + request_queue.describe(row) for row in data.get('requests', ())]
    return rows + render_headlines(data["usage"])


def table(ship: Ship, tmux: Tmux) -> list[str]:
    return render_table(status_reading(ship, tmux))


def tool_reading(seat, state) -> tuple[str | None, str | None]:
    """The tool reason as the idle proof reads it: a seat's own inbox watch, proved, is not a pending tool."""
    if state is None:
        return None, None
    executed = idle.execution(seat, state)
    proved = executed is not state and len(executed.active_tools) < len(state.active_tools)
    return idle.tool_reason(executed), ("own inbox watch, proved" if proved else None)


def seat_reading(seat: Seat | UnresolvedSeat) -> dict:
    r = reading(seat)
    if isinstance(seat, UnresolvedSeat):
        return dict(seat=r, record=None, state=None, record_error=str(seat.error),
                    requests=request_queue.pending(seat.ship.runtime, seat.name),
                    receipt_skips=dict(total=0, counts=[], examples=[], omitted=0),
                    receipt_error=str(seat.error), record_absent=str(seat.error),
                    record_limits=None, screen=None, answered=[], act=None, usage=headline_readings(seat.ship),
                    **providers.identities(seat.ship))
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
    tools, own_watch = tool_reading(seat, state)
    return {"seat": r, "record": record, "state": state, "record_error": error, "tools": tools, "own_watch": own_watch,
            "requests": request_queue.pending(seat.ship.runtime, seat.name),
            "receipt_skips": skips, "receipt_error": receipt_error,
            "record_absent": seat.join().reason if record is None else None,
            "record_limits": getattr(seat.harness, "record_limits", None),
            "screen": Door(seat).screen(), "answered": list((launched.answered or []) if (launched := seat.launched()) else []),
            "act": acts.last(seat),
            "launch_notice": launched.notice if launched else None,
            "usage": headline_readings(seat.ship), **providers.identities(seat.ship)}


def render_full(data: dict) -> list[str]:
    r = data["seat"]
    today = f"{r.today} tokens today" + (" (+: some turns were not measured)" if r.today.unmeasured else "")
    lines = [f"{r.name}: {r.activity} · door {r.door} · order {r.order_text} · {today}",
             f"  {r.harness_text} · provider {r.provider_name or 'unknown'} · pool {r.pool} · model {r.model_text} · effort {r.effort_text}"]
    if r.compaction:
        lines.append(f"  {r.compaction}")
    lines += ['  ' + request_queue.describe(row) for row in data.get('requests', ())]
    if r.wake:
        lines.append(f"  {r.wake}")
    if r.person_quiet:
        lines.append(f"  {r.person_quiet['text']}")
    record, state = data["record"], data["state"]
    if record:
        if data["record_error"] is not None:
            lines.append(f"  record {record} · unreadable: {data['record_error']}")
        else:
            turns_shown = state.turns if state.turns is not None else "unknown"
            lines.append(f"  record {record} · {turns_shown} turns · last input: {(state.last_input or '')[:60]!r}")
            if state.queue_error:
                lines.append(f"  queue unknown: {state.queue_error}; queued is candidate history")
            if detail := data.get("tools", idle.tool_reason(state)):
                lines.append(f"  {detail}")
            if data.get("own_watch"):
                lines.append(f"  {data['own_watch']}")
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
    if data.get("launch_notice"):
        lines.append(f"  launch notice: {data['launch_notice']}")
    for done in data.get("answered") or []:
        lines.append(f"  answered for the account holder: {done.get('question')} · {done.get('choice')} pressed at "
                     f"{done.get('at')} · {done.get('outcome')}")
    if data.get("act"):
        lines.append(f"  {acts.describe(data['act'])}")
    screen = data["screen"]
    if screen:
        lines.append(f"  screen: box {screen.box!r} · working {'unknown, not seen' if screen.working is None else screen.working} · queued {screen.queued} · shows {screen.shows}")
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
    unsettled: int = 0  # typed into a turn that ended in a recorded error: unacted or uncertain
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
    queue_unknown: list[str] = field(default_factory=list)
    compactions: list[str] = field(default_factory=list)
    held: list[str] = field(default_factory=list)
    turn_errors: list[str] = field(default_factory=list)
    group_held: list[str] = field(default_factory=list)
    cascade_held: list[str] = field(default_factory=list)
    structure_waiting: list[str] = field(default_factory=list)
    manual_fresh: list[str] = field(default_factory=list)  # no systems recipient: retain names on overflow
    move_holds: list[str] = field(default_factory=list)
    ending: list[str] = field(default_factory=list)  # a seat down because its program is still ending: names kept
    leftover_checks: list[str] = field(default_factory=list)  # a check's seat that outlived its check, until it is gone
    sign_in_failures: list[str] = field(default_factory=list)  # retain seat and fixed failure on overflow
    request_waits: list[str] = field(default_factory=list)
    cutover: str = ""  # the ship still runs on tmux's default server, until one qd down and qd up
    server_recovery: list[str] = field(default_factory=list)  # a gone ship server: stopped or unknown
    accounting: str | None = None
    check_failed: list[str] = field(default_factory=list)
    check_unknown: list[str] = field(default_factory=list)
    model_mismatches: list[str] = field(default_factory=list)
    pane_obstructions: list[str] = field(default_factory=list)  # preserve named geometry diagnostics on overflow

    def line(self) -> str:  # law: nothing-waits-silently
        parts = ["SERVICE DOWN" if self.service == "down" else f"service {self.service}"]
        if self.accounting:
            parts.append(self.accounting)
        parts.append(f"{self.waiting} waiting" + (f", oldest {self.oldest}m" if self.waiting else ""))
        if self.queued:
            parts.append(f"{self.queued} queued behind a turn, oldest {self.oldest_queued}m")
        if self.blind:
            parts.append(f"{self.blind} blind")
        if self.unsettled:
            parts.append(f"{self.unsettled} unsettled after an error")  # law: nothing-waits-silently
        parts += self.unknown
        parts += self.queue_unknown
        parts += [detail for detail in self.compactions
                  if not any(fallback.startswith(detail + ';') for fallback in self.manual_fresh)]
        parts += self.manual_fresh
        parts += self.model_mismatches
        parts += self.turn_errors
        parts += self.sign_in_failures
        parts += self.pane_obstructions
        parts += self.group_held
        parts += self.cascade_held
        parts += self.structure_waiting
        parts += self.check_failed
        parts += self.check_unknown
        parts += [f"{name} needs a human" for name in self.needs_human]
        parts += [f"{name}: no watch armed" for name in self.unarmed]
        parts += [f"{name} held at limit" for name in self.held]
        parts += self.move_holds
        parts += self.ending
        parts += self.leftover_checks
        parts += self.request_waits
        parts += self.server_recovery
        parts += self.routes
        parts += self.pending
        parts += [f"disk: {name} {pct}%" for name, pct in self.disks]
        parts += [f"{pool} · login needed by {when}" for pool, when in self.login]
        parts += [self.cutover] if self.cutover else []
        from .headlines import status_line
        return status_line(self, parts)


SCHEMA_BEHIND = "ledger written by an older service; qd up restarts it"


def accounting_reading(runtime, state: str) -> str | None:
    """The accounting alarm; unknown evidence is said only while the service runs, since its state says the rest."""
    reading = accounting_health.reading(runtime)
    return None if reading == accounting_health.UNKNOWN and state != "ok" else reading


def held_scope(reason: str, pool: str) -> str:
    """The scope of a held seat's limit, short enough for the line: the group, or what is known of it."""
    head = reason.split(" · ")[0]
    head = head[len(pool) + 1:] if head.startswith(pool + " ") else head
    return head[head.rindex("(") + 1:-1] if head.endswith(")") and "(" in head else head


def headline_readings(ship: Ship) -> list[dict]:
    """Every supported subscription headline, from the cache only."""
    from . import harness as harnesses
    from .ledger_reading import window_reading

    readers = {p.name: harnesses.native_for(p.provider) for p in ship.pools.values() if p.kind == "subscription"}
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
                for key in missing:
                    fallback = reported_without(book.usage.rows(key[0]), key, selected[key[0]].headline_fallback)
                    if fallback is not None:
                        found[key] = {**window_reading(fallback, post.now()), "window": fallback.window,
                                      "note": f"provider reports no {key[2]} window"}
            except sqlite3.OperationalError:
                return [{"pool": None, "reading": None, "reason": SCHEMA_BEHIND}]
    return [found.get(key, item) for key, item in missing.items()] + [
        item for key, item in found.items() if key not in missing]


def reported_without(rows, key: tuple, fallback: tuple | None):
    """The fallback window's reading when the latest good read carried no headline window, else None.

    The provider reports no such window when a good read gave the fallback (its row has no
    error) and either never gave the headline window or its row says only that the latest
    read left it out. A failed read marks the fallback's row too, so it stays unknown.
    """
    if fallback is None:
        return None
    by = {(row.group, row.window): row for row in rows}
    own, other = by.get(key[1:]), by.get(fallback)
    if other is None or other.error is not None or not other.complete:
        return None
    return other if own is None or own.error == MISSING else None


def render_headlines(rows: list[dict]) -> list[str]:
    lines = []
    for row in rows:
        if row["reading"]:
            lines.append(row["reading"].describe(row["observed_at"]) + (f" · {row['note']}" if row.get("note") else ""))
        elif row["pool"]:
            group = f"{row['group']} / " if row.get("group") else ""
            lines.append(f"{row['pool']} {group}{row.get('window', '')} {row['reason']}")
        else:
            lines.append(row["reason"])
    return lines


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


def limit_holds(ship: Ship, selected: list[Seat | UnresolvedSeat]) -> tuple[list[str], list[str]]:
    """Keep wall outcomes and uncertainty separate from operational move-hold reasons."""
    held, unknown, _ = limit_state(ship, selected)
    return held, unknown


def limit_state(ship: Ship, selected: list[Seat | UnresolvedSeat]):
    """Up seats at a limit with no eligible destination in the recorded evidence."""
    up = [seat for seat in selected if not isinstance(seat, UnresolvedSeat) and seat.is_up]
    if not up:
        return [], [], []
    try:
        book = ledger.Ledger.reading(ship.runtime / "ledger.sqlite")
        if book is None:
            return [], [], []
        with book:
            held, walled = [], []
            for seat in up:
                if not quota.current_walled(seat, book):
                    continue
                walled.append(seat)
                if not any(quota.cached_open(ship, book, pool, seat) for pool in quota.candidate_pools(seat)):
                    label = seat.name
                    if seat.harness.model_scoped_quota and (reasons := quota.current_walls(seat, book)):
                        label += " (" + "; ".join(dict.fromkeys(held_scope(r, seat.pool.name) for r in reasons)) + ")"
                    held.append(label)
            reasons = [f'{seat.name}: move held: {reason}' for seat in walled
                       if len(seat.spec.pools) > 1 and (reason := move_hold.reading(seat))]
        return held, [], reasons  # law: nothing-waits-silently -- tool holds with a usable destination
    except (OSError, sqlite3.Error):
        return [], ["held seats unknown; ledger unreadable"], []


@post.reading_pass()
@reading_scope.reading_pass()
def facts(ship: Ship, tmux: Tmux, walls: list[str] | None = None, login: list[tuple[str, str]] | None = None,
          *, store=None, attempts=None, request_rows: list[dict] | None = None, check_reading=None) -> Facts:
    """Everything the status line says; cheap enough for tmux to ask every few seconds."""
    store = post.Store(ship.runtime) if store is None else store
    attempts = post.Attempts(ship.runtime) if attempts is None else attempts
    records = attempts.all()
    latest = attempts.latest(attempts=records)
    waiting, needs_human, unarmed, pending_lines, routes, unknown, queue_unknown, compactions, turn_errors = [], [], [], [], [], [], [], [], []
    sign_in_failures, ending = [], []
    pane_obstructions = []
    selected = seats(ship, tmux, False)
    held, limit_unknown, move_reasons = limit_state(ship, selected)
    pending_lines += limit_unknown
    for seat in selected:
        pending = attempts.undelivered(store, seat.name, results=latest)
        waiting += pending
        if isinstance(seat, UnresolvedSeat):
            needs_human.append(seat.name)
            unknown.append(str(seat.error).partition(';')[0])
            continue
        if (refused := getattr(seat, "relaunch_refused", None)) and (words := refused()):
            ending.append(words)  # law: nothing-waits-silently
        launched = seat.launched()
        from .turn_failure import obstruction as failure_obstruction
        from .auth_failure import status as authentication_status, waiting as authentication_waiting
        authentication = authentication_status(seat)
        if reason := authentication:
            sign_in_failures.append(f"{seat.name}: {reason}")  # law: nothing-waits-silently
            if not authentication_waiting(reason):
                needs_human.append(seat.name)
        elif reason := failure_obstruction(seat, store):
            turn_errors.append(f"{seat.name}: {reason}")  # law: nothing-waits-silently
            needs_human.append(seat.name)
        from .compaction import status as compaction_status
        if detail := compaction_status(seat, alerts_only=True):
            compactions.append(f"{seat.name}: {detail}")
        if launched is not None and launched.pool in seat.spec.pools[1:]:
            routes.append(f"{seat.name} on {seat.harness.name}/{launched.pool}")
        if seat.session.human:
            if reason := recovery.held(seat):
                if seat.name not in needs_human:
                    needs_human.append(seat.name)
                pending_lines.append(f"{seat.name}: {reason}")
            from . import human_wake
            watch = post.watch_reading(ship.runtime, seat.name)
            reason = human_wake.status(seat)
            if human_wake.action_required(seat, reason):
                if seat.name not in needs_human:
                    needs_human.append(seat.name)
                pending_lines.append(f"{seat.name}: {reason}")
            if pending and watch.armed is False:
                unarmed.append(seat.name)
        else:
            # Human alarms use owned metadata only; never read their conversation here.
            if seat.harness.reads_queue_error and (record := seat.record()) is not None:
                try:
                    queue_error = seat.harness.state(record).queue_error
                except OSError:
                    queue_error = None  # ordinary unreadable-record reporting remains independent
                if queue_error:
                    queue_unknown.append(f"{seat.name}: queue unknown: {queue_error}")
            door, obstruction = Door(seat).human_obstruction()
            if diagnostic := getattr(door, "diagnostic", ""):
                pane_obstructions.append(f"{seat.name}: {diagnostic}")
            if (obstruction and not authentication_waiting(authentication)
                    and seat.name not in needs_human):
                needs_human.append(seat.name)
            if door.waiting or "pending" in door.reason or "; qd submit " in door.reason:
                pending_lines.append(f"{seat.name}: {door.reason}")
            for reason in pending_delivery.detached(seat):
                if seat.name not in needs_human:
                    needs_human.append(seat.name)
                pending_lines.append(f"{seat.name}: {reason}")
    pending_lines += [f"request claimed: {path.name}; inspect requests/ before retrying"
                      for path in sorted((ship.runtime / "requests").glob("*.claimed"))]
    now = post.now()
    from . import cascade_runtime, compaction_alert, group_post, model_mismatch, structure_outcome
    oldest = int(max(((now - m.at).total_seconds() for m in waiting), default=0) // 60)
    queued = [a for a in records if a.result == "queued" and latest.get(a.message) == 'queued']
    outcomes, outcome_errors = check_reading if check_reading is not None else check_outcome.collect(ship.runtime)
    return Facts(
        service=(state := service_state(ship.runtime)),
        waiting=len(waiting),
        oldest=oldest,
        blind=sum(result == 'blind' for result in latest.values()),
        unsettled=sum(result in post.UNSETTLED for result in latest.values()),
        queued=len(queued),
        oldest_queued=int(max(((now - a.when).total_seconds() for a in queued), default=0) // 60),
        needs_human=needs_human,
        pending=pending_lines,
        routes=routes,
        unknown=unknown,
        queue_unknown=queue_unknown,
        compactions=compactions,
        manual_fresh=compaction_alert.unrouted(ship, [seat for seat in selected if not isinstance(seat, UnresolvedSeat)]),
        server_recovery=server_recovery.status(ship.runtime),
        model_mismatches=model_mismatch.unrouted(ship, [seat for seat in selected if not isinstance(seat, UnresolvedSeat)],
                                               pending_model=request_queue.pending_model),
        held=held,
        move_holds=move_reasons,
        ending=ending,
        leftover_checks=leftover_checks(ship),
        request_waits=[request_queue.headline(row) for row in
                       (request_queue.pending(ship.runtime) if request_rows is None else request_rows)],
        cutover=(f"tmux: still on the default server {tmux.socket}; qd down, then qd up, moves it"
                 if getattr(tmux, "legacy", False) else ""),
        turn_errors=turn_errors,
        sign_in_failures=sign_in_failures,
        pane_obstructions=pane_obstructions,
        group_held=group_post.holds(ship.runtime),
        cascade_held=cascade_runtime.holds(ship.runtime),
        structure_waiting=structure_outcome.waits(ship, selected),
        accounting=accounting_reading(ship.runtime, state),
        check_failed=check_outcome.alarms(outcomes),
        check_unknown=outcome_errors,
        unarmed=unarmed,
        walls=walls if walls is not None else ledger_walls(ship),
        usage=headline_readings(ship),
        disks=disks([ship.runtime, ship.workspace, Path(tempfile.gettempdir())]),
        login=login if login is not None else credential.login_needed(ship),
    )


def line(ship: Ship, tmux: Tmux) -> str:
    return facts(ship, tmux).line()
