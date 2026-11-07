"""One complete real measurement per pool/window, plus its latest attempt verdict.

A failure changes freshness metadata, never pieces of the retained measurement.
There is no reading-history table and rejected numeric values are not stored.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import math

from .quota import reset_text

MAX_AGE = 3600  # the service's existing availability freshness bound


def aware(value):
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def parse(value):
    try:
        value = datetime.fromisoformat(value)
        return value if aware(value) else None
    except (TypeError, ValueError):
        return None


def invalid(used, reset, at):
    if type(used) not in (int, float) or not 0 <= used <= 1 or not math.isfinite(used):
        return 'fraction unavailable, nonfinite or outside [0,1]'
    if not aware(reset):
        return 'reset unavailable or invalid (timezone required)'
    if not aware(at):
        return 'reading time unavailable or invalid'
    if reset <= at:
        return 'reset is not after the reading time'
    return None


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

    @property
    def complete(self):
        return invalid(self.used, parse(self.resets_at), parse(self.read_at)) is None

    def reason(self, now):
        if self.error:
            return self.error
        if not self.complete:
            return 'no complete real reading available'
        if parse(self.resets_at) <= now:
            return 'retained reset has passed'
        if (now - parse(self.read_at)).total_seconds() >= MAX_AGE:
            return 'reading is older than one hour'
        return None

    def current(self, now):
        return self.reason(now) is None

    def describe(self, now=None):
        now = now or datetime.now(timezone.utc)
        label = f'{self.pool} {self.window}'
        reason = self.reason(now)
        if not self.complete:
            return f'{label} usage unknown · {reason}'
        age = max(0, int((now - parse(self.read_at)).total_seconds() // 60))
        value = f'{self.used:.0%} used' + (f' ({self.bucket_name})' if self.bucket_name else '')
        reading = f'read {self.read_at} ({age}m ago)'
        stale = f' · LAST KNOWN · {reason}' if reason else ''
        return f'{label} {value}{stale} · {reset_text(self.resets_at)} · {reading}'


class UsageCache:
    def __init__(self, db):
        self.db = db
        columns = {r[1] for r in db.execute('PRAGMA table_info(pools)')}
        for name in ('attempted_at', 'error'):
            if name not in columns:
                db.execute(f'ALTER TABLE pools ADD COLUMN {name} TEXT')
        # Migration adds metadata only. Existing tuples and identities are retained.
        db.execute('UPDATE pools SET attempted_at=read_at WHERE attempted_at IS NULL')

    def rows(self, pool=None):
        sql = ('SELECT pool,window,used,resets_at,read_at,blocks,group_name,bucket,bucket_name,attempted_at,error FROM pools'
               + (' WHERE pool=?' if pool is not None else '') + ' ORDER BY pool,window')
        return [Reading(*r) for r in self.db.execute(sql, (pool,) if pool is not None else ())]

    def record(self, pool, usage, attempted_at):
        previous = {r.window:r for r in self.rows(pool)}
        grouped = {}
        for window in usage.windows:
            grouped.setdefault(window.name, []).append(window)
        errors = [usage.error] if usage.error else []
        for name in sorted(set(previous) | set(grouped)):
            old, candidates = previous.get(name), grouped.get(name, [])
            new = candidates[0] if candidates else None
            if not candidates:
                error = 'window missing from latest usage read' + (f': {usage.error}' if usage.error else '')
            elif len(candidates) != 1:
                error = 'ambiguous duplicate bucket'
            else:
                error = new.error or invalid(new.used, new.resets_at, usage.read_at)
                if error and usage.error and not new.error:
                    error = usage.error
                if old and old.bucket is not None and new.bucket is not None and old.bucket != new.bucket:
                    error = 'bucket identity changed; new reading could not be attributed to this window'
                if not error and old and old.group is not None and new.group != old.group:
                    error = 'group identity changed; new reading could not be attributed to this window'
            if error:
                errors.append(f'{name}: {error}')
                if old:
                    self.db.execute('UPDATE pools SET attempted_at=?,error=? WHERE pool=? AND window=?',
                                    (attempted_at.isoformat(),error,pool,name))
                else:
                    self.db.execute('INSERT INTO pools (pool,window,blocks,group_name,attempted_at,error) VALUES (?,?,?,?,?,?)',
                                    (pool,name,new.blocks,new.group,attempted_at.isoformat(),error))
                continue
            bucket = new.bucket if new.bucket is not None else (old.bucket if old else None)
            self.db.execute('INSERT INTO pools (pool,window,used,resets_at,read_at,blocks,group_name,bucket,bucket_name,attempted_at,error) '
                            'VALUES (?,?,?,?,?,?,?,?,?,?,NULL) ON CONFLICT(pool,window) DO UPDATE SET '
                            'used=excluded.used,resets_at=excluded.resets_at,read_at=excluded.read_at,blocks=excluded.blocks,'
                            'group_name=excluded.group_name,bucket=excluded.bucket,bucket_name=excluded.bucket_name,'
                            'attempted_at=excluded.attempted_at,error=NULL',
                            (pool,name,new.used,new.resets_at.isoformat(),usage.read_at.isoformat(),new.blocks,new.group,
                             bucket,new.bucket_name,attempted_at.isoformat()))
        if not grouped and not errors:
            errors.append('usage windows unavailable')
        return '; '.join(dict.fromkeys(errors)) or None
