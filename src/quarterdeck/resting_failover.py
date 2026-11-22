"""Evidence and the one durable claim for an exceptional automatic pool move."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
import os
import sqlite3

from . import ledger_recovery, quota, unit_hold
from .harness import claude_sign_in, native_for
from .provenance import UNKNOWN
from .relaunch_intent import _log as _service_log, pending_resting
from .ship import ShipError
from .tmux import TmuxError
from .usage_cache import MAX_AGE, parse


FILE = 'resting-failover.sqlite'
PROBLEMS = {
    'reserved': 'the launch outcome is unknown',
    'releasing': 'the named up outcome is unknown',
    'failed': 'the launch failed',
    'login': 'the program needs a login',
    'rejected': 'the program rejected the sign-in',
    'quota-refused': 'quota refused before first success',
}
# A recorded failure is released only on a person's word, which the desk carries.
DESK_RELEASES = ('failed', 'login', 'rejected', 'quota-refused')


class Hold(Enum):
    CLEAR = 'clear'
    HELD = 'held'
    UNKNOWN = 'unknown'


class Held(ShipError):
    """A resting move or a busy attempt database keeps this launch as it is."""


@dataclass(frozen=True)
class Snapshot:
    rows: dict
    problem: str | None = None


_pass = ContextVar('resting_attempt_pass', default=None)


@contextmanager
def reading_pass():
    """Share a lazy read across all steps and lifecycle checks in this pass."""
    if _pass.get() is not None:
        yield
        return
    token = _pass.set({})
    try:
        yield
    finally:
        _pass.reset(token)


def _busy(error):
    code = getattr(error, 'sqlite_errorcode', None)
    return code is not None and (code & 0xff) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


def snapshot(runtime):
    def read():
        try:
            return Snapshot(attempts(runtime))
        except (OSError, sqlite3.DatabaseError) as error:
            return Snapshot({}, 'busy' if _busy(error) else 'unreadable')

    views = _pass.get()
    if views is None:
        return read()
    if runtime not in views:
        views[runtime] = read()
    return views[runtime]


def _remember(runtime, attempt, *, previous=None):
    views = _pass.get()
    if views is not None and (view := views.get(runtime)) is not None and view.problem is None:
        if previous is None or view.rows.get(attempt.seat) == previous:
            view.rows[attempt.seat] = attempt


def _runtime(seat):
    return getattr(getattr(seat, 'ship', None), 'runtime', None)


def current_attempt(seat):
    runtime = _runtime(seat)
    return snapshot(runtime).rows.get(seat.name) if runtime is not None else None


def hold_state(seat):
    """Use the shared attempt read and the seat's current intent and launch identity."""
    runtime = _runtime(seat)
    if runtime is None:
        return Hold.CLEAR
    if pending_resting(runtime, seat.name):
        return Hold.HELD
    view = snapshot(runtime)
    if view.problem == 'busy' and _declares_subscription(seat):
        return Hold.UNKNOWN
    attempt = view.rows.get(seat.name)
    if attempt is not None and attempt.phase not in ('ready', 'released') and attempt.matches(seat.launched()):
        return Hold.HELD
    return Hold.CLEAR


def reason(seat, state):
    if state is Hold.UNKNOWN:
        return f'{FILE} busy; resting move unknown (qd up {seat.name})'
    attempt = current_attempt(seat)
    if attempt is not None and attempt.unproved(seat):
        return f'resting move held (qd down {seat.name}, then qd up {seat.name})'
    if attempt is not None and attempt.needs_desk and attempt.matches(seat.launched()):
        return f'resting move held; needs the desk (qd up {seat.name})'
    return f'resting move held (qd up {seat.name})'


def require_clear(seat):
    with reading_pass():
        state = hold_state(seat)
        if state is not Hold.CLEAR:
            raise Held(f'{seat.name}: {reason(seat, state)}')


@dataclass(frozen=True)
class Candidate:
    pool: str
    identity: tuple[str, str]


@dataclass(frozen=True)
class Attempt:
    account: str
    generation: str
    seat: str
    pool: str
    source: str
    destination: str | None
    phase: str
    origin: str | None = None  # the phase a named up is releasing, so that a kill cannot change its result

    @property
    def standing(self):
        """The phase this row stands in, through a named up in progress."""
        return self.origin if self.phase == 'releasing' else self.phase

    @property
    def needs_desk(self):
        return self.standing in DESK_RELEASES

    def matches(self, launch):
        if self.phase == 'releasing' or launch is None or launch.at in (self.source, self.destination):
            return True
        # A crash after the pane opened may leave a destination without a receipt.
        return self.phase == 'reserved' and launch.pool == self.pool

    def unproved(self, seat):
        return self.phase == 'launched' and self.matches(seat.launched()) and seat.is_up

    def problem(self, seat):
        if self.phase in PROBLEMS and self.matches(seat.launched()):
            return self.detail(PROBLEMS[self.phase])
        if self.phase == 'launched' and self.matches(seat.launched()) and not seat.is_up:
            return self.detail('the program ended before sign-in was proved')
        return None

    def detail(self, reason):
        who = 'the desk' if self.needs_desk else 'a human'
        return f'{self.seat}: resting subscription {self.pool}: {reason}; needs {who} (qd up {self.seat})'


@contextmanager
def _database(runtime, *, write=False):
    path = runtime / FILE
    if not write:
        try:
            path.stat()
        except FileNotFoundError:
            yield None
            return
    # SQLite may have to roll back a hot journal before even a SELECT can proceed.
    db = sqlite3.connect(path.absolute().as_uri() + ('?mode=rwc' if write else '?mode=rw'),
                         uri=True, timeout=1)
    try:
        if write:
            db.execute("PRAGMA synchronous=EXTRA")
            db.execute("""CREATE TABLE IF NOT EXISTS attempts (
                       account TEXT NOT NULL, generation TEXT NOT NULL, seat TEXT NOT NULL, pool TEXT NOT NULL,
                       source TEXT NOT NULL, destination TEXT, phase TEXT NOT NULL, origin TEXT,
                       CHECK ((phase = 'releasing') = (origin IS NOT NULL)),
                       PRIMARY KEY (account,generation), UNIQUE (seat,source))""")
        with db:
            yield db
    finally:
        db.close()


def attempts(runtime):
    with _database(runtime) as db:
        rows = () if db is None else db.execute("""SELECT * FROM attempts
            WHERE rowid IN (SELECT MAX(rowid) FROM attempts GROUP BY seat)""")
        return {row[2]: Attempt(*row) for row in rows}


def attempt_for(seat):
    return attempts(seat.ship.runtime).get(seat.name)


def claim(seat, choice, source):
    attempt = Attempt(*choice.identity, seat.name, choice.pool, source, None, 'reserved')
    with _database(seat.ship.runtime, write=True) as db:
        inserted = db.execute("""INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?)
                              ON CONFLICT DO NOTHING""", tuple(vars(attempt).values())).rowcount
    if inserted == 1:
        _remember(seat.ship.runtime, attempt)
        return attempt
    return None


def spent(runtime, choice):
    with _database(runtime) as db:
        return db is not None and db.execute("SELECT 1 FROM attempts WHERE account=? AND generation=?",
                                             choice.identity).fetchone() is not None


def outcome(runtime, attempt, phase, destination):
    origin = attempt.standing if phase == 'releasing' else None
    with _database(runtime, write=True) as db:
        updated = db.execute("""UPDATE attempts SET phase=?,destination=?,origin=?
                                WHERE account=? AND generation=? AND phase=? AND destination IS ? AND origin IS ?""",
                             (phase, destination, origin, attempt.account, attempt.generation,
                              attempt.phase, attempt.destination, attempt.origin)).rowcount
    if updated == 1:
        _remember(runtime, replace(attempt, phase=phase, destination=destination, origin=origin), previous=attempt)
    return updated == 1


def _release_write(seat, attempt, phase, destination):
    try:
        updated = outcome(seat.ship.runtime, attempt, phase, destination)
    except sqlite3.DatabaseError as error:
        if not _busy(error):
            raise
        raise Held(f'{seat.name}: {reason(seat, Hold.UNKNOWN)}') from None
    if not updated:
        raise Held(f'{seat.name}: resting attempt changed; retry qd up {seat.name}')


def _confirmed_down(seat):
    try:
        pane = seat._lifecycle_pane(seat.launched())
        return (pane is None or pane.dead) and not seat.relaunch_refused()
    except (OSError, ShipError, TmuxError):
        return False


def refuse_release(seat, previous, attempt):
    """Only the desk or a person releases a recorded failure, also on a retry after a kill.

    A caller with no QD_SEAT is a person. This stops a seat following a printed step. It is not a wall against a
    process of the same user that clears its environment.
    """
    if attempt is None or not attempt.needs_desk or not attempt.matches(previous):
        return
    caller = os.environ.get('QD_SEAT')
    if caller and getattr(seat.ship.seats.get(caller), 'role', None) != 'desk':
        raise Held(f'{seat.name}: {attempt.standing} on resting subscription {attempt.pool}; '
                   f'{caller} cannot release it: tell the desk, which releases it on a person\'s word')


def _lands_on(seat, attempt, launch):
    """Another program is never the resting subscription; an alias is, and an unreadable location counts as it."""
    if claude_sign_in.on_another_program(launch):
        return False
    if launch.pool == attempt.pool:
        return True
    try:
        resting = claude_sign_in.location(seat.ship.pools[attempt.pool])
        landed = claude_sign_in.location(seat.ship.pools[launch.pool])
    except (KeyError, OSError, RuntimeError, ShipError):
        return True
    return resting is not None and resting == landed


def _after_up(seat, before, launch):
    """The row's result from the phase it stood in and the launch alone, so a retry after a kill reaches the same."""
    if before.phase in ('launched', 'reserved') and launch.at != before.source and _lands_on(seat, before, launch):
        return 'launched', launch.at  # nothing is proved: the first real turn or its rejection decides
    return 'released', launch.at  # elsewhere, the resting sign-in is not in use; its generation stays spent


@contextmanager
def releasing(seat, previous, attempt, *, live, caller):
    """Keep the seat held across named up without keeping SQLite locked."""
    if (attempt is None or attempt.phase in ('ready', 'released')
            or (attempt.phase != 'reserved' and not attempt.matches(previous))):
        yield
        return
    before = replace(attempt, phase=attempt.standing, origin=None)
    in_progress = replace(before, phase='releasing', origin=before.phase)
    if attempt.phase != 'releasing' or not live:
        # Even a retry must prove it can write before it launches. A reservation can first match the resulting launch.
        try:
            _release_write(seat, attempt, 'releasing', attempt.destination)
        except sqlite3.DatabaseError as error:
            if not ledger_recovery.malformed(error):
                raise
            yield  # corruption has its own alert and does not block ordinary recovery
            return
    try:
        yield
    except Exception:
        if _confirmed_down(seat):
            _release_write(seat, in_progress, before.phase, before.destination)
        raise
    launch = seat.launched()
    pane = seat._lifecycle_pane(launch)
    retirement = seat.relaunch_refused()
    if pane is None or pane.dead:
        if not retirement:
            _release_write(seat, in_progress, before.phase, before.destination)
        raise Held(f'{seat.name}: named up left the seat down (qd up {seat.name})')
    if retirement:
        raise Held(f'{seat.name}: named up could not be confirmed (qd up {seat.name})')
    phase, destination = _after_up(seat, before, launch)
    if phase == 'released':  # logged first: a release is never left unrecorded
        _service_log(seat.ship.runtime, 'resting-release', seat=seat.name, phase=before.phase, by=caller)
    _release_write(seat, in_progress, phase, destination)


def _observe_outcome(runtime, attempt, phase, destination):
    try:
        outcome(runtime, attempt, phase, destination)
    except sqlite3.DatabaseError as error:
        if not _busy(error):
            raise


def observe_screen(seat, attempt, screen, launch):
    if (launch is not None and launch.at == attempt.destination and seat.launched() == launch
            and getattr(getattr(screen, 'interruption', None), 'name', None) == 'login'):
        _observe_outcome(seat.ship.runtime, attempt, 'login', launch.at)
        return True
    return False


def observe_record(seat, attempt, state):
    """Resolve startup by the record's first successful use or a preceding refusal."""
    launch = seat.launched()
    if launch is None or launch.at != attempt.destination or state is None or (since := parse(launch.at)) is None:
        return False
    from .harness.types import AUTHENTICATION

    failure = state.failure
    proved_at = min((turn.ended for turn in getattr(state, 'completed_turns', ())
                     if turn.timed and turn.successful is True and turn.ended is not None and turn.ended >= since),
                    default=None)
    if any(wall.at is not None and wall.at >= since and (proved_at is None or wall.at <= proved_at)
           for wall in getattr(state, 'walls', ())):
        _observe_outcome(seat.ship.runtime, attempt, 'quota-refused', launch.at)
        return True
    if proved_at is not None:
        _observe_outcome(seat.ship.runtime, attempt, 'ready', launch.at)
        return False
    if failure is not None and failure.kind == AUTHENTICATION:
        if failure.at is None or failure.at >= since:
            _observe_outcome(seat.ship.runtime, attempt, 'rejected', launch.at)
        return True  # a refusal carried from the source cannot relaunch a provisional destination
    return False


def status(ship, seats):
    view = snapshot(ship.runtime)
    if view.problem == 'busy':
        return [f'{FILE} busy; waiting']
    if view.problem:
        return [f'{FILE} unreadable, needs a human']
    try:
        return [reason for name, attempt in sorted(view.rows.items())
                if name in seats and (attempt.phase == 'launched' or not pending_resting(ship.runtime, name))
                and (reason := attempt.problem(seats[name]))]
    except (OSError, ShipError):
        return [f'{FILE} unreadable, needs a human']


def provisional_status(ship, seats):
    view = snapshot(ship.runtime)
    lines = []
    for seat in seats:
        attempt = view.rows.get(seat.name)
        try:
            if attempt is not None and attempt.unproved(seat):
                lines.append(f'{seat.name}: on resting subscription {attempt.pool}, not yet proved by a first turn')
        except (OSError, ShipError):
            continue  # the seat's own reading reports the error
    return lines


def _eligible_crew(seat):
    return (not seat.session.human and seat.session.name != 'office' and seat.ship.kind == 'crew'
            and seat.spec.role not in ('desk', 'office'))


def _declares_subscription(seat):
    if not _eligible_crew(seat):
        return False
    spec = seat.declaration
    return any(claude_sign_in.subscription(pool, spec.for_pool(name).harness) for name in spec.pools
               if (pool := seat.ship.pools.get(name)) is not None
               and (not spec.pool_choices or name in spec.pool_choices))


def eligible_source(seat):
    return _eligible_crew(seat) and claude_sign_in.subscription(seat.pool, seat.spec.harness)


def _windows_open(rows, scope, started, bad, now):
    # Successful work through the recorded sign-in renews its generation, so check the reading's
    # age at the first matching expiry read. Another client can still spend the account's allowance.
    applicable = False
    for row in rows:
        applies = quota.window_applies(scope, row.pool, row.group)
        if applies is False:
            continue
        if (applies is not True or row.window in bad or not row.complete or row.used >= 1
                or not parse(row.read_at) <= started <= now < parse(row.resets_at)
                or (started - parse(row.read_at)).total_seconds() >= MAX_AGE):
            return False
        applicable = True
    return applicable


def _aliases(ship, source):
    return {pool.name for pool in ship.pools.values() if claude_sign_in.location(pool) == source}


def _resting(service, seat, aliases):
    for other in service.ship.seats.values():
        if other.name == seat.name:
            continue
        member = service.seat(other.name)
        launch = member.launched()
        if launch is not None and launch.pool in aliases and member.is_up:
            return False
    return True


def _no_refusals(book, ship, aliases, scope, now):
    for event in book.quota.events(now, ship=ship):
        pool = event['pool']
        if pool in aliases:
            scoped = replace(scope, pool=pool) if scope is not None else None
            if not event['scope_known'] or book.quota.applicability(event, scoped) is not False:
                return False
        elif pool == UNKNOWN:
            owner = ship.seats.get(event['seat'])
            if owner is None or not aliases.isdisjoint(owner.pools):
                return False
    return True


def candidate(service, seat, name, now):
    """All positive evidence must agree. A known spent or unresolved window defeats it."""
    pool = service.ship.pool(name)
    spec = seat.declaration.for_pool(name)
    if not claude_sign_in.subscription(pool, spec.harness):
        return None
    try:
        source = claude_sign_in.location(pool)
        if source == claude_sign_in.location(seat.pool):
            return None
        reader = native_for(pool.provider)
        sign_in = claude_sign_in.observe(reader, source)
        identity = claude_sign_in.renewable_identity(reader, source, sign_in, now) if sign_in is not None else None
        if identity is None:
            return None
        book = service.ledger
        scope = quota.model_scope(seat, name)
        rows = book.usage.rows(name)
        evidence = book.usage.expired_access(name, identity, rows, now)
        if evidence is None:
            return None
        started, bad = evidence
        if not _windows_open(rows, scope, started, bad, now):
            return None
        aliases = _aliases(service.ship, source)
        if (name not in aliases or book.catching_up() or not _no_refusals(book, service.ship, aliases, scope, now)
                or not _resting(service, seat, aliases)):
            return None
        for alias in aliases:
            if quota.held(service, alias):
                return None
            if alias == name:
                continue
            other = book.usage.rows(alias)
            latest = book.usage._latest(alias)
            prior = book.usage.expired_access(alias, identity, other, now)
            if not other and (latest is None or prior is not None):
                continue
            other_scope = replace(scope, pool=alias) if scope is not None else None
            if prior is None:
                attempted = parse(latest[0]) if latest is not None else None
                if attempted is None or attempted > now or latest[1] is not None:
                    return None
                prior = started, [
                    row.window for row in other
                    if row.error is not None or parse(row.attempted_at) != attempted
                    or (row.complete and parse(row.read_at) > attempted)
                ]
            if not _windows_open(other, other_scope, *prior, now):
                return None
        choice = Candidate(name, identity)
        return choice if not spent(service.ship.runtime, choice) else None
    except (OSError, RuntimeError, ShipError, TypeError, ValueError, sqlite3.DatabaseError):
        return None


def unit_reason(ship):
    return unit_hold.reason(ship.resting_subscription_hold_unit)
