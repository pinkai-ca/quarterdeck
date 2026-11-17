"""Weekly subscription spending forecasts, from numeric Ledger observations alone.

Three hours reacts to a subscription spent within a working shift without treating
one short jump as a trend. Require three observations spanning at least an hour;
never bridge a reset, a decreasing meter, an identity change or an hour-long gap.
Forecasts notify people only and supply no pool-selection or lifecycle evidence.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
import sqlite3

from . import durable, post
from .ship import ShipError
from .usage_cache import MAX_AGE, invalid, parse

LOOKBACK = timedelta(hours=3)
MIN_SPAN = timedelta(hours=1)
MAX_GAP = timedelta(hours=1)
RETENTION = timedelta(days=1)
# Two readings name the same week when their resets lie within a minute. A provider's reset time jitters below the
# second from read to read (one subscription's resets read .143 to .973 past the same second), so exact equality
# broke every run at its second point and no forecast was ever made. A real reset moves by a week, and the shortest
# window that resets is hours long, so a minute is far above the jitter and far below any true move.
RESET_TOLERANCE = timedelta(minutes=1)
SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_samples (
    pool TEXT NOT NULL, window TEXT NOT NULL, read_at TEXT NOT NULL,
    used REAL, resets_at TEXT NOT NULL, group_name TEXT, bucket TEXT,
    PRIMARY KEY (pool, window, read_at)
);
"""


def same_reset(first, second):
    """Whether two reset times name the same week: within RESET_TOLERANCE. The one rule for both the forecast's run
    and the once-per-reset report, so neither breaks nor repeats on the jitter."""
    return first is not None and second is not None and abs(first - second) <= RESET_TOLERANCE


def weekly(window):
    return window == '7d' or window.endswith(' / 7d')


def remember(db, pool, window, at, bucket):
    """Part of the existing usage transaction; repeat reads add no observations."""
    if not weekly(window.name) or not window.blocks or window.uncertainty:
        return
    stamp = at.astimezone(timezone.utc).isoformat()
    reset = window.resets_at.astimezone(timezone.utc).isoformat()
    db.execute('''INSERT INTO usage_samples VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(pool,window,read_at) DO UPDATE SET used=
        CASE WHEN used IS excluded.used AND resets_at=excluded.resets_at
                  AND group_name IS excluded.group_name AND bucket IS excluded.bucket
             THEN used ELSE NULL END''',
        (pool, window.name, stamp, window.used, reset, window.group, bucket))
    db.execute('DELETE FROM usage_samples WHERE pool=? AND read_at<?',
               (pool, (at - RETENTION).astimezone(timezone.utc).isoformat()))


@dataclass(frozen=True)
class Forecast:
    pool: str
    window: str
    rate_per_hour: float
    spent_at: datetime
    resets_at: datetime
    first_at: datetime
    read_at: datetime
    samples: int

    @property
    def warning(self):
        return self.spent_at < self.resets_at

    def describe(self, *, detail=False):
        spent = self.spent_at.astimezone()
        reset = self.resets_at.astimezone()
        text = (f'{self.pool}: spent by {spent:%d %b %H:%M} at this rate '
                f'(reset {reset:%d %b %H:%M}) · {self.rate_per_hour:.1%}/h')
        if detail:
            hours = (self.read_at - self.first_at).total_seconds() / 3600
            text += (f' over {hours:.1f}h ({self.samples} readings); '
                     f'{self.window}; warning only')
        return text


def estimate(reading, rows, now):
    if (not weekly(reading.window) or not reading.blocks or not reading.current(now)
            or parse(reading.read_at) > now or reading.used >= 1):
        return None
    latest = parse(reading.read_at)
    identity = (parse(reading.resets_at), reading.group, reading.bucket)
    points = []
    for at, used, reset, group, bucket in reversed(rows):
        at, reset = parse(at), parse(reset)
        if at is None or at > latest:
            return None
        if at < latest - LOOKBACK:
            break
        if invalid(used, reset, at) or not same_reset(reset, identity[0]) or (group, bucket) != identity[1:]:
            break
        if points and (points[-1][0] - at > MAX_GAP or used > points[-1][1]):
            break
        points.append((at, used))
    if (len(points) < 3 or points[0] != (latest, reading.used)
            or latest - points[-1][0] < MIN_SPAN):
        return None
    first, used = points[-1]
    rate = (reading.used - used) / ((latest - first).total_seconds() / 3600)
    if rate <= 0 or not math.isfinite(rate):
        return None
    try:
        spent = latest + timedelta(hours=(1 - reading.used) / rate)
    except OverflowError:
        return None
    if spent <= now:
        return None  # do not keep predicting a past wall from a stale observation
    return Forecast(reading.pool, reading.window, rate, spent, identity[0], first, latest, len(points))


def collect(book, now, pools=None):
    """One weekly exhaustion estimate per pool: the earliest among the windows that warn, so a window that runs out
    only after its own earlier reset cannot hide another's real warning; the earliest overall where none warns.
    Older books have none."""
    if not book.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='usage_samples'").fetchone():
        return []
    found = {}
    for reading in book.usage.rows():
        if pools is not None and reading.pool not in pools:
            continue
        if not weekly(reading.window):
            continue
        rows = book.db.execute('''SELECT read_at,used,resets_at,group_name,bucket FROM usage_samples
            WHERE pool=? AND window=? AND read_at>=? ORDER BY read_at''',
            (reading.pool, reading.window,
             (now - LOOKBACK - timedelta(seconds=MAX_AGE)).astimezone(timezone.utc).isoformat())).fetchall()
        forecast = estimate(reading, rows, now)
        rank = lambda item: (not item.warning, item.spent_at)
        if forecast is not None and (reading.pool not in found or rank(forecast) < rank(found[reading.pool])):
            found[reading.pool] = forecast
    return [found[pool] for pool in sorted(found)]


def readings(ship, now=None):
    from .ledger import Ledger
    try:
        book = Ledger.reading(ship.runtime / 'ledger.sqlite')
        if book is None:
            return []
        with book:
            return collect(book, now or post.now(),
                           {pool.name for pool in ship.pools.values() if pool.kind == 'subscription'})
    except (OSError, sqlite3.Error):
        return []  # unavailable history proves no rate


def notify(service):
    """Save fixed report intents before publishing; retain deduplication until reset."""
    from .compaction_alert import publish
    runtime = service.ship.runtime
    target = runtime / 'pool-forecast-alerts.json'
    try:
        journal = json.loads(target.read_text())
    except FileNotFoundError:
        journal = dict(version=1, periods=[])
    except (OSError, ValueError) as error:
        raise ShipError('pool forecast alarm journal unreadable') from error
    if (not isinstance(journal, dict) or journal.get('version') != 1
            or not isinstance(journal.get('periods'), list)):
        raise ShipError('pool forecast alarm journal unsupported')
    now = post.now()
    for period in journal['periods']:
        if (not isinstance(period, dict) or not isinstance(period.get('pool'), str)
                or parse(period.get('reset')) is None or not isinstance(period.get('reports'), list)):
            raise ShipError('pool forecast alarm period unsupported')
        for intent in period['reports']:
            message = post.Message.from_json(intent)
            if message.kind != 'report' or message.sender != post.SHIP or message.re is not None:
                raise ShipError('pool forecast alarm intent unsupported')
    # Publish saved alarms even after the reading cleared or the service restarted.
    recipients = sorted(spec.name for spec in service.ship.seats.values() if spec.role == 'systems')
    known = [(period['pool'], parse(period['reset'])) for period in journal['periods']]
    for forecast in readings(service.ship, now):
        if (not forecast.warning or not recipients
                or any(pool == forecast.pool and same_reset(reset, forecast.resets_at) for pool, reset in known)):
            continue  # one report per pool and week, though each reading names its reset a little differently
        reports, planned = [], set()
        for name in recipients:
            while True:
                message = service.store.prepare('report', post.SHIP, name, forecast.describe(detail=True))
                if message.short not in planned:
                    break
            reports.append(message.to_json())
            planned.add(message.short)
        journal['periods'].append(dict(pool=forecast.pool, reset=forecast.resets_at.isoformat(), reports=reports))
        known.append((forecast.pool, forecast.resets_at))
    if not journal['periods']:
        return
    text = json.dumps(journal, sort_keys=True) + '\n'
    if not target.exists() or target.read_text() != text:
        durable.replace_file(target, text)
    for period in journal['periods']:
        for intent in period['reports']:
            message = post.Message.from_json(intent)
            with post.admission(runtime, message.to), post.fixed_report(service.store, message):
                publish(runtime, intent)
    # Expired identities can retire after their reports have reached the immutable store.
    remaining = [period for period in journal['periods'] if parse(period['reset']) > now]
    if remaining != journal['periods']:
        durable.replace_file(target, json.dumps(dict(version=1, periods=remaining), sort_keys=True) + '\n')
