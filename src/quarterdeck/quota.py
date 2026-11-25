"""Persisted quota events and background refresh; readings never call the provider."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import sqlite3
import time

from . import keyfile
from .harness import claude_sign_in
from .harness.limits import Wall, Usage
from .provenance import UNKNOWN


ACCESS_WAITING_FOR_RENEWAL = "access expired, waiting for renewal"
ACCESS_LOGIN_NEEDED = "access expired, login needed"
ACCESS_TOKEN_MISSING = "no access token in the credential file"


def candidate_pools(seat) -> tuple[str, ...]:
    """Later declared choices, then earlier choices, excluding the current pool and any whose key is missing or
    refused: a launch there would be refused, so a seat with no other choice is held at its limit and the line says so."""
    pools = seat.spec.pools
    source = seat.pool.name
    if source not in pools:
        return ()  # automatic moves require a declared source in the ordering
    current = pools.index(source)
    return tuple(name for name in pools[current + 1:] + pools[:current] if keyfile.usable(seat.ship.pool(name)))


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


def model_scope(seat, pool_name: str, *, running: bool = False, screen=None, declared_fallback=False, read_only=False):
    """Adapter-owned group applicability; source evidence differs from a resolved destination."""
    from . import harness as harnesses

    declaration = getattr(seat, 'declaration', seat.spec)
    spec = None if running else declaration.for_pool(pool_name)
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
        if read_only:
            record = seat.join(publish=False).record
            model = running_settings(seat, screen=screen, record=record).get("model")
        else:
            model = running_settings(seat, screen=screen).get("model")
        if model is None and declared_fallback and pool_name in declaration.pools:
            model = declaration.for_pool(pool_name).model
        if reader.model_scoped_quota:
            if not read_only:
                record = seat.record()
            launched = seat.launched()
            sources = tuple(filter(None, (str(record) if record else None,
                            f"screen:{seat.name}:{launched.at}" if launched else None)))
    else:
        model = spec.model
    return ModelScope(pool_name, reader, model, seat.name if running else None, sources)


def window_applies(scope, pool: str, group: str | None):
    # A destination's model policy is never applied to another pool's measurements.
    return scope(group) if scope is not None and getattr(scope, "pool", pool) == pool else True


def current_walls(seat, book) -> list[str]:
    """Model-scoped wall reasons for display, without publishing the observed record join."""
    pool = seat.pool.name
    if not book.walled(pool, seat.ship) and not book.quota.blocked(seat.name, pool):
        return []  # No model reading is needed unless this pool has a wall to scope.
    scope = model_scope(seat, pool, running=True, read_only=True)
    found = book.walls(seat.ship, scope=scope, pool=pool)
    unknown = book.quota.blocked(seat.name, pool, scope=scope)
    if unknown and unknown not in found:
        found.append(unknown)
    return found


def current_walled(seat, book, *, declared_fallback=False) -> bool:
    """Main's boolean path: the pool's walls and refusals, under the running model's scope."""
    pool = seat.pool.name
    if not book.walled(pool, seat.ship) and not book.quota.blocked(seat.name, pool):
        return False  # No model reading is needed unless this pool has a wall to scope.
    scope = model_scope(seat, pool, running=True, declared_fallback=declared_fallback)
    return book.walled(pool, seat.ship, scope=scope) or book.quota.blocked(seat.name, pool, scope=scope) is not None


def cached_open(ship, book, pool_name: str, seat=None) -> bool:
    """Routing eligibility from existing evidence, with no provider read."""
    from . import harness as harnesses

    pool = ship.pools[pool_name]
    if getattr(pool, 'paid_by_key', False) and pool.budget is not None and book.catching_up():
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
            from .harness import claude_extra_usage as extra_usage

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
                CREATE TABLE IF NOT EXISTS usage_holds (
                    pool TEXT PRIMARY KEY, retry_at TEXT, set_at TEXT NOT NULL, reason TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS usage_hold_insert AFTER INSERT ON usage_reads
                    BEGIN DELETE FROM usage_holds WHERE pool=NEW.pool; END;
                CREATE TRIGGER IF NOT EXISTS usage_hold_update AFTER UPDATE ON usage_reads
                    BEGIN DELETE FROM usage_holds WHERE pool=OLD.pool OR pool=NEW.pool; END;
                CREATE TRIGGER IF NOT EXISTS usage_hold_delete AFTER DELETE ON usage_reads
                    BEGIN DELETE FROM usage_holds WHERE pool=OLD.pool; END;
            """)
            db.executescript(extra_usage.SCHEMA)
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
               *, commit: bool = True, extra_readings=()) -> None:
        for event in events:
            owner, pool = provenance.resolve(record, event.at, event.at, seat, UNKNOWN)
            self.observe(str(record), owner, pool, event, now, commit=False)
        if extra_readings:
            from .harness import claude_extra_usage as extra_usage

            for observation in extra_readings:
                _, pool = provenance.resolve(record, observation.at, observation.at, seat, UNKNOWN)
                if pool != UNKNOWN:
                    extra_usage.remember(self.db, pool, observation)
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
        from .harness.key_limits import ZAI_EVENT_PREFIX
        if event.key.startswith(ZAI_EVENT_PREFIX):
            retry = datetime.fromisoformat(item['resets_at']).astimezone(timezone.utc)
            return f"{label} {event.reason} · reset unknown · next eligible try {retry:%Y-%m-%d %H:%M:%S UTC}"
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

    def accept_zai_request(self, pool: str, at: datetime, *, commit: bool) -> None:
        """A proved accepted request after eligibility supersedes this pool's previous Z.ai refusal."""
        from .harness.key_limits import ZAI_EVENT_PREFIX

        matches = (pool, ZAI_EVENT_PREFIX + '%', stamp(at))
        where = "pool=? AND event LIKE ? AND resets_at<=? AND superseded=0"
        if self.db.execute(f"SELECT 1 FROM wall_events WHERE {where} LIMIT 1", matches).fetchone() is None:
            return
        self.db.execute(f"UPDATE wall_events SET superseded=1 WHERE {where}", matches)
        if commit:
            self.db.commit()

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

    def usage_read(self, pool: str, at: datetime, error: str | None, *,
                   retry_at: datetime | None = None, retry_reason: str | None = None,
                   commit: bool = True) -> None:
        def write():
            # Preserve the three-column attempt writer and its invalidation triggers.
            self.db.execute("INSERT OR REPLACE INTO usage_reads VALUES (?,?,?)", (pool, stamp(at), error))
            if retry_reason is not None:
                self.db.execute("INSERT OR REPLACE INTO usage_holds (pool,retry_at,set_at,reason) VALUES (?,?,?,?)",
                                (pool, stamp(retry_at), stamp(at), retry_reason))

        if self.commit_attempt is not None:
            self.commit_attempt(write, commit=commit)
        else:
            try:
                write()
            except BaseException:
                self.db.rollback()  # an incomplete attempt must never outlive its missing hold
                raise
            if commit:
                self.db.commit()  # a completed writer's failed commit remains visible to its owner

    def strict_attempt(self, pool: str, now: datetime | None = None) -> datetime | None:
        """A strict floor needs sane local attempt evidence, never an indefinite future wait."""
        at = self.last_attempt(pool)
        now = now or datetime.now(timezone.utc)
        if at is not None and (at.utcoffset() is None or at > now + timedelta(seconds=BACKOFF_MAX)):
            raise ValueError("invalid stored usage attempt; usage held for a human")
        return at

    def usage_hold(self, pool: str, now: datetime | None = None) -> datetime | None:
        """An exact stored deadline, absent on older books; invalid evidence is an error."""
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='usage_holds'").fetchone() is None:
            return None  # old read-only book; do not migrate or invent a deadline
        row = self.db.execute("SELECT retry_at,set_at,reason FROM usage_holds WHERE pool=?", (pool,)).fetchone()
        if row is None:
            return None
        now = now or datetime.now(timezone.utc)
        try:
            deadline, at = datetime.fromisoformat(row[0]), datetime.fromisoformat(row[1])
            if (row[2] != "native refusal" or deadline.utcoffset() is None or at.utcoffset() is None
                    or not 0 <= (deadline - at).total_seconds() <= BACKOFF_MAX
                    or max(deadline, at) > now + timedelta(seconds=BACKOFF_MAX)):
                raise ValueError
        except (ValueError, TypeError, OverflowError):
            raise ValueError("invalid stored usage hold; usage held for a human") from None
        return deadline

    def pacing_error(self, pool: str, *, strict: bool = False, now: datetime | None = None) -> str | None:
        """Safe operational detail only; never includes stored values or provider content."""
        try:
            self.usage_hold(pool, now)
            if strict:
                self.strict_attempt(pool, now)
        except (ValueError, TypeError, OverflowError, sqlite3.DatabaseError):
            return "stored usage timing invalid; native reads held; needs a human"
        return None

    def usage_notes(self) -> list[str]:
        notes = [f"{pool} latest usage read: {error}" for pool, error in
                 self.db.execute("SELECT pool,error FROM usage_reads WHERE error IS NOT NULL ORDER BY pool")]
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='usage_holds'").fetchone():
            for pool, in self.db.execute("SELECT pool FROM usage_holds ORDER BY pool").fetchall():
                try:
                    self.usage_hold(pool)
                except (ValueError, sqlite3.DatabaseError) as error:
                    notes.append(f"{pool}: {error}")
        return notes


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


def observe_key_refusal(book, seat, now: datetime | None = None, *, commit: bool = True) -> None:
    """A provider's refusal of a key's work, once, for the launch that met it (harness.key_limits)."""
    from .harness import key_limits

    launched = seat.launched()
    if launched is None or (pool := seat.ship.pools.get(launched.pool)) is None or not pool.paid_by_key:
        return
    from .harness.key_routes import ZAI_CHAT, zai_route
    from .provenance import provider_route
    launch_route = getattr(launched, 'provider_route', None)
    now = now or datetime.now(timezone.utc)
    if pool.provider == 'zai' or (launch_route and launch_route['provider'] == 'zai'):
        # A same-name edit cannot turn another provider's refusal into Z.ai evidence,
        # or reinterpret a Z.ai refusal as a native subscription rate wall.
        if zai_route(pool) != ZAI_CHAT or launch_route != provider_route(pool):
            return
        event, accepted = key_limits.zai_outcome(seat, launched)
        if accepted is not None and accepted <= now:
            book.quota.accept_zai_request(pool.name, accepted, commit=commit)
    else:
        event = key_limits.refusal(seat)
    if (event is None or event.resets_at <= now or not datetime.fromisoformat(launched.at) <= event.at <= now
            or book.quota.recognized(seat.name, event)):
        return
    book.quota.observe(f'key:{seat.name}:{launched.at}', seat.name, launched.pool, event, now, commit=commit)


BACKOFF_MAX = 3600  # a refusal holds its pool at most an hour


def held(service, pool: str) -> bool:
    """A pool whose last read was refused with a Retry-After is not read again before it."""
    memory = time.monotonic() < service.usage_hold.get(pool, 0.0)
    reader = getattr(service.ledger.quota, "usage_hold", None)
    if reader is None:
        return memory
    try:
        deadline = reader(pool)
    except (ValueError, sqlite3.DatabaseError):
        error = "stored usage hold unreadable or invalid; native usage held for a human"
        if service.usage_logged.get(pool) != error:
            service.usage_logged[pool] = error
            service.log("usage", pool=pool, error=error, no_call=True)
        return True  # corrupt evidence never grants permission for another call
    return memory or (deadline is not None and datetime.now(timezone.utc) < deadline)


def pacing_alerts(ship, now: datetime | None = None) -> list[str]:
    """Read-only status-line and detailed-status alerts, including pools with no seat."""
    from .harness import native_for
    from .ledger import Ledger
    try:
        book = Ledger.reading(ship.runtime / "ledger.sqlite")
        if book is None:
            return []
        with book:
            alerts = []
            for pool in ship.pools.values():
                if pool.kind == "subscription":
                    reader = native_for(pool.provider)
                    error = book.quota.pacing_error(pool.name, strict=getattr(reader, "strict_usage_interval", False), now=now)
                    if error:
                        alerts.append(f"{pool.name}: {error}")
            return alerts
    except (OSError, sqlite3.DatabaseError):
        return ["usage pacing evidence unreadable; needs a human"]


def record_read(service, pool: str, usage: Usage, interval: int, attempted_at: datetime, *,
                backoff: float | None = None) -> None:
    """Keep one read in the book; log a failed one with its reason and hold a refused pool by Retry-After.

    Every failed provider call is logged, so how often is readable. A state that made no call
    repeats each interval unchanged, so it is logged only when its reason differs from the
    pool's last logged one, and again once a good read has come between.
    """
    retry_at, retry_reason, hold = None, None, None
    if usage.error and usage.retry_after is not None:
        delay = usage.retry_after
        if type(delay) not in (int, float) or not math.isfinite(delay):
            hold = BACKOFF_MAX
            # Unknown remote delay is an ordinary bounded refusal, not corrupt
            # local evidence. Neither the remote error nor delay is logged.
            usage = replace(usage, error="native usage refused; retry delay unavailable; hold 3600 seconds",
                            retry_after=BACKOFF_MAX)
        else:
            hold = min(max(delay, interval), BACKOFF_MAX)
        retry_at = attempted_at + timedelta(seconds=hold)
        retry_reason = "native refusal"
    service.usage_read[pool] = time.monotonic()
    if retry_reason is None:
        service.ledger.record_usage(pool, usage, attempted_at=attempted_at)
    else:
        service.ledger.record_usage(pool, usage, attempted_at=attempted_at,
                                    retry_at=retry_at, retry_reason=retry_reason)
    if not usage.error:
        service.usage_hold.pop(pool, None)
        service.usage_logged.pop(pool, None)
        return
    if not usage.called and service.usage_logged.get(pool) == usage.error:
        return
    service.usage_logged[pool] = usage.error
    entry = {"pool": pool, "error": usage.error}
    if retry_at is not None:
        remaining = max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
        service.usage_hold[pool] = time.monotonic() + remaining
    elif usage.retry_after is None and backoff is not None:
        hold = min(max(backoff, interval), BACKOFF_MAX)
        service.usage_hold[pool] = time.monotonic() + hold
    if hold is not None:
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


def strict_ready(service, pool, reader) -> bool:
    """One absolute floor for every opted-in live/background native read."""
    if not getattr(reader, "strict_usage_interval", False):
        return True
    if held(service, pool.name):
        return False
    try:
        at = service.ledger.quota.strict_attempt(pool.name)
        elapsed = math.inf if at is None else max(0.0, (datetime.now(timezone.utc) - at).total_seconds())
        return (elapsed >= reader.usage_interval
                and time.monotonic() - last_read(service, pool.name) >= reader.usage_interval)
    except (ValueError, TypeError, OverflowError, sqlite3.DatabaseError):
        error = "stored usage attempt invalid; native reads held for a human"
        if service.usage_logged.get(pool.name) != error:
            service.usage_logged[pool.name] = error
            service.log("usage", pool=pool.name, error=error, no_call=True)
        return False


def read_usage(service, pool, reader) -> Usage | None:
    """One context for background, live and destination reads; only native clients create it."""
    service.ledger.prepare_usage()  # settle prior intended pacing before another provider attempt
    if not strict_ready(service, pool, reader):
        return None  # not a new attempt: callers must not record or slide its floor
    if (getattr(reader, "sign_in_metadata", None) is not None and pool.kind == "subscription"
            and pool.config_dir is not None and reader.credential_file):
        source = pool.config_dir / reader.credential_file
        try:
            canonical = claude_sign_in.location(pool)
        except (OSError, RuntimeError):
            canonical = None
        sign_in = claude_sign_in.observe(reader, canonical or source)
        if sign_in is not None:
            now = datetime.now(timezone.utc)
            if sign_in.access_expired(now):
                if sign_in.can_renew(now):
                    reason = ACCESS_WAITING_FOR_RENEWAL
                    if canonical is not None:
                        identity = claude_sign_in.renewable_identity(reader, canonical, sign_in, now)
                        if identity is not None:
                            return claude_sign_in.ExpiredAccess((), now, reason, called=False, identity=identity)
                elif sign_in.has_access:
                    reason = ACCESS_LOGIN_NEEDED
                else:
                    reason = ACCESS_TOKEN_MISSING
                return Usage((), now, reason, called=False)
    if not getattr(reader, "private_usage_home", False):
        return reader.usage(pool)
    from . import credential
    from .ship import program_adapter

    reader = program_adapter(service.ship, reader)

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
        if not strict_ready(service, pool, reader):
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
        if usage is None and getattr(reader, "strict_usage_interval", False):
            continue
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
    if not live or reader.background_usage or getattr(seat, 'withdrawn_pool', None) is not None:
        return
    from .seat import declared_launch_pool
    name = declared_launch_pool(seat, launched, seat.pool.name)
    pool = service.ship.pools[name] if name in service.ship.pools else seat.pool
    if (pool.kind == "subscription" and strict_ready(service, pool, reader) and not held(service, pool.name)
            and time.monotonic() - last_read(service, pool.name) >= reader.usage_interval):
        if not getattr(reader, "strict_usage_interval", False):
            service.usage_read[pool.name] = time.monotonic()
        usage = read_usage(service, pool, reader)
        if usage is None and getattr(reader, "strict_usage_interval", False):
            return
        if usage is not None or reader.reads_usage:
            record_read(service, pool.name, usage or Usage((), datetime.now(timezone.utc), "provider usage unavailable"),
                        reader.usage_interval, datetime.now(timezone.utc))
