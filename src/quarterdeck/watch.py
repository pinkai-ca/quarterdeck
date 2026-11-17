"""The Watch: reading a seat, and the one line every ship session shows.

Two readings, taken together: activity from the record answers "what is it doing", and the
door from the screen answers "can the typing path deliver" (law: record-first). A reading
the Watch could not take is unknown, never dressed as a state. The status line is
refreshed by tmux itself, so a dead service never looks like a healthy idle ship
(law: nothing-waits-silently).
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from . import trusted_files
from . import post_retirement, accounting_health, acts, check_outcome, credential, idle, ledger, move_hold, server_recovery, pending as pending_delivery, post, post_dir, program_use, providers, quota, record_owner, recovery, request_queue, servicectl as service
from .door import Door, Reading
from .harness.capabilities import selection_label
from .harness import reading_scope
from .harness.base import record_fingerprint
from .seat import SEAT_HOLDS, Seat, UnresolvedSeat, launch_pool_refusal, leftover_seats, seats
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
    selection_warning: str | None = None

    @property
    def selection_text(self) -> str | None:
        if not self.selection_warning:
            return None
        return (f'program selection fallback: declared {self.declared_model or "program default"} / '
                f'{self.declared_effort or "program default"}, running {self.model or "unknown"} / '
                f'{self.effort or "unknown"}; program: {self.selection_warning}')

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
    from . import pr_screen
    if pr_screen.held(seat.ship.runtime, seat.name):
        # A hand screening a pull request takes no work: nothing is typed to it until its hold is released, and its
        # row says so first, whatever its record reads (an unreadable hold holds the same).
        act = Activity('screening', f"a pull request · {act}")
        door = Reading(False, pr_screen.SCREENING, "the screen's release")
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
                       selection_warning=observed.get('selection_warning'),
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
            named = (seat.trusted / trusted_files.RECORD).read_text().strip()
            current = Path(named) if named else None
        except FileNotFoundError:
            current = None
        prepared = book.provenance.prepare(seat, current, seat.launched())
        reader_keys = reader_stamp.readers()
        for item in owned:
            if item.reader is None or item.transient:
                return False
            stamp = ledger.record_stamp(item)
            key = reader_keys.get(item.name)
            if key is None:
                return False
            launch = book.provenance.owning_launch(prepared[0], item.path)
            evidence = book.accounting_evidence(seat.name, stamp, item.reason, launch, key)
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
    if screen and (warning := screen.shows.get('selection_warning')):
        found['selection_warning'] = warning
    return found


def service_state(runtime: Path, *, now=None) -> str:
    """``ok``; ``busy <age> in <duty>`` while its main loop has made no new stamp for servicectl.BUSY_SECONDS, with
    ``main loop stuck``
    once that passes servicectl.LOOP_STUCK_SECONDS; or ``down`` when no process holds the lock.

    The beat, written from a thread of its own, proves only that the process is alive. The main loop's pass stamp,
    written as it begins each pass and each duty, proves it is moving: a loop stuck in one duty never reads ok."""
    if not service.alive(runtime):
        return "down"
    beat = service.read_heartbeat(runtime) if now is None else service.read_heartbeat(runtime, now=now)
    if beat is None or beat[1] < 0:
        return 'busy (heartbeat unknown)'
    if beat[1] > STALE_SECONDS:
        return f"busy {int(beat[1])}s"  # the whole process is silent, its beat thread too
    loop = service.read_pass(runtime, now=now)
    if loop is None or loop[0] != beat[0] or loop[1] < 0:
        return 'busy (main loop not yet seen)'
    pid, age, duty = loop
    if age > service.LOOP_STUCK_SECONDS:
        return f"busy {int(age)}s in {duty}: main loop stuck, nothing is delivered"
    return f"busy {int(age)}s in {duty}" if age > service.BUSY_SECONDS else "ok"


@post.reading_pass()
@reading_scope.reading_pass()
@record_owner.reader_pass()
def status_reading(ship: Ship, tmux: Tmux, *, store=None, collected=None) -> dict:
    store, post_held = _post_listing(ship, post.Store(ship.runtime) if store is None else store)
    requests = request_queue.pending(ship.runtime)
    selected = seats(ship, tmux, False)
    programs, program_error = program_use.collect(ship, selected)
    outcomes = check_outcome.collect(ship.runtime)
    data = {"service": service_state(ship.runtime), "seats": [reading(seat, store) for seat in selected],
            "programs": programs, "programs_error": program_error,
            "check_outcomes": outcomes[0], "check_outcome_errors": outcomes[1],
            "open_orders": [m for m in store.all() if m.kind == "order" and store.is_open(m)],
            "requests": requests, "facts": (collected if collected is not None else
                facts(ship, tmux, store=store, post_held=post_held, check_reading=outcomes)),
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
    recovery_lines += list(getattr(data.get("facts"), "leftover_seats", None) or [])  # a temporary seat left behind
    recovery_lines += list(getattr(data.get("facts"), "post_waits", None) or [])
    recovery_lines += list(getattr(data.get("facts"), "pool_forecasts", None) or [])
    rows = [head, *recovery_lines, f"{'seat':<10} {'activity':<24} {'door':<40} {'order':<10} {'today':>8}  harness · provider · pool · model · effort"]
    if accounting := getattr(data.get('facts'), 'accounting', None):
        rows.insert(1, accounting)
    if data.get('accounting_note'):
        rows.insert(1, data['accounting_note'])
    for r in data["seats"]:
        rows.append(f"{r.name:<10} {str(r.activity):<24} {str(r.door):<40} {r.order_text:<10} {str(r.today):>8}  {r.harness_text} · {r.provider_name or 'unknown'} · {r.pool} · {r.model_text} · {r.effort_text}")
        if warning := getattr(r, 'selection_text', None):
            rows.append(f"  {warning}")
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


def tool_reading(seat, state, screen=None) -> tuple[str | None, str | None]:
    """The tool reason as the idle proof reads it: a seat's own inbox watch, proved, is not a pending tool. Where
    the record cannot say, the line adds what the screen, read second, shows or lacks (law: record-first)."""
    if state is None:
        return None, None
    executed = idle.execution(seat, state)
    proved = executed is not state and len(executed.active_tools) < len(state.active_tools)
    reason = idle.tool_reason(executed)
    if executed.pending_tools is None:
        _, hold, saw = idle.second(seat, executed, idle.Hold('tools-unknown', reason), screen=screen)
        if hold is None:
            reason = f'{reason} in its record; its screen shows {saw}: at rest'
        elif hold.dimension == 'tools-pending':
            reason = f'{reason} in its record; its screen shows {saw}'
        else:
            reason = hold.reason
    return reason, ("own inbox watch, proved" if proved else None)


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
    screen = Door(seat).screen()
    tools, own_watch = tool_reading(seat, state, screen)
    return {"seat": r, "record": record, "state": state, "record_error": error, "tools": tools, "own_watch": own_watch,
            "requests": request_queue.pending(seat.ship.runtime, seat.name),
            "receipt_skips": skips, "receipt_error": receipt_error,
            "record_absent": seat.join().reason if record is None else None,
            "record_limits": getattr(seat.harness, "record_limits", None),
            "screen": screen, "answered": list((launched.answered or []) if (launched := seat.launched()) else []),
            "act": acts.last(seat),
            "launch_notice": launched.notice if launched else None,
            "usage": headline_readings(seat.ship), **providers.identities(seat.ship)}


def render_full(data: dict) -> list[str]:
    r = data["seat"]
    today = f"{r.today} tokens today" + (" (+: some turns were not measured)" if r.today.unmeasured else "")
    lines = [f"{r.name}: {r.activity} · door {r.door} · order {r.order_text} · {today}",
             f"  {r.harness_text} · provider {r.provider_name or 'unknown'} · pool {r.pool} · model {r.model_text} · effort {r.effort_text}"]
    if warning := getattr(r, 'selection_text', None):
        lines.append(f"  {warning}")
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
    output_limits: list[str] = field(default_factory=list)  # a seat stopped at its output limit with an order open
    group_held: list[str] = field(default_factory=list)
    drop_held: list[str] = field(default_factory=list)  # a refused post drop: the folder is named
    cascade_held: list[str] = field(default_factory=list)
    structure_waiting: list[str] = field(default_factory=list)
    manual_fresh: list[str] = field(default_factory=list)  # no systems recipient: retain names on overflow
    move_holds: list[str] = field(default_factory=list)
    ending: list[str] = field(default_factory=list)  # a seat down because its program is still ending: names kept
    leftover_seats: list[str] = field(default_factory=list)  # a temporary seat that outlived its run, until it is gone
    raises: list[str] = field(default_factory=list)  # a crew qd raise started, until the desk reports on its order
    sign_in_failures: list[str] = field(default_factory=list)  # retain seat and fixed failure on overflow
    request_waits: list[str] = field(default_factory=list)
    cutover: str = ""  # the ship still runs on tmux's default server, until one qd down and qd up
    server_recovery: list[str] = field(default_factory=list)  # a gone ship server: stopped or unknown
    seat_holds: list[str] = field(default_factory=list)  # one seat held alone by a bad file or a refused record
    screening: list[str] = field(default_factory=list)  # a hand screening a pull request: its posts are held (pr_screen)
    brief_git: list[str] = field(default_factory=list)  # the ship folder's git unanswered past its passes (brief_change)
    relaunches: list[str] = field(default_factory=list)  # a relaunch a stopped service left unfinished (relaunch_intent)
    write_bounds: list[str] = field(default_factory=list)  # a jailed seat stopped at its write bound (write_bound)
    jail_sweeps: list[str] = field(default_factory=list)  # a stray sandbox the sweep ended, within the day (jail_end)
    record_bounds: list[str] = field(default_factory=list)  # a seat's record near or past its read bound (record_file)
    retirement: str | None = None
    accounting: str | None = None
    ledger_recovery: list[str] = field(default_factory=list)
    check_failed: list[str] = field(default_factory=list)
    check_unknown: list[str] = field(default_factory=list)
    model_mismatches: list[str] = field(default_factory=list)
    pane_obstructions: list[str] = field(default_factory=list)  # preserve named geometry diagnostics on overflow
    post_held: list[str] = field(default_factory=list)  # an error that stops the store listing, or a link at imports
    post_waits: list[str] = field(default_factory=list)
    pool_forecasts: list[str] = field(default_factory=list)

    def line(self) -> str:  # law: nothing-waits-silently
        parts = ["SERVICE DOWN" if self.service == "down" else f"service {self.service}"]
        if self.retirement:
            parts.append(self.retirement)
        parts += self.ledger_recovery
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
        parts += self.seat_holds
        parts += self.screening
        parts += self.brief_git
        parts += self.queue_unknown
        parts += [detail for detail in self.compactions
                  if not any(fallback.startswith(detail + ';') for fallback in self.manual_fresh)]
        parts += self.manual_fresh
        parts += self.model_mismatches
        parts += self.turn_errors
        parts += self.output_limits
        parts += self.sign_in_failures
        parts += self.pane_obstructions
        parts += self.post_held
        parts += self.post_waits
        parts += self.pool_forecasts
        parts += self.group_held
        parts += self.drop_held
        parts += self.cascade_held
        parts += self.structure_waiting
        parts += self.check_failed
        parts += self.check_unknown
        parts += [f"{name} needs a human" for name in self.needs_human]
        parts += [f"{name}: no watch armed" for name in self.unarmed]
        parts += [f"{name} held at limit" for name in self.held]
        parts += self.move_holds
        parts += self.ending
        parts += self.leftover_seats
        parts += self.raises
        parts += self.request_waits
        parts += self.server_recovery
        parts += self.relaunches
        parts += self.write_bounds
        parts += self.jail_sweeps
        parts += self.record_bounds
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


def _launch_reads(seat: Seat) -> bool:
    try:
        seat.launched()
        return True
    except ShipError:
        return False


def _readable_up(seat: Seat) -> bool:
    """Up by its launch record; a record refused is not up here, and the seat's own line names the refusal."""
    try:
        return seat.is_up
    except ShipError:
        return False


def limit_state(ship: Ship, selected: list[Seat | UnresolvedSeat]):
    """Up seats at a limit with no eligible destination in the recorded evidence."""
    up = [seat for seat in selected if not isinstance(seat, UnresolvedSeat) and _readable_up(seat)]
    if not up:
        return [], [], []
    try:
        book = ledger.Ledger.reading(ship.runtime / "ledger.sqlite")
        if book is None:
            return [], [], []
        with book:
            held, walled = [], []
            for seat in up:
                try:
                    if not quota.current_walled(seat, book):
                        continue
                except ShipError:
                    continue  # a seat whose own record is refused: its own line names it
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


class _HeldStore:
    """No messages, once a link hold has already stopped the real listing."""

    held = True  # its readers may say so, rather than read its refusals as their own data unreadable

    def __init__(self, store):
        self.dir = store.dir

    def all(self):
        return []

    def for_seat(self, seat):
        return []

    def open_for(self, seat):
        return []

    def replies(self, message):
        return []

    def is_open(self, message):
        return False

    def claimed(self, seat, before):
        return None

    def get(self, ref):
        raise ShipError(f'no message {ref!r} in {self.dir}')

    def closing(self, message):
        return message

    def closing_report(self, order):
        return None


class HeldReadings(dict):
    """The service's native record readings, kept across its status collections (held_readings).

    Each entry is reading_scope's own: a reading, its record's fingerprint (presence, inode, size, mtime, device and
    change time of the record and its write-ahead log) and the time it was read. reading_scope.read reuses one only
    while that fingerprint is unchanged and the record was older than its timestamp tick, so a changed or replaced
    record is always read again. Bounded: after each collection only the readings that collection used remain.
    Held by the service alone, here and not in a reader's module, so no reader stamp moves."""

    def __init__(self):
        super().__init__()
        self.used = set()

    def __setitem__(self, key, value):
        self.used.add(key)
        super().__setitem__(key, value)


@contextmanager
def held_readings(memo: HeldReadings):
    """One status collection whose native readings come from, and go back to, the service's memo."""
    from .harness import reading_scope
    memo.used = set()
    token = reading_scope._READING.set(memo)
    try:
        yield memo
    finally:
        reading_scope._READING.reset(token)
        for key in [key for key in memo if key not in memo.used]:
            del memo[key]


def _imports_hold(runtime) -> str | None:
    """A link at imports/, named, or None when that directory is absent or real."""
    import os
    try:
        descriptor = post_dir.open_directory(runtime / 'post' / 'imports', create=False)
    except FileNotFoundError:
        return None
    except ShipError as error:
        text = str(error)
        return text if text.startswith(post_dir.LINK_HELD) else None
    except OSError:
        return None
    os.close(descriptor)
    return None


def _post_listing(ship, store):
    """The store listing, and every error that stopped it, plus a link at imports/."""
    holds = []
    try:
        store.all()
    except (ShipError, OSError) as error:
        holds.append(str(error))
        store = _HeldStore(store)
    extra = _imports_hold(ship.runtime)
    if extra and extra not in holds:
        holds.append(extra)
    from . import pane_obstruction
    for line in pane_obstruction.held_links(ship.runtime):
        if line not in holds:
            holds.append(line)
    return store, holds


@post.reading_pass()
@reading_scope.reading_pass()
def facts(ship: Ship, tmux: Tmux, walls: list[str] | None = None, login: list[tuple[str, str]] | None = None,
          *, store=None, attempts=None, request_rows: list[dict] | None = None, check_reading=None,
          post_held=None, keep: list | None = None) -> Facts:
    """Everything the status line says; cheap enough for tmux to ask every few seconds. ``keep``, a list, receives
    the collection a post refresh reuses (refresh_post)."""
    facts, collection = collect(ship, tmux, walls, login, store=store, attempts=attempts, request_rows=request_rows,
                                check_reading=check_reading, post_held=post_held)
    if keep is not None:
        keep.append(collection)
    return facts


# A seat's reading is a fixed sequence of steps. Each is the post's (it reads only what the post's traffic changes:
# the store, the attempts, the pending journals, the seat's watch) or the slow part's (records, screens, launches,
# sign-ins, compactions). Each step records what it appends, in order; a step that raises ends the seat's reading
# there, said for that seat alone. A full collection runs every step. A post refresh reruns only the post steps,
# and the door step of a seat whose own pending or drop folder changed, over the slow steps kept from the last full
# collection, then replays every seat's appends in step order: the facts are those a full collection would give.
POST, SLOW, DOOR = 'post', 'slow', 'door'
_HUMAN_STEPS = (('pending', POST), ('unresolved', SLOW), ('ending', SLOW), ('launch', SLOW), ('auth', SLOW),
                ('obstruction', POST), ('compaction', SLOW), ('route', SLOW), ('human-held', SLOW),
                ('human-wake', SLOW), ('unarmed', POST))
_CREW_STEPS = (('pending', POST), ('unresolved', SLOW), ('ending', SLOW), ('launch', SLOW), ('auth', SLOW),
               ('obstruction', POST), ('compaction', SLOW), ('route', SLOW), ('queue', SLOW), ('door', DOOR),
               ('detached', POST))
_SEAT_LISTS = ('waiting', 'needs_human', 'unarmed', 'pending', 'routes', 'unknown', 'queue_unknown', 'compactions',
               'turn_errors', 'output_limits', 'sign_in_failures', 'ending', 'pane_obstructions', 'seat_holds')


@dataclass
class _Step:
    ops: list  # (list, value, how): how is 'append', 'extend' or 'once' (append if absent)
    stop: bool = False  # the seat's reading ends after this step: it raised, or the seat is unresolved


@dataclass
class Collection:
    """What a full collection keeps for the post refreshes that follow it, until the next full collection."""
    ship: Ship
    tmux: Tmux
    selected: list
    readable: list
    steps: dict  # seat name -> list of _Step, as many as ran
    contexts: dict  # seat name -> values later steps read (authentication, launched, pending)
    prefix: dict  # list name -> what comes before the seats' appends
    suffix: dict  # list name -> what comes after them
    slow: dict  # every other Facts field from the slow part, as collected
    request_rows: list | None = None


def _held_line(name: str, error: Exception) -> str:
    text = str(error)[:200]
    return text if text.startswith(f"{name}:") else f"{name}: held for this seat alone: {type(error).__name__}: {text}"


def _steps_of(seat):
    return _HUMAN_STEPS if getattr(getattr(seat, 'session', None), 'human', False) else _CREW_STEPS


def _run_step(step: str, seat, ctx: dict, store, attempts, latest) -> _Step:
    ops, name = [], seat.name
    try:
        if step == 'pending':
            ctx['pending'] = pending = attempts.undelivered(store, name, results=latest)
            ops.append(('waiting', pending, 'extend'))
            if pending and getattr(getattr(seat, 'spec', None), 'role', None) == 'hand':
                from . import hand_door
                for message in pending:  # a hand's one order at a time: what its door holds is named
                    if held := hand_door.held(seat, store, attempts, message):
                        ops.append(('pending', f"{name}: {message.short} {held}", 'append'))  # law: nothing-waits-silently
        elif step == 'unresolved':
            if isinstance(seat, UnresolvedSeat):
                ops.append(('needs_human', name, 'append'))
                ops.append(('unknown', str(seat.error).partition(';')[0], 'append'))
                return _Step(ops, stop=True)
        elif step == 'ending':
            if (refused := getattr(seat, "relaunch_refused", None)) and (words := refused()):
                ops.append(('ending', words, 'append'))  # law: nothing-waits-silently
        elif step == 'launch':
            ctx['launched'] = launched = seat.launched()
            if refused_pool := launch_pool_refusal(seat, launched):
                ops.append(('seat_holds', refused_pool, 'append'))  # law: nothing-waits-silently
        elif step == 'auth':
            from .auth_failure import status as authentication_status, waiting as authentication_waiting
            ctx['authentication'] = authentication = authentication_status(seat)
            if reason := authentication:
                ops.append(('sign_in_failures', f"{name}: {reason}", 'append'))  # law: nothing-waits-silently
                if not authentication_waiting(reason):
                    ops.append(('needs_human', name, 'append'))
        elif step == 'obstruction':
            from .turn_failure import obstruction as failure_obstruction
            if not ctx.get('authentication') and (reason := failure_obstruction(seat, store)):
                ops.append(('turn_errors', f"{name}: {reason}", 'append'))  # law: nothing-waits-silently
                ops.append(('needs_human', name, 'append'))
            from .output_limit import line as output_limit_line
            if stopped := output_limit_line(seat, store):
                ops.append(('output_limits', stopped, 'append'))  # law: nothing-waits-silently
        elif step == 'compaction':
            from .compaction import status as compaction_status
            if detail := compaction_status(seat, alerts_only=True):
                ops.append(('compactions', f"{name}: {detail}", 'append'))
        elif step == 'route':
            launched = ctx.get('launched')
            if launched is not None and launched.pool in seat.spec.pools[1:]:
                ops.append(('routes', f"{name} on {seat.harness.name}/{launched.pool}", 'append'))
        elif step == 'human-held':
            if reason := recovery.held(seat):
                ops.append(('needs_human', name, 'once'))
                ops.append(('pending', f"{name}: {reason}", 'append'))
        elif step == 'human-wake':
            from . import human_wake
            reason = human_wake.status(seat)
            if human_wake.action_required(seat, reason):
                ops.append(('needs_human', name, 'once'))
                ops.append(('pending', f"{name}: {reason}", 'append'))
        elif step == 'unarmed':
            watch = post.watch_reading(seat.ship.runtime, name)
            if ctx.get('pending') and watch.armed is False:
                ops.append(('unarmed', name, 'append'))
        elif step == 'queue':
            # Human alarms use owned metadata only; never read their conversation here.
            if seat.harness.reads_queue_error and (record := seat.record()) is not None:
                try:
                    from . import record_follow
                    queue_error = record_follow.state(seat, record).queue_error
                except OSError:
                    queue_error = None  # ordinary unreadable-record reporting remains independent
                if queue_error:
                    ops.append(('queue_unknown', f"{name}: queue unknown: {queue_error}", 'append'))
        elif step == 'door':
            from .auth_failure import waiting as authentication_waiting
            door, obstruction = Door(seat).human_obstruction()
            if diagnostic := getattr(door, "diagnostic", ""):
                ops.append(('pane_obstructions', f"{name}: {diagnostic}", 'append'))
            if obstruction and not authentication_waiting(ctx.get('authentication')):
                ops.append(('needs_human', name, 'once'))
            if door.waiting or "pending" in door.reason or "; qd submit " in door.reason:
                ops.append(('pending', f"{name}: {door.reason}", 'append'))
        elif step == 'detached':
            for reason in pending_delivery.detached(seat):
                ops.append(('needs_human', name, 'once'))
                ops.append(('pending', f"{name}: {reason}", 'append'))
    except Exception as error:  # law: nothing-waits-silently
        ops.append(('seat_holds', _held_line(name, error), 'append'))
        return _Step(ops, stop=True)
    return _Step(ops)


def _replay(reading: Collection) -> dict:
    lists = {name: list(reading.prefix.get(name, [])) for name in _SEAT_LISTS}
    for seat in reading.selected:
        for step in reading.steps.get(seat.name, []):
            for target, value, how in step.ops:
                if how == 'extend':
                    lists[target] += value
                elif how == 'once':
                    if value not in lists[target]:
                        lists[target].append(value)
                else:
                    lists[target].append(value)
            if step.stop:
                break
    for name, values in reading.suffix.items():
        lists[name] += values
    return lists


def _post_fields(ship, store, attempts, post_held, records, latest, waiting, reading: Collection) -> dict:
    """Every Facts field read from the post alone, beside the seats' replayed appends."""
    from . import cascade_runtime, group_post, relaunch_intent, structure_outcome, fleet_post
    now = post.now()
    queued = [a for a in records if a.result == "queued" and latest.get(a.message) == 'queued']
    state = service_state(ship.runtime)
    return dict(
        service=state,
        accounting=accounting_reading(ship.runtime, state),  # said by the service's state, read with it
        post_waits=fleet_post.status(ship, store) + post.filing_waits(ship.runtime) + post.drop_waits(ship.runtime),
        waiting=len(waiting),
        oldest=int(max(((now - m.at).total_seconds() for m in waiting), default=0) // 60),
        blind=sum(result == 'blind' for result in latest.values()),
        unsettled=_unsettled(store, latest),
        queued=len(queued),
        oldest_queued=int(max(((now - a.when).total_seconds() for a in queued), default=0) // 60),
        relaunches=relaunch_intent.status(ship, {seat.name: seat for seat in reading.selected
                                                 if not isinstance(seat, UnresolvedSeat)}),
        post_held=list(post_held),
        group_held=group_post.holds(ship.runtime),
        drop_held=post.drop_alarms(ship.runtime),
        cascade_held=cascade_runtime.holds(ship.runtime),
        structure_waiting=structure_outcome.waits(ship, reading.readable),
    )


def _unsettled(store, latest) -> int:
    """Messages whose latest attempt left them unsettled after an error and which have not reached their addressee,
    read for every kind as the waiting count (Attempts.undelivered) reads that: not read whole by the seat, not a
    screen's spent instruction, not a settled notice. An order since reported on, or a question since answered, no
    longer counts; an acknowledgment is never delivered, so never counts. One that cannot be read stays counted."""
    from .pr_screen import spent_instructions
    count, reached = 0, {}

    def reached_by(seat):
        if seat not in reached:
            try:
                reached[seat] = (store.read_ids(seat) | spent_instructions(store.dir.parent.parent, seat, store)
                                 | frozenset(store.settled_notices(seat)))
            except (ShipError, OSError, ValueError):
                reached[seat] = frozenset()  # what cannot be read proves nothing reached it: still counted
        return reached[seat]

    for identifier, result in latest.items():
        if result not in post.UNSETTLED:
            continue
        try:
            message = store.get(identifier)
        except ShipError:
            count += 1
            continue
        if message.kind == 'ack' or message.obligation and not store.is_open(message):
            continue
        count += message.id not in reached_by(message.to)
    return count


def _post_reading(ship, store, attempts, post_held):
    store = post.Store(ship.runtime) if store is None else store
    if post_held is None:
        if isinstance(store, post.Store):
            store, post_held = _post_listing(ship, store)
        else:
            post_held = []
    attempts = post.Attempts(ship.runtime) if attempts is None else attempts
    records = attempts.all()
    return store, attempts, post_held, records, attempts.latest(attempts=records)


def _facts(reading: Collection, lists: dict, fields: dict) -> Facts:
    values = dict(reading.slow)
    values.update(fields)
    for name in _SEAT_LISTS:
        if name != 'waiting':
            values[name] = lists[name]
    return Facts(**values)


@post.reading_pass()
@reading_scope.reading_pass()
def collect(ship: Ship, tmux: Tmux, walls: list[str] | None = None, login: list[tuple[str, str]] | None = None,
            *, store=None, attempts=None, request_rows: list[dict] | None = None, check_reading=None,
            post_held=None) -> tuple[Facts, Collection]:
    """A full collection: the facts, and what a post refresh reuses until the next full collection."""
    store, attempts, post_held, records, latest = _post_reading(ship, store, attempts, post_held)
    selected = seats(ship, tmux, False)
    # Seats whose launch record reads: a refused record holds its seat alone, said by that seat's own line.
    readable = [seat for seat in selected if not isinstance(seat, UnresolvedSeat) and _launch_reads(seat)]
    held, limit_unknown, move_reasons = limit_state(ship, selected)
    reading = Collection(ship, tmux, selected, readable, {}, {}, prefix=dict(pending=list(limit_unknown),
                      seat_holds=service_holds(ship.runtime)), suffix={}, slow={}, request_rows=request_rows)
    for seat in selected:
        ctx, ran = {}, []
        for step, _ in _steps_of(seat):
            ran.append(result := _run_step(step, seat, ctx, store, attempts, latest))
            if result.stop:
                break
        reading.steps[seat.name], reading.contexts[seat.name] = ran, ctx
    reading.suffix['pending'] = [f"request claimed: {path.name}; inspect requests/ before retrying"
                                 for path in sorted((ship.runtime / "requests").glob("*.claimed"))]
    now = post.now()
    from . import brief_change, compaction_alert, jail_end, ledger_recovery, model_mismatch, pr_screen, stop_barrier, write_bound
    from . import raise_notice
    from . import human_wake
    from .harness import record_file
    outcomes, outcome_errors = check_reading if check_reading is not None else check_outcome.collect(ship.runtime)
    from . import usage_forecast
    lists = _replay(reading)
    fields = _post_fields(ship, store, attempts, post_held, records, latest, lists['waiting'], reading)
    reading.slow = dict(
        pool_forecasts=[forecast.describe() for forecast in usage_forecast.readings(ship, now) if forecast.warning],
        manual_fresh=compaction_alert.unrouted(ship, readable),
        server_recovery=server_recovery.status(ship.runtime) + stop_barrier.status(ship.runtime),
        write_bounds=write_bound.status(ship),
        jail_sweeps=jail_end.status(ship),
        record_bounds=record_file.alarms(readable, owned=human_wake.owned_record),
        screening=pr_screen.status(ship.runtime),
        brief_git=brief_change.status(ship.runtime),
        model_mismatches=model_mismatch.unrouted(ship, readable, pending_model=request_queue.pending_model),
        held=held,
        move_holds=move_reasons,
        leftover_seats=leftover_seats(ship),
        raises=raise_notice.unseen(ship.runtime, store),
        request_waits=[request_queue.headline(row) for row in
                       (request_queue.pending(ship.runtime) if request_rows is None else request_rows)],
        cutover=(f"tmux: still on the default server {tmux.socket}; qd down, then qd up, moves it"
                 if getattr(tmux, "legacy", False) else ""),
        retirement=post_retirement.reading(ship.runtime),
        ledger_recovery=ledger_recovery.alarms(ship.runtime),
        check_failed=check_outcome.alarms(outcomes),
        check_unknown=outcome_errors,
        walls=walls if walls is not None else ledger_walls(ship),
        usage=headline_readings(ship),
        disks=disks([ship.runtime, ship.workspace, Path(tempfile.gettempdir())]),
        login=login if login is not None else credential.login_needed(ship),
    )
    return _facts(reading, lists, fields), reading


@post.reading_pass()
@reading_scope.reading_pass()
def refresh_post(reading: Collection, *, store=None, attempts=None, door_seats=frozenset()) -> Facts | None:
    """The facts after post traffic alone: the post steps and fields read again, the rest as last collected.

    ``door_seats`` are seats whose own pending or drop folder changed: their door step is read again too. None
    when a seat's last reading stopped at a post step, whose later steps never ran: only a full collection can
    say what they give."""
    ship = reading.ship
    store, attempts, post_held, records, latest = _post_reading(ship, store, attempts, None)
    for seat in reading.selected:
        ran, ctx = reading.steps.get(seat.name, []), reading.contexts.get(seat.name, {})
        kinds = dict(_steps_of(seat))
        order = [step for step, _ in _steps_of(seat)]
        if ran and ran[-1].stop and kinds[order[len(ran) - 1]] != SLOW:
            return None
        for index, result in enumerate(ran):
            kind = kinds[order[index]]
            if kind == POST or kind == DOOR and seat.name in door_seats:
                ran[index] = _run_step(order[index], seat, ctx, store, attempts, latest)
                if ran[index].stop:
                    return None  # a post step now stops the seat: the steps after it need a full collection
    lists = _replay(reading)
    return _facts(reading, lists, _post_fields(ship, store, attempts, post_held, records, latest, lists['waiting'],
                                               reading))


@contextmanager
def _one_seat(name: str, lines: list[str]):
    """One seat's reading that fails is said for that seat alone; the rest of the line still reads."""
    try:
        yield
    except Exception as error:  # law: nothing-waits-silently
        text = str(error)[:200]
        lines.append(text if text.startswith(f"{name}:") else f"{name}: held for this seat alone: "
                     f"{type(error).__name__}: {text}")


def service_holds(runtime) -> list[str]:
    """The service's steps held for one seat alone (Service.seat_guard), from the file only it writes."""
    try:
        data = json.loads((runtime / SEAT_HOLDS).read_text())
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as error:
        return [f"seat holds unreadable: {type(error).__name__}"]
    if not isinstance(data, dict):
        return ["seat holds unreadable: not an object"]
    return [f"{name}: {step} held for this seat alone: {str(text)[:160]}"
            for name, steps in sorted(data.items()) if isinstance(steps, dict)
            for step, text in sorted(steps.items())]


def line(ship: Ship, tmux: Tmux) -> str:
    return facts(ship, tmux).line()
