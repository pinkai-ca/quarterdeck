"""One complete real measurement per pool/window, plus its latest attempt verdict.

A failure changes freshness metadata, never pieces of the retained measurement.
Weekly accepted numeric readings also feed the bounded forecast history;
rejected numeric values are not stored.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import sqlite3

from .harness.limits import Usage, Window

from .quota import ACCESS_LOGIN_NEEDED, ACCESS_TOKEN_MISSING, ACCESS_WAITING_FOR_RENEWAL, reset_text

MAX_AGE = 3600  # the service's availability freshness bound
MISSING = "window missing from latest usage read"
READ_SKIPPED = "usage read skipped: "
RESET_PASSED = "retained reset has passed"


def skipped_access_read(error: str | None) -> str | None:
    """Recognize the three expired-access outcomes in retained rows and pool attempts."""
    # A pool attempt starts with the outcome, followed by any per-window diagnostics.
    reason = (error or "").partition("; ")[0].removeprefix(f"{MISSING}: ")
    if reason in (ACCESS_WAITING_FOR_RENEWAL, ACCESS_LOGIN_NEEDED, ACCESS_TOKEN_MISSING):
        return READ_SKIPPED + reason
    return None


def aware(value: object) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def parse(value: str | None) -> datetime | None:
    try:
        value = datetime.fromisoformat(value)
        return value if aware(value) else None
    except (TypeError, ValueError):
        return None


def invalid(used: object, reset: datetime | None, at: datetime | None) -> str | None:
    if type(used) not in (int, float) or not 0 <= used <= 1 or not math.isfinite(used):
        return "fraction unavailable, nonfinite or outside [0,1]"
    if not aware(reset):
        return "reset unavailable or invalid (timezone required)"
    if not aware(at):
        return "reading time unavailable or invalid"
    if reset <= at:
        return "reset is not after the reading time"
    return None


def warning_evidence(pool, window, used, resets_at, read_at, blocks, group) -> str | None:
    """Bind a provider's near-limit warning to its complete tuple; an older writer cannot leave a stale one.

    Stored in the column ``exhausted``, the name an earlier writer gave it when it read the same
    severity as a wall. It is warning evidence only: no wall is ever read from it.
    """
    if type(used) not in (int, float) or not isinstance(resets_at, str) or not isinstance(read_at, str):
        return None
    number = float(used)
    number = 0.0 if number == 0 else number  # SQLite REAL does not retain the sign of zero.
    identity = (pool, window, number, resets_at, read_at, bool(blocks), group)
    return "v1:" + hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Reading:
    pool: str
    window: str
    used: float | None
    resets_at: str | None
    read_at: str | None
    blocks: bool
    group: str | None
    bucket: str | None
    bucket_name: str | None
    attempted_at: str | None
    error: str | None
    warning: bool = False  # the provider marks it near or at its limit; never a wall

    @property
    def complete(self) -> bool:
        return invalid(self.used, parse(self.resets_at), parse(self.read_at)) is None

    def reason(self, now: datetime) -> str | None:
        if self.error:
            return self.error
        if not self.complete:
            return "no complete real reading available"
        if parse(self.resets_at) <= now:
            return RESET_PASSED
        if (now - parse(self.read_at)).total_seconds() >= MAX_AGE:
            return "reading is older than one hour"
        return None

    def current(self, now: datetime) -> bool:
        return self.reason(now) is None

    def display_reason(self, now: datetime) -> str | None:
        if skipped := skipped_access_read(self.error):
            if self.complete and parse(self.resets_at) <= now:
                return f"{RESET_PASSED} · {skipped}"
            return skipped
        return self.reason(now)

    @property
    def label(self) -> str:
        scope = f"{self.group} / " if self.group and self.group not in self.window else ""
        return f"{self.pool} {scope}{self.window}"

    def describe(self, now: datetime | None = None, *, inspect_command: str | None = "qd ledger") -> str:
        now = now or datetime.now(timezone.utc)
        label = self.label
        reason = self.display_reason(now)
        next_step = f"; inspect with {inspect_command}" if reason and inspect_command else ""
        if not self.complete:
            return f"{label} usage unknown · {reason}{next_step}"
        age = max(0, int((now - parse(self.read_at)).total_seconds() // 60))
        reading = f"read {self.read_at} ({age}m ago)"
        if skipped_access_read(self.error) and parse(self.resets_at) <= now:
            return f"{label} usage unknown · {reason} · {reset_text(self.resets_at)} · last {reading}{next_step}"
        value = f"{self.used:.0%} used" + (f" ({self.bucket_name})" if self.bucket_name else "")
        stale = f" · LAST KNOWN · {reason}" if reason else ""
        warning = " · provider warns it is near its limit" if self.warning else ""
        return f"{label} {value}{warning}{stale} · {reset_text(self.resets_at)} · {reading}{next_step}"


class UsageCache:
    def __init__(self, db: sqlite3.Connection, create: bool = True) -> None:
        self.db = db
        if not create:  # a reading opens the book read-only and migrates nothing
            return
        from .usage_forecast import SCHEMA
        db.execute(SCHEMA)
        columns = {r[1] for r in db.execute("PRAGMA table_info(pools)")}
        for name in ("attempted_at", "error"):
            if name not in columns:
                db.execute(f"ALTER TABLE pools ADD COLUMN {name} TEXT")
        # Only attempt metadata is added; measurement tuples and identities stay intact.
        db.execute("UPDATE pools SET attempted_at=read_at WHERE attempted_at IS NULL")

    def rows(self, pool: str | None = None) -> list[Reading]:
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(pools)")}
        warning = "exhausted" if "exhausted" in columns else "''"
        sql = (f"SELECT pool,window,used,resets_at,read_at,blocks,group_name,bucket,bucket_name,attempted_at,error,{warning} FROM pools"
               + (" WHERE pool=?" if pool is not None else "") + " ORDER BY pool,window")
        # A warning left by an older writer that no longer matches its measurement is simply no warning.
        return [Reading(*row[:5], bool(row[5]), *row[6:11], bool(row[11]) and row[11] == warning_evidence(*row[:7]))
                for row in self.db.execute(sql, (pool,) if pool is not None else ())]

    def record(self, pool: str, usage: Usage, attempted_at: datetime) -> str | None:
        previous = {r.window: r for r in self.rows(pool)}
        grouped: dict[str, list[Window]] = {}
        for window in usage.windows:
            grouped.setdefault(window.name, []).append(window)
        errors = ([usage.error] if usage.error else []) + list(usage.diagnostics)
        accepted = []
        for name in sorted(set(previous) | set(grouped)):
            old, candidates = previous.get(name), grouped.get(name, [])
            new = candidates[0] if candidates else None
            if not candidates:
                error = MISSING + (f": {usage.error}" if usage.error else "")
            elif len(candidates) != 1:
                error = "ambiguous duplicate bucket"
            else:
                error = new.error or invalid(new.used, new.resets_at, usage.read_at)
                if error and usage.error and not new.error:
                    error = usage.error
                if old and old.bucket is not None and new.bucket is not None and old.bucket != new.bucket:
                    error = "bucket identity changed; new reading could not be attributed to this window"
                if not error and old:
                    if parse(old.read_at) is not None and usage.read_at < parse(old.read_at):
                        error = "source observation older than retained reading"
                    elif old.error and parse(old.attempted_at) is not None and usage.read_at <= parse(old.attempted_at):
                        error = old.error  # recovery needs an observation newer than the failed attempt
                if not error and old and old.group is not None and new.group != old.group:
                    error = "group identity changed; new reading could not be attributed to this window"
            if error:
                errors.append(f"{name}: {error}")
                if old:
                    self.db.execute("UPDATE pools SET attempted_at=?,error=? WHERE pool=? AND window=?",
                                    (attempted_at.isoformat(), error, pool, name))
                else:
                    self.db.execute("INSERT INTO pools (pool,window,blocks,group_name,attempted_at,error) VALUES (?,?,?,?,?,?)",
                                    (pool, name, new.blocks, new.group, attempted_at.isoformat(), error))
                continue
            bucket = new.bucket if new.bucket is not None else (old.bucket if old else None)
            warning = warning_evidence(pool, name, new.used, new.resets_at.isoformat(),
                                       usage.read_at.isoformat(), new.blocks, new.group) if new.warning else ""
            self.db.execute("INSERT INTO pools (pool,window,used,resets_at,read_at,blocks,group_name,bucket,bucket_name,attempted_at,error,exhausted) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(pool,window) DO UPDATE SET "
                            "used=excluded.used,resets_at=excluded.resets_at,read_at=excluded.read_at,blocks=excluded.blocks,"
                            "group_name=excluded.group_name,bucket=excluded.bucket,bucket_name=excluded.bucket_name,"
                            "attempted_at=excluded.attempted_at,error=excluded.error,exhausted=excluded.exhausted",
                            (pool, name, new.used, new.resets_at.isoformat(), usage.read_at.isoformat(), new.blocks, new.group,
                             bucket, new.bucket_name, attempted_at.isoformat(), new.uncertainty, warning))
            if new.uncertainty:
                errors.append(f"{name}: {new.uncertainty}")
            from .usage_forecast import remember
            remember(self.db, pool, new, usage.read_at, bucket)
            accepted.append(new)
        retired = self.reconcile(pool, previous, grouped, accepted, usage, attempted_at)
        errors = [error for error in errors if not any(error.startswith(name + ": ") for name in retired)]
        if not grouped and not errors:
            errors.append("usage windows unavailable")
        return "; ".join(dict.fromkeys(errors)) or None


    def reconcile(self, pool, previous, grouped, accepted, usage, attempted_at) -> set[str]:
        """Retire one obsolete identity only on unique, newer same-group/cap/reset evidence."""
        candidates = {}
        if (usage.error or not aware(usage.read_at) or not aware(attempted_at)
                or (attempted_at - usage.read_at).total_seconds() >= MAX_AGE):
            return set()
        for new in accepted:
            if new.uncertainty:
                continue
            for name, tolerance in new.replaces:
                old = previous.get(name)
                if (old is None or name in grouped or not old.complete or old.group is None
                        or old.group != new.group or old.bucket != new.bucket or old.blocks != new.blocks
                        or usage.read_at <= parse(old.read_at)
                        or (parse(old.attempted_at) is not None and usage.read_at <= parse(old.attempted_at))
                        or type(tolerance) not in (int, float) or not math.isfinite(tolerance) or tolerance < 0
                        or abs((new.resets_at - parse(old.resets_at)).total_seconds()) > tolerance):
                    continue
                candidates.setdefault(name, set()).add(new.name)
        retired = set()
        for name, names in candidates.items():
            if len(names) != 1:
                continue
            old = previous[name]
            deleted = self.db.execute("DELETE FROM pools WHERE pool=? AND window=? AND group_name IS ? AND bucket IS ? "
                                      "AND resets_at IS ? AND read_at IS ? AND used IS ? AND blocks=?",
                                      (pool, name, old.group, old.bucket, old.resets_at, old.read_at, old.used, old.blocks))
            if deleted.rowcount == 1:
                retired.add(name)
        return retired
