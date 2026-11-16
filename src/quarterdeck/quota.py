"""Persisted quota events and background refresh; readings never call the provider."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import math
import time

from .harness.limits import Wall, Usage
from .provenance import UNKNOWN



def candidate_pools(seat) -> tuple[str, ...]:
    """Later declared choices, then earlier choices, excluding the current pool and any pool paid by a key.

    A launch on a key is refused (ship.key_refusal), so a key pool is no destination: a seat whose other choices
    are spent is held at its limit with no eligible destination, and the status line says so."""
    pools = seat.spec.pools
    current = pools.index(seat.pool.name)
    return tuple(name for name in pools[current + 1:] + pools[:current] if not seat.ship.pool(name).paid_by_key)


@dataclass(frozen=True)
class ModelScope:
    pool: str
    reader: object
    model: str | None
    seat: str | None = None
    sources: tuple[str, ...] = ()

    def __call__(self, group):
        return self.reader.usage_scope(group, self.model)

    def event(self, event, *, source=None, owner=None):
        verdict = self.reader.quota_scope(event, self.model)
        if verdict is True:
            return True
        # An unplaced actual refusal remains local to its own observed execution.
        if (self.reader.model_scoped_quota and self.model is None and event.model is None
                and source in self.sources and owner == self.seat):
            return True
        return verdict


def model_scope(seat, pool_name: str, *, running: bool = False, screen=None):
    """Adapter-owned group applicability; source evidence differs from a resolved destination."""
    from . import harness as harnesses

    spec = None if running else seat.spec.for_pool(pool_name)
    reader = seat.harness if running else harnesses.get(spec.harness)
    ship = getattr(seat, "ship", None)
    pool = ship.pools.get(pool_name) if ship is not None else None
    name = harnesses.native_name(pool.provider) if pool is not None and pool.kind == "subscription" else None
    if name is not None:
        native = harnesses.get(name)
        if native.model_scoped_quota:
            reader = native  # Membership belongs to the paying provider's adapter.
    if not (reader.model_scoped_usage or reader.model_scoped_quota):
        return None
    sources = ()
    if running:
        from .watch import running_settings
        model = running_settings(seat, screen=screen).get("model")
        if reader.model_scoped_quota:
            record, launched = seat.record(), seat.launched()
            sources = tuple(filter(None, (str(record) if record else None,
                            f"screen:{seat.name}:{launched.at}" if launched else None)))
    else:
        model = spec.model
    return ModelScope(pool_name, reader, model, seat.name if running else None, sources)


def window_applies(scope, pool: str, group: str | None):
    # A destination's model policy is never applied to another pool's measurements.
    return scope(group) if scope is not None and getattr(scope, "pool", pool) == pool else True


def current_walls(seat, book) -> list[str]:
    pool = seat.pool.name
    if not book.walled(pool, seat.ship) and not book.quota.blocked(seat.name, pool):
        return []  # No model reading is needed unless this pool has a wall to scope.
    scope = model_scope(seat, pool, running=True)
    found = book.walls(seat.ship, scope=scope, pool=pool)
    unknown = book.quota.blocked(seat.name, pool, scope=scope)
    if unknown and unknown not in found:
        found.append(unknown)
    return found


def current_walled(seat, book) -> bool:
    """Main's boolean path: the pool's walls and refusals, under the running model's scope."""
    pool = seat.pool.name
    if not book.walled(pool, seat.ship) and not book.quota.blocked(seat.name, pool):
        return False  # No model reading is needed unless this pool has a wall to scope.
    scope = model_scope(seat, pool, running=True)
    return book.walled(pool, seat.ship, scope=scope) or book.quota.blocked(seat.name, pool, scope=scope) is not None


def cached_open(ship, book, pool_name: str, seat=None) -> bool:
    """Routing eligibility from existing evidence, with no provider read."""
    from . import harness as harnesses

    pool = ship.pools[pool_name]
    if pool.kind == 'key' and pool.budget is not None and book.catching_up():
        return False  # configured spend cannot be inferred from an unfinished history
    if pool.kind in ("key", "endpoint"):
        return not book.walled(pool_name, ship)
    reader = harnesses.native_for(pool.provider)
    scope = (model_scope(seat, pool_name) if seat is not None else
             ModelScope(pool_name, reader, None) if reader.model_scoped_quota else None)
    if reader.background_usage or reader.reads_usage:
        return (book.usage_known(pool_name, scope=scope) and book.quota.room_known(pool_name, scope=scope)
                and not book.walled(pool_name, ship, scope=scope))
    return not book.walled(pool_name, ship, scope=scope)


def stamp(at: datetime | None) -> str | None:
    return at.astimezone(timezone.utc).isoformat() if at is not None else None


def reset_text(at: str | None, from_screen: bool = False) -> str:
    if not at:
        return "reset unknown"
    label = " (estimated from first screen observation)" if from_screen else ""
    return f"reset {datetime.fromisoformat(at).astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}{label}"


class Quotas:
    def __init__(self, db, create: bool = True, *, commit_attempt=None):
        self.db = db
        self.commit_attempt = commit_attempt
        if create:  # a reading opens the book read-only and makes no table
            db.executescript("""
                CREATE TABLE IF NOT EXISTS wall_events (
                    source TEXT NOT NULL, event TEXT NOT NULL, seat TEXT NOT NULL, pool TEXT NOT NULL,
                    at TEXT, resets_at TEXT, observed_at TEXT NOT NULL, reason TEXT NOT NULL,
                    error_id TEXT, signature TEXT NOT NULL, superseded INTEGER NOT NULL DEFAULT 0,
                    group_name TEXT, PRIMARY KEY (source, event));
                CREATE TABLE IF NOT EXISTS wall_scopes (
                    source TEXT NOT NULL, event TEXT NOT NULL, harness TEXT, model TEXT, group_name TEXT,
                    all_models INTEGER NOT NULL DEFAULT 0, evidence TEXT NOT NULL, error TEXT,
                    PRIMARY KEY (source, event));
                CREATE TABLE IF NOT EXISTS usage_reads (pool TEXT PRIMARY KEY, at TEXT NOT NULL, error TEXT);
            """)
            if "group_name" not in self.columns():  # an older book gains the refused limit's model group
                db.execute("ALTER TABLE wall_events ADD COLUMN group_name TEXT")

    def columns(self) -> set[str]:
        return {row[1] for row in self.db.execute("PRAGMA table_info(wall_events)")}

    def observe(self, source: str, seat: str, pool: str, event: Wall, now: datetime,
                *, commit: bool = True) -> None:
        old = self.db.execute("SELECT pool,at,resets_at FROM wall_events WHERE source=? AND event=?",
                              (source, event.key)).fetchone()
        if old is None:
            self.db.execute("INSERT INTO wall_events (source,event,seat,pool,at,resets_at,observed_at,reason,"
                            "error_id,signature,superseded,group_name) VALUES (?,?,?,?,?,?,?,?,?,?,0,?)",
                            (source, event.key, seat, pool, stamp(event.at), stamp(event.resets_at), stamp(now),
                             event.reason, event.error_id, event.signature, event.group))
        # Only fill missing evidence. A duplicate observation never shifts a known time/reset.
        else:
            filled = (pool if old[0] == UNKNOWN else old[0],
                      old[1] if old[1] is not None else stamp(event.at),
                      old[2] if old[2] is not None else stamp(event.resets_at))
            if old != filled:
                self.db.execute("UPDATE wall_events SET pool=?,at=?,resets_at=? WHERE source=? AND event=?",
                                (*filled, source, event.key))
        if event.harness is not None:
            self.observe_scope(source, event)
        if not source.startswith("screen:"):
            matches = self.db.execute("SELECT source,event FROM wall_events WHERE superseded=0 "
                                      "AND source LIKE 'screen:%' AND seat=? "
                                      "AND (signature=? OR (error_id IS NOT NULL AND error_id=?))",
                                      (seat, event.signature, event.error_id)).fetchall()
            if matches:
                self.db.executemany("UPDATE wall_events SET superseded=1 WHERE source=? AND event=?", matches)
        if commit and self.db.in_transaction:
            self.db.commit()

    def record(self, record, seat: str, events: tuple[Wall, ...], provenance, now: datetime,
               *, commit: bool = True) -> None:
        for event in events:
            owner, pool = provenance.resolve(record, event.at, event.at, seat, UNKNOWN)
            self.observe(str(record), owner, pool, event, now, commit=False)
        if commit and self.db.in_transaction:
            self.db.commit()

    def observe_scope(self, source: str, event: Wall) -> None:
        row = self.db.execute("SELECT pool,at,resets_at,reason,error_id,signature FROM wall_events "
                              "WHERE source=? AND event=?", (source, event.key)).fetchone()
        evidence = self.scope_evidence(row)
        old = self.db.execute("SELECT harness,model,group_name,all_models,evidence,error FROM wall_scopes "
                              "WHERE source=? AND event=?", (source, event.key)).fetchone()
        values = (event.harness, event.model, event.group, event.all_models)
        error = None if (event.reason, event.error_id, event.signature) == row[3:] else "event identity differs from retained refusal"
        if old is not None and old[4] == evidence:
            error = error or old[5]
            if any(a is not None and b is not None and a != b for a, b in zip(old[:3], values[:3])):
                error = "conflicting event scope evidence"
            values = tuple(a if a is not None else b for a, b in zip(old[:3], values[:3])) + (old[3] or values[3],)
        if old != (*values, evidence, error):
            self.db.execute("INSERT OR REPLACE INTO wall_scopes VALUES (?,?,?,?,?,?,?,?)",
                            (source, event.key, *values, evidence, error))

    @staticmethod
    def scope_evidence(row) -> str:
        return hashlib.sha256(json.dumps(row, separators=(",", ":")).encode()).hexdigest()

    def events(self, now: datetime | None = None, *, ship=None) -> list[dict]:
        from . import harness as harnesses

        now = now or datetime.now(timezone.utc)
        has_scope = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='wall_scopes'").fetchone()
        group_column = "group_name" if "group_name" in self.columns() else "NULL"
        found = []
        for source, key, seat, pool, at, reset, observed, reason, error_id, signature, refused_group in self.db.execute(
                "SELECT source,event,seat,pool,at,resets_at,observed_at,reason,error_id,signature,"
                f"{group_column} FROM wall_events WHERE superseded=0"):
            if reset is not None and datetime.fromisoformat(reset) <= now:
                continue
            scope = (self.db.execute("SELECT harness,model,group_name,all_models,evidence,error FROM wall_scopes "
                                     "WHERE source=? AND event=?", (source, key)).fetchone() if has_scope else None)
            valid = (scope is not None and not scope[5] and
                     scope[4] == self.scope_evidence((pool, at, reset, reason, error_id, signature)))
            wall = Wall(key, datetime.fromisoformat(at) if at else None,
                        datetime.fromisoformat(reset) if reset else None, reason, error_id, signature,
                        *scope[:3], all_models=bool(scope[3])) if valid else Wall(key, None, None, reason, error_id, signature)
            if wall.group is None and refused_group is not None:
                wall = replace(wall, group=refused_group)  # the refused limit's model group, as its record named it
            if wall.harness is None and ship is not None and pool in ship.pools and ship.pools[pool].kind == "subscription":
                native = harnesses.native_for(ship.pools[pool].provider)
                if native.model_scoped_quota:
                    wall = replace(wall, harness=native.name)  # Provider identity supplies no model/group scope.
            found.append(dict(seat=seat, pool=pool, reason=reason, resets_at=reset, observed_at=observed,
                              source=source, wall=wall, scope_known=bool(valid and (wall.model or wall.group or wall.all_models)),
                              scope_error=scope[5] if scope else None))
        return found

    def active(self, now: datetime | None = None, *, scope=None) -> list[tuple]:
        """Refusals not yet reset, in the public six-column layout, that ``scope`` proves apply."""
        return [tuple(item[key] for key in ("seat", "pool", "reason", "resets_at", "observed_at", "source"))
                for item in self.events(now) if self.applicability(item, scope) is True]

    @staticmethod
    def describe(item) -> str:
        from . import harness as harnesses

        event = item["wall"]
        reader = harnesses.get(event.harness) if event.harness in harnesses.NAMES else None
        scope = reader.quota_scope_label(event) if reader is not None else None
        label = item["pool"] if item["pool"] != UNKNOWN else item["seat"] + " (pool unknown)"
        return (f"{label} {event.reason}" + (f" ({scope})" if scope else "") +
                f" · {reset_text(item['resets_at'], item['source'].startswith('screen:'))}")

    @staticmethod
    def applicability(item, scope) -> bool | None:
        if scope is None or getattr(scope, "pool", item["pool"]) != item["pool"]:
            return True  # Overview, or an unrelated pool that has its own policy.
        if hasattr(scope, "event"):
            return scope.event(item["wall"], source=item["source"], owner=item["seat"])
        group = item["wall"].group  # a window-only predicate: a grouped refusal applies where it proves the group
        return True if group is None else scope(group) is True

    def room_known(self, pool: str, *, scope=None) -> bool:
        return all(self.applicability(item, scope) is False for item in self.events() if item["pool"] == pool)

    def walls(self, now: datetime | None = None, *, scope=None, pool=None, ship=None) -> list[str]:
        return sorted({self.describe(item) for item in self.events(now, ship=ship)
                       if (pool is None or item["pool"] == pool) and self.applicability(item, scope) is True})

    def recognized(self, seat: str, event: Wall) -> bool:
        """Persisted evidence also recognizes an old banner when its record is unreadable."""
        return self.db.execute("SELECT 1 FROM wall_events WHERE seat=? AND "
                               "(signature=? OR (error_id IS NOT NULL AND error_id=?)) LIMIT 1",
                               (seat, event.signature, event.error_id)).fetchone() is not None

    def blocked(self, seat: str, pool: str, *, scope=None) -> str | None:
        for item in self.events():
            charged = item["pool"]
            if charged == pool or (charged == UNKNOWN and item["seat"] == seat):
                scoped = {**item, "pool": pool} if charged == UNKNOWN else item
                if self.applicability(scoped, scope) is True:
                    if scope is not None and hasattr(scope, "reader") and item["wall"].harness is None:
                        item = {**item, "wall": replace(item["wall"], harness=scope.reader.name)}
                    return self.describe(item)
        return None

    def due(self, pool: str, now: datetime, interval: int) -> bool:
        last = self.db.execute("SELECT at FROM usage_reads WHERE pool=?", (pool,)).fetchone()
        if last is None or (now - datetime.fromisoformat(last[0])).total_seconds() >= interval:
            return True
        return any(p == pool and observed > last[0] for _, p, _, _, observed, _ in self.active(now))

    def last_attempt(self, pool: str) -> datetime | None:
        row = self.db.execute("SELECT at FROM usage_reads WHERE pool=?", (pool,)).fetchone()
        return datetime.fromisoformat(row[0]) if row else None

    def usage_read(self, pool: str, at: datetime, error: str | None, *, commit: bool = True) -> None:
        def write():
            self.db.execute("INSERT OR REPLACE INTO usage_reads VALUES (?,?,?)", (pool, stamp(at), error))

        if self.commit_attempt is not None:
            self.commit_attempt(write, commit=commit)
        else:
            write()
            if commit:
                self.db.commit()

    def usage_notes(self) -> list[str]:
        return [f"{pool} latest usage read: {error}" for pool, error in
                self.db.execute("SELECT pool,error FROM usage_reads WHERE error IS NOT NULL ORDER BY pool")]


def observe_screen(book, seat, screen, launched, now: datetime | None = None, *, commit: bool = True) -> None:
    """A quota banner on a seat's screen, persisted once by the service under the launch it was seen on.

    The record's own quota errors are persisted beside it when the record is read; a banner the
    book already knows, from the record or from an earlier look, is not a new event.
    """
    if screen is None or not screen.quota:
        return
    now = now or datetime.now(timezone.utc)
    event = seat.harness.quota_wall(screen.quota, now)
    if event is None or book.quota.recognized(seat.name, event):
        return
    if seat.harness.model_scoped_quota:
        from .watch import running_settings
        event = replace(event, model=running_settings(seat, screen=screen).get("model"))
    source = f"screen:{seat.name}:{launched.at if launched else 'unknown-launch'}"
    from .seat import declared_launch_pool
    book.quota.observe(source, seat.name, declared_launch_pool(seat, launched, UNKNOWN),
                       replace(event, key=event.error_id or event.key), now, commit=commit)


BACKOFF_MAX = 3600  # a refusal holds its pool at most an hour


def held(service, pool: str) -> bool:
    """A pool whose last read was refused with a Retry-After is not read again before it."""
    return time.monotonic() < service.usage_hold.get(pool, 0.0)


def record_read(service, pool: str, usage: Usage, interval: int, attempted_at: datetime, *,
                backoff: float | None = None) -> None:
    """Keep one read in the book; log a failed one with its reason and hold a refused pool by Retry-After.

    Every failed provider call is logged, so how often is readable. A state that made no call
    repeats each interval unchanged, so it is logged only when its reason differs from the
    pool's last logged one, and again once a good read has come between.
    """
    service.usage_read[pool] = time.monotonic()
    service.ledger.record_usage(pool, usage, attempted_at=attempted_at)
    if not usage.error:
        service.usage_logged.pop(pool, None)
        return
    if not usage.called and service.usage_logged.get(pool) == usage.error:
        return
    service.usage_logged[pool] = usage.error
    entry = {"pool": pool, "error": usage.error}
    if usage.retry_after is not None or backoff is not None:
        hold = min(max(usage.retry_after if usage.retry_after is not None else backoff, interval), BACKOFF_MAX)
        service.usage_hold[pool] = time.monotonic() + hold
        entry["backoff"] = round(hold)
    if not usage.called:
        entry["no_call"] = True
    service.log("usage", **entry)


def last_read(service, pool: str) -> float:
    """The monotonic time of the pool's last read, seeded after a restart from the book's stored attempt."""
    if pool not in service.usage_read:
        at = service.ledger.quota.last_attempt(pool)
        if at is None:
            return -math.inf
        elapsed = max(0.0, (datetime.now(timezone.utc) - at).total_seconds())  # a clock set back waits a full interval
        service.usage_read[pool] = time.monotonic() - elapsed
    return service.usage_read[pool]


def read_usage(service, pool, reader) -> Usage | None:
    """One context for background, live and destination reads; only native clients create it."""
    service.ledger.prepare_usage()  # settle prior intended pacing before another provider attempt
    if not getattr(reader, "private_usage_home", False):
        return reader.usage(pool)
    from . import credential

    def reconcile():
        for proof in credential.reseed(service.ship, list(service.resolved_seats())):
            service.log("credential", proof=proof)

    home = credential.usage_home(service.ship, pool)
    if home.is_symlink() or home.parent.is_symlink():
        return Usage((), datetime.now(timezone.utc), "native usage home is a link; ownership unknown", called=False)
    reconcile()  # never rebuild over a detached native refresh
    link = home / reader.credential_file
    if link.exists() and not link.is_symlink() and (not getattr(reader, "credential_copy", False)
                                                    or reader.freshness(link) is None):
        return Usage((), datetime.now(timezone.utc), "native usage credential detached and unreadable", called=False)
    try:
        return reader.usage_in_home(pool, home)
    finally:
        reconcile()  # a failed billing call can still have refreshed its sign-in


def refresh_declared(service) -> None:
    """Adapters with background subscription reads run even with no active seat."""
    from . import harness as harnesses

    shared = {}
    for pool in service.ship.pools.values():
        if pool.kind != "subscription":
            continue
        reader = harnesses.native_for(pool.provider)
        if not reader.background_usage and getattr(reader, 'idle_usage', False):
            refresh_idle(service, pool, reader)
            continue
        if (not reader.background_usage or held(service, pool.name)
                or not service.ledger.quota.due(pool.name, datetime.now(timezone.utc), reader.usage_interval)):
            continue
        key = reader.shared_usage_key(pool)
        if reader.usage_from_records is not None:
            from .record_owner import launch_reader
            sources = []
            for seat in service.resolved_seats():
                launched = seat.launched()
                from .seat import launch_pool_refusal
                if (launched is not None and launched.pool == pool.name and launch_pool_refusal(seat, launched) is None
                        and launch_reader(launched) == reader.name and seat.spec.harness == reader.name):
                    sources.append((seat.record(), launched.when))
            usage = replace(reader.usage_from_records(sources, datetime.now(timezone.utc))
                            or Usage((), datetime.now(timezone.utc), "provider usage unavailable"), called=False)
        else:
            usage = shared[key] if key is not None and key in shared else read_usage(service, pool, reader)
        usage = usage or Usage((), datetime.now(timezone.utc), "provider usage unavailable")
        if key is not None:
            shared[key] = usage
        record_read(service, pool.name, usage, reader.usage_interval, datetime.now(timezone.utc))


def refresh_idle(service, pool, reader) -> None:
    """A declared pool read with its own sign-in on the service's tick, whether or not a seat sits on it, by the same
    reader and on the same clock as a live seat's read (``last_read``): a live seat's read this pass makes this one
    wait, so the pool is read once per ``usage_interval``, never on a reading's demand. A refusal holds it by its
    Retry-After, as a live read does; a failed call with no Retry-After waits twice as long each time, at most
    ``BACKOFF_MAX``, and a good read ends the wait. Each failure is kept in the book and logged with its wait."""
    if held(service, pool.name) or time.monotonic() - last_read(service, pool.name) < reader.usage_interval:
        return
    now = datetime.now(timezone.utc)
    service.usage_read[pool.name] = time.monotonic()
    usage = read_usage(service, pool, reader) or Usage((), now, "provider usage unavailable")
    failures = getattr(service, 'usage_idle_failures', None)
    if failures is None:
        failures = service.usage_idle_failures = {}
    backoff = None
    if not usage.error:
        failures.pop(pool.name, None)
    elif usage.called and usage.retry_after is None:  # a failed call with no Retry-After of its own
        failures[pool.name] = failures.get(pool.name, 0) + 1
        backoff = min(reader.usage_interval * 2 ** failures[pool.name], BACKOFF_MAX)
    record_read(service, pool.name, usage, reader.usage_interval, now, backoff=backoff)


def refresh(service, seat, live: bool, launched) -> None:
    """A live seat's provider read, paced by its adapter."""
    reader = seat.harness
    if not live or reader.background_usage:
        return
    from .seat import declared_launch_pool
    name = declared_launch_pool(seat, launched, seat.pool.name)
    pool = service.ship.pools[name] if name in service.ship.pools else seat.pool
    if (pool.kind == "subscription" and not held(service, pool.name)
            and time.monotonic() - last_read(service, pool.name) >= reader.usage_interval):
        service.usage_read[pool.name] = time.monotonic()
        usage = read_usage(service, pool, reader)
        if usage is not None or reader.reads_usage:
            record_read(service, pool.name, usage or Usage((), datetime.now(timezone.utc), "provider usage unavailable"),
                        reader.usage_interval, datetime.now(timezone.utc))
