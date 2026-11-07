"""Persisted quota events and background refresh; readings never call the provider."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import time

from .harness.limits import Wall
from .provenance import UNKNOWN

GOOGLE_INTERVAL = 300


def stamp(at: datetime | None) -> str | None:
    return at.astimezone(timezone.utc).isoformat() if at is not None else None


def reset_text(at: str | None, from_screen: bool = False) -> str:
    if not at:
        return "reset unknown"
    label = " (estimated from first screen observation)" if from_screen else ""
    return f"reset {datetime.fromisoformat(at).astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}{label}"


class Quotas:
    def __init__(self, db):
        self.db = db
        db.executescript("""
            CREATE TABLE IF NOT EXISTS wall_events (
                source TEXT NOT NULL, event TEXT NOT NULL, seat TEXT NOT NULL, pool TEXT NOT NULL,
                at TEXT, resets_at TEXT, observed_at TEXT NOT NULL, reason TEXT NOT NULL,
                error_id TEXT, signature TEXT NOT NULL, superseded INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (source, event));
            CREATE TABLE IF NOT EXISTS usage_reads (pool TEXT PRIMARY KEY, at TEXT NOT NULL, error TEXT);
        """)

    def observe(self, source: str, seat: str, pool: str, event: Wall, now: datetime) -> None:
        self.db.execute("INSERT OR IGNORE INTO wall_events VALUES (?,?,?,?,?,?,?,?,?,?,0)",
                        (source, event.key, seat, pool, stamp(event.at), stamp(event.resets_at), stamp(now),
                         event.reason, event.error_id, event.signature))
        # Only fill missing evidence. A duplicate observation never shifts a known time/reset.
        self.db.execute("UPDATE wall_events SET pool=CASE WHEN pool=? THEN ? ELSE pool END, "
                        "at=COALESCE(at,?), resets_at=COALESCE(resets_at,?) WHERE source=? AND event=?",
                        (UNKNOWN, pool, stamp(event.at), stamp(event.resets_at), source, event.key))
        if not source.startswith("screen:"):
            self.db.execute("UPDATE wall_events SET superseded=1 WHERE source LIKE 'screen:%' AND seat=? "
                            "AND (signature=? OR (error_id IS NOT NULL AND error_id=?))",
                            (seat, event.signature, event.error_id))
        self.db.commit()

    def record(self, record, seat: str, events: tuple[Wall, ...], provenance, now: datetime) -> None:
        for event in events:
            owner, pool = provenance.resolve(record, event.at, event.at, seat, UNKNOWN)
            self.observe(str(record), owner, pool, event, now)

    def active(self, now: datetime | None = None) -> list[tuple]:
        now = now or datetime.now(timezone.utc)
        rows = self.db.execute("SELECT seat,pool,reason,resets_at,observed_at,source FROM wall_events WHERE superseded=0").fetchall()
        return [r for r in rows if r[3] is None or datetime.fromisoformat(r[3]) > now]

    def walls(self, now: datetime | None = None) -> list[str]:
        return sorted({f"{pool if pool != UNKNOWN else seat + ' (pool unknown)'} {reason} · {reset_text(reset, source.startswith('screen:'))}"
                       for seat, pool, reason, reset, _, source in self.active(now)})

    def recognized(self, seat: str, event: Wall) -> bool:
        """Persisted evidence also recognizes an old banner when its record is unreadable."""
        return self.db.execute("SELECT 1 FROM wall_events WHERE seat=? AND "
                               "(signature=? OR (error_id IS NOT NULL AND error_id=?)) LIMIT 1",
                               (seat, event.signature, event.error_id)).fetchone() is not None

    def blocked(self, seat: str, pool: str) -> str | None:
        for owner, charged, reason, reset, _, source in self.active():
            if charged == pool or (charged == UNKNOWN and owner == seat):
                return f"{charged if charged != UNKNOWN else 'pool unknown'} {reason} · {reset_text(reset, source.startswith('screen:'))}"
        return None

    def due(self, pool: str, now: datetime) -> bool:
        last = self.db.execute("SELECT at FROM usage_reads WHERE pool=?", (pool,)).fetchone()
        if last is None or (now - datetime.fromisoformat(last[0])).total_seconds() >= GOOGLE_INTERVAL:
            return True
        return any(p == pool and observed > last[0] for _, p, _, _, observed, _ in self.active(now))

    def usage_read(self, pool: str, at: datetime, error: str | None) -> None:
        self.db.execute("INSERT OR REPLACE INTO usage_reads VALUES (?,?,?)", (pool, stamp(at), error))
        self.db.commit()

    def usage_notes(self) -> list[str]:
        return [f"{pool} usage unknown: {error}" for pool, error in
                self.db.execute("SELECT pool,error FROM usage_reads WHERE error IS NOT NULL ORDER BY pool")]


def guard(seat, screen) -> str | None:
    """Record first, durable events second, screen fallback last. No authenticated queries."""
    from .ledger import Ledger
    from .harness.antigravity_quota import wall

    if not hasattr(seat, "ship"):
        return None
    path = seat.ship.runtime / "ledger.sqlite"
    is_google = seat.harness.name == "antigravity"
    if not is_google and not path.exists():
        return None
    book = Ledger(path)
    try:
        launched = seat.launched()
        pool = launched.pool if launched else seat.pool.name
        if is_google:
            record = seat.record()
            book.provenance.sync(seat, record, launched)
            events = ()
            if record is not None:
                try:
                    events = seat.harness.state(record).walls
                    book.quota.record(record, seat.name, events, book.provenance, datetime.now(timezone.utc))
                except OSError:
                    pass  # a read failure cannot erase an already persisted wall
            if screen and screen.quota:
                now = datetime.now(timezone.utc)
                event = wall(screen.quota, "individual-quota", now)
                if not book.quota.recognized(seat.name, event):
                    source = f"screen:{seat.name}:{launched.at if launched else 'unknown-launch'}"
                    book.quota.observe(source, seat.name, launched.pool if launched else UNKNOWN,
                                       replace(event, key=event.error_id or event.key), now)
        observed = book.quota.blocked(seat.name, pool)
        measured = next((w for w in book.walls(seat.ship) if w.split(" ")[0] == pool), None)
        return observed or measured
    finally:
        book.close()


def refresh_declared(service) -> None:
    """Declared Google subscriptions are read even when all seats use other programs."""
    from .harness.antigravity import AntigravityHarness

    shared = None
    for pool in service.ship.pools.values():
        if getattr(pool, "provider", None) != "antigravity" or pool.kind != "subscription":
            continue
        if not service.ledger.quota.due(pool.name, datetime.now(timezone.utc)):
            continue
        # No account selector is qualified. Default-sign-in pool names are aliases,
        # so one measured result suffices; an explicit account directory stays unknown.
        usage = shared if pool.config_dir is None and shared is not None else AntigravityHarness().usage(pool)
        if pool.config_dir is None:
            shared = usage
        service.ledger.record_usage(pool.name, usage)
        service.ledger.quota.usage_read(pool.name, datetime.now(timezone.utc), usage.error)


def refresh(service, seat, live: bool, launched) -> None:
    """Other providers retain their existing live-seat cadence."""
    if not live or seat.harness.name == "antigravity":
        return
    pool = service.ship.pools[launched.pool] if launched and launched.pool in service.ship.pools else seat.pool
    if pool.kind == "subscription" and time.monotonic() - service.usage_read.get(pool.name, -60) >= 60:
        service.usage_read[pool.name] = time.monotonic()
        usage = seat.harness.usage(pool)
        if usage:
            service.ledger.record_usage(pool.name, usage)
