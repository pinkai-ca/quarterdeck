"""The Ledger: what the crew spends, by pool, by seat and by order.

One SQLite file in the runtime, written by the service alone and opened read-only by every reading.
Turns come from each harness's own record and are ingested idempotently, a turn read twice counted
once (law: every-turn-accounted). Each turn carries its tokens in the provider's classes, the tool
calls it made, its duration, the machine it used, and the order it is charged to: the one the seat
claimed with an ack, else its only open order, else unattributed and counted as such. What a record
did not hold is not guessed and not hidden: a turn whose usage is missing in part is counted and
marked, and a figure built on such turns carries a trailing ``+``, meaning at least this much. Pools
carry the provider's utilization windows and their walls; a key pool carries its spend against its
budget.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

from .harness.base import Turn, Usage
from .post import Store
from .provenance import Provenance, UNKNOWN
from .quota import Quotas, reset_text
from .ship import Ship, ShipError
from .usage_cache import UsageCache, aware

UNATTRIBUTED = "-"
PERIODS = {"day": 1, "week": 7, "month": 30}


@dataclass(frozen=True)
class Budget:
    """A key pool's wall: ``200 USD/month`` in the ship file."""

    amount: float
    currency: str
    period: str

    @classmethod
    def parse(cls, text: str | None) -> "Budget | None":
        if not text:
            return None
        try:
            figure, period = text.split("/")
            amount, currency = figure.split()
            if period.strip() not in PERIODS:
                raise ValueError(period)
            return cls(float(amount), currency.strip(), period.strip())
        except ValueError:
            raise ShipError(f"a budget reads like \"200 USD/month\" (day, week or month), not {text!r}") from None

    def period_start(self, now: datetime) -> datetime:
        if self.period == "month":
            return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if self.period == "week":
            return (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return now.replace(hour=0, minute=0, second=0, microsecond=0)


SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    record TEXT NOT NULL, turn INTEGER NOT NULL, seat TEXT NOT NULL, pool TEXT NOT NULL, order_id TEXT NOT NULL,
    started TEXT, ended TEXT, seconds REAL, model TEXT,
    read_cached INTEGER, read_uncached INTEGER, cache_written INTEGER, written INTEGER, cost REAL,
    tool_calls INTEGER, tool_failures INTEGER, subagent_turns INTEGER,
    cpu_seconds REAL, peak_rss INTEGER, max_processes INTEGER, write_bytes INTEGER,
    unmeasured INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (record, turn)
);
CREATE TABLE IF NOT EXISTS tool_calls (
    record TEXT NOT NULL, turn INTEGER NOT NULL, name TEXT NOT NULL, seconds REAL, failed INTEGER
);
CREATE TABLE IF NOT EXISTS pools (
    pool TEXT NOT NULL, window TEXT NOT NULL, used REAL, resets_at TEXT, read_at TEXT,
    blocks INTEGER NOT NULL DEFAULT 1, group_name TEXT, bucket TEXT, bucket_name TEXT,
    PRIMARY KEY (pool, window)
);
CREATE TABLE IF NOT EXISTS records (
    record TEXT PRIMARY KEY, seat TEXT NOT NULL, pool TEXT NOT NULL, error TEXT NOT NULL
);
"""
# Columns added since a table was first written; an older file gains them when the service opens it.
ADDED_COLUMNS = {
    "turns": (("unmeasured", "INTEGER NOT NULL DEFAULT 0"),),
    "pools": (("blocks", "INTEGER NOT NULL DEFAULT 1"), ("group_name", "TEXT"), ("bucket", "TEXT"), ("bucket_name", "TEXT")),
}
# A turn whose tokens are not wholly known: a step without usage, or a class an older row left null.
PARTLY_UNMEASURED = "unmeasured > 0 OR read_cached IS NULL OR read_uncached IS NULL OR cache_written IS NULL OR written IS NULL"


@dataclass(frozen=True)
class Machine:
    """What a turn cost the machine, from its seat's process tree."""

    cpu_seconds: float = 0.0
    peak_rss: int = 0
    max_processes: int = 0
    write_bytes: int = 0


@dataclass(frozen=True)
class Count:
    """A figure and how many turns it could not measure; shown as ``1234+`` when some are."""

    known: int = 0
    unmeasured: int = 0

    def __str__(self) -> str:
        return f"{self.known}{'+' if self.unmeasured else ''}"

    def __add__(self, other: "Count") -> "Count":
        return Count(self.known + other.known, self.unmeasured + other.unmeasured)


@dataclass(frozen=True)
class Row:
    """One line of a rollup: the sums, and beside them how many turns each sum could not measure."""

    key: str
    turns: int
    read_cached: int
    read_uncached: int
    cache_written: int
    written: int
    cost: float | None
    tool_calls: int
    tool_failures: int
    minutes: float
    cpu_seconds: float
    write_bytes: int
    unmeasured: int = 0  # turns with tokens the record did not hold, and records that could not be read
    untimed: int = 0  # turns without a duration
    unjudged: int = 0  # turns with a tool call whose failure is unknown


def figure(value, flagged: int, form: str = "") -> str:
    """A number as the tables print it, with the ``+`` that says some turns were not measured."""
    return f"{value:{form}}" + ("+" if flagged else "")


class Ledger:
    """The book. The service opens it to write; a reading opens it read-only with ``Ledger.reading``."""

    def __init__(self, path: Path, readonly: bool = False):
        self.path = path
        if readonly:
            self.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            self.provenance = Provenance(self.db, create=False)
            self.quota = Quotas(self.db, create=False)
            self.usage = UsageCache(self.db, create=False)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        for table, columns in ADDED_COLUMNS.items():
            present = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
            for name, kind in columns:
                if name not in present:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
        self.provenance = Provenance(self.db)
        self.quota = Quotas(self.db)
        self.usage = UsageCache(self.db)
        self.db.commit()

    @classmethod
    def reading(cls, path: Path) -> "Ledger | None":
        """The book opened read-only, for a status, a watch or a ledger table; None while the service has not written one."""
        return cls(path, readonly=True) if path.exists() else None

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- turns -----------------------------------------------------------------------------

    def ingested(self, record: Path) -> int:
        """How many turns of this record are already in the Ledger."""
        return self.db.execute("SELECT COUNT(*) FROM turns WHERE record = ?", (str(record),)).fetchone()[0]

    def mark_record(self, record: Path, seat: str, pool: str, error: str | None) -> None:
        """Note a record that could not be read, or clear the note: its seat's figures carry a ``+`` while it stands."""
        if error is None:
            self.db.execute("DELETE FROM records WHERE record = ?", (str(record),))
        else:
            self.db.execute("INSERT OR REPLACE INTO records VALUES (?,?,?,?)", (str(record), seat, pool, error))
        self.db.commit()

    def incomplete_records(self) -> list[tuple[str, str, str, str]]:
        errors = self.db.execute("SELECT record, seat, pool, error FROM records ORDER BY record").fetchall()
        return [(r, s, p, error) for r, seat, pool, error in errors
                for s, p in self.provenance.owners(r, seat, pool)]

    def ingest(self, seat: str, pool: str, record: Path, turns: list[Turn], store: Store,
               machine: dict[int, Machine] | None = None) -> int:
        """Write the turns of a record not yet written, or changed since; the count written. Reading twice counts once."""
        known = {r[1]: r for r in self.db.execute("SELECT * FROM turns WHERE record = ?", (str(record),))}
        changed = 0
        for turn in turns:
            previous = known.get(turn.index)
            owner, charged_pool = (previous[2:4] if previous and previous[3] != UNKNOWN else
                                   self.provenance.resolve(record, turn.started, turn.ended, seat, pool))
            if previous:
                owner = previous[2]  # an unknown pool does not undo an established seat identity
            used = Machine(*previous[17:21]) if previous else (machine or {}).get(turn.index, Machine())
            started = turn.started.astimezone().isoformat() if turn.started else None
            ended = turn.ended.astimezone().isoformat() if turn.ended else None
            failures = sum(bool(t.failed) for t in turn.tools) if all(t.failed is not None for t in turn.tools) else None
            row = (str(record), turn.index, owner, charged_pool, attribution(store, owner, turn), started, ended,
                   turn.seconds, turn.model, turn.tokens.read_cached, turn.tokens.read_uncached,
                   turn.tokens.cache_written, turn.tokens.written, turn.cost, len(turn.tools), failures,
                   turn.subagent_turns, used.cpu_seconds, used.peak_rss, used.max_processes, used.write_bytes,
                   turn.tokens.unmeasured)
            tool_rows = [(str(record), turn.index, t.name, t.seconds, t.failed) for t in turn.tools]
            previous_tools = self.db.execute("SELECT * FROM tool_calls WHERE record = ? AND turn = ? ORDER BY rowid",
                                             (str(record), turn.index)).fetchall()
            if previous == row and previous_tools == tool_rows:
                continue
            changed += 1
            self.db.execute("INSERT OR REPLACE INTO turns VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
            self.db.execute("DELETE FROM tool_calls WHERE record = ? AND turn = ?", (str(record), turn.index))
            self.db.executemany("INSERT INTO tool_calls VALUES (?,?,?,?,?)", tool_rows)
        self.db.commit()
        return changed

    def rollup(self, by: str) -> list[Row]:
        """Turns summed by ``seat``, ``order`` (order_id) or ``pool``, each sum with the count of turns it could not measure."""
        column = {"seat": "seat", "order": "order_id", "pool": "pool"}[by]
        rows = self.db.execute(
            f"SELECT {column}, COUNT(*), SUM(COALESCE(read_cached, 0)), SUM(COALESCE(read_uncached, 0)), "
            f"SUM(COALESCE(cache_written, 0)), SUM(COALESCE(written, 0)), SUM(cost), SUM(tool_calls), "
            f"SUM(COALESCE(tool_failures, 0)), SUM(COALESCE(seconds, 0)) / 60.0, SUM(cpu_seconds), SUM(write_bytes), "
            f"SUM(CASE WHEN {PARTLY_UNMEASURED} THEN 1 ELSE 0 END), SUM(seconds IS NULL), SUM(tool_failures IS NULL) "
            f"FROM turns GROUP BY {column} ORDER BY {column}"
        ).fetchall()
        by_key = {r[0]: Row(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10] or 0.0, r[11] or 0,
                            r[12], r[13], r[14]) for r in rows}
        for key in self.affected(by):  # a record that could not be read: its figures are at least what was read
            prev = by_key.get(key) or Row(key, 0, 0, 0, 0, 0, None, 0, 0, 0.0, 0.0, 0)
            by_key[key] = replace(prev, unmeasured=prev.unmeasured + 1, untimed=prev.untimed + 1, unjudged=prev.unjudged + 1)
        return [by_key[k] for k in sorted(by_key)]

    def affected(self, by: str) -> set[str]:
        """The keys of a rollup that an unreadable record touches."""
        incomplete = self.incomplete_records()
        if not incomplete:
            return set()
        if by == "seat":
            return {s for _, s, _, _ in incomplete}
        if by == "pool":
            return {p for _, _, p, _ in incomplete}
        orders = self.db.execute("SELECT DISTINCT order_id FROM turns WHERE record IN (SELECT record FROM records)")
        return {UNATTRIBUTED} | {r[0] for r in orders}

    def today(self, seat: str, day: str) -> Count:
        """Tokens of every class a seat used in turns that ended on ``day``, a local ISO date, and how many of them were not measured."""
        known, unmeasured = self.db.execute(
            "SELECT SUM(COALESCE(read_cached, 0) + COALESCE(read_uncached, 0) + COALESCE(cache_written, 0) + COALESCE(written, 0)), "
            f"SUM(CASE WHEN {PARTLY_UNMEASURED} THEN 1 ELSE 0 END) FROM turns "
            "WHERE seat = ? AND (substr(ended, 1, 10) = ? OR (ended IS NULL AND substr(started, 1, 10) = ?))",
            (seat, day, day),
        ).fetchone()
        unreadable = sum(1 for _, owner, _, _ in self.incomplete_records() if owner == seat)
        return Count(int(known or 0), int(unmeasured or 0) + unreadable)

    def spend(self, pool: str, since: datetime) -> tuple[float | None, int]:
        """A pool's stated cost since a moment, and how many of its turns stated no cost."""
        row = self.db.execute("SELECT SUM(cost), SUM(cost IS NULL) FROM turns WHERE pool = ? AND ended >= ?",
                              (pool, since.astimezone().isoformat())).fetchone()
        return row[0], row[1] or 0

    def tools(self) -> list[tuple[str, int, float, int, int, int]]:
        """Tool calls by name: count, seconds, failures; then how many calls had no duration and how many no verdict."""
        return self.db.execute(
            "SELECT name, COUNT(*), SUM(COALESCE(seconds, 0)), SUM(COALESCE(failed, 0)), SUM(seconds IS NULL), SUM(failed IS NULL) "
            "FROM tool_calls GROUP BY name ORDER BY COUNT(*) DESC, name"
        ).fetchall()

    # -- pools -----------------------------------------------------------------------------

    def record_usage(self, pool: str, usage: Usage, attempted_at: datetime | None = None) -> None:
        attempted_at = attempted_at or (usage.read_at if aware(usage.read_at) else datetime.now().astimezone())
        error = self.usage.record(pool, usage, attempted_at)
        self.quota.usage_read(pool, attempted_at, error)

    def pool_windows(self, pool: str | None = None) -> list[tuple[str, str, float | None, str | None, str | None]]:
        query = "SELECT pool, window, used, resets_at, read_at FROM pools" + (" WHERE pool = ?" if pool else "") + " ORDER BY pool, window"
        return self.db.execute(query, (pool,) if pool else ()).fetchall()

    def walls(self, ship: Ship | None = None, now: datetime | None = None) -> list[str]:
        """Pools with a window spent and not yet reset, ``subscription 7d``, and key pools past their budget."""
        now = now or datetime.now().astimezone()
        found = []
        for reading in self.usage.rows():
            if reading.blocks and reading.current(now) and reading.used >= 1.0:
                found.append(f"{reading.pool} {reading.window}" +
                             (f" · {reset_text(reading.resets_at)}" if reading.group is not None else ""))
        for pool in (ship.pools.values() if ship else ()):
            budget = Budget.parse(pool.budget)
            if budget is not None:
                spent, _ = self.spend(pool.name, budget.period_start(now))
                if spent is not None and spent >= budget.amount:
                    found.append(f"{pool.name} budget")
        return found + self.quota.walls(now)

    def walled(self, pool: str, ship: Ship | None = None) -> bool:
        return any(w.split(" ")[0] == pool for w in self.walls(ship))

    def usage_known(self, pool: str) -> bool:
        rows = [r for r in self.usage.rows(pool) if r.blocks]
        now = datetime.now().astimezone()
        return bool(rows) and all(r.current(now) for r in rows)


def attribution(store: Store, seat: str, turn: Turn) -> str:
    """The order a turn is charged to: the one closed by a report sent in the turn, the one claimed
    with an ack, else the seat's only open order at the time; failing all, unattributed."""
    if turn.ended is None:
        return UNATTRIBUTED  # law: every-turn-accounted
    messages = store.all()
    closing = [m for m in messages if m.kind == "report" and m.sender == seat and m.re
               and turn.started is not None and turn.started <= m.at <= turn.ended]
    if closing:
        return closing[-1].re
    claimed = store.claimed(seat, turn.ended)
    if claimed:
        return claimed
    open_then = [m for m in messages if m.kind == "order" and m.to == seat and m.at <= turn.ended
                 and not any(r.kind == "report" and r.sender == seat and r.re == m.id and r.at <= turn.ended for r in messages)]
    return open_then[0].id if len(open_then) == 1 else UNATTRIBUTED  # law: every-turn-accounted


def incomplete_notes(ledger: Ledger) -> list[str]:
    unknown = ledger.db.execute("SELECT COUNT(*) FROM turns WHERE pool = ?", (UNKNOWN,)).fetchone()[0]
    notes = ([f"{unknown} turns with unknown pool attribution: missing/ambiguous launch history or turn times, or a turn crossing pools"]
             if unknown else [])
    return notes + [f"unread record: {Path(r).name} ({s}, {p}) · {e}" for r, s, p, e in ledger.incomplete_records()]


PLUS_NOTE = "a figure with + has turns beside it that the record did not measure: at least this much"


def table(ledger: Ledger, by: str) -> list[str]:
    """``qd ledger --by seat|order|pool``: tokens, cost, tool calls, minutes, machine load side by side."""
    head = f"{by:<30} {'turns':>5} {'cached':>10} {'uncached':>9} {'cache-w':>9} {'written':>8} {'cost':>8} {'tools':>7} {'fail':>7} {'min':>7} {'cpu-s':>7} {'written-MB':>10}"
    lines = [head]
    rows = ledger.rollup(by)
    for r in rows:
        cost = f"{r.cost:.2f}" if r.cost is not None else "-"
        lines.append(f"{r.key:<30} {r.turns:>5} {figure(r.read_cached, r.unmeasured):>10} {figure(r.read_uncached, r.unmeasured):>9} "
                     f"{figure(r.cache_written, r.unmeasured):>9} {figure(r.written, r.unmeasured):>8} {cost:>8} {r.tool_calls:>7} "
                     f"{figure(r.tool_failures, r.unjudged):>7} {figure(r.minutes, r.untimed, '.1f'):>7} {r.cpu_seconds:>7.1f} {r.write_bytes / 1e6:>10.1f}")
    if by == "order":
        lines.append(f"unattributed turns are shown as {UNATTRIBUTED!r}")
    if any(r.unmeasured or r.untimed or r.unjudged for r in rows):
        lines.append(PLUS_NOTE)
    return lines + incomplete_notes(ledger)


def tools_table(ledger: Ledger) -> list[str]:
    """``qd ledger --tools``: tool calls by name."""
    lines = [f"{'tool':<24} {'calls':>6} {'seconds':>9} {'failed':>6}"]
    rows = ledger.tools()
    for name, count, seconds, failed, untimed, unjudged in rows:
        lines.append(f"{name:<24} {count:>6} {figure(seconds, untimed, '.1f'):>9} {figure(failed, unjudged):>6}")
    if any(untimed or unjudged for *_, untimed, unjudged in rows):
        lines.append(PLUS_NOTE)
    return lines + incomplete_notes(ledger)


def pools_table(ledger: Ledger, ship: Ship | None = None) -> list[str]:
    lines = ["pool window · used · freshness · reset · reading time (age)"]
    walls = set(ledger.walls(ship))
    now = datetime.now().astimezone()
    for reading in ledger.usage.rows():
        wall = "  WALL" if any(w == f"{reading.pool} {reading.window}" or w.startswith(f"{reading.pool} {reading.window} ·") for w in walls) else ""
        lines.append(reading.describe(now) + wall)
    for pool in (p for p in (ship.pools.values() if ship else ()) if p.kind == "key"):
        budget = Budget.parse(pool.budget)
        spent, unpriced = ledger.spend(pool.name, budget.period_start(now) if budget else now.replace(day=1, hour=0, minute=0, second=0, microsecond=0))
        stated = f"{spent:.2f} {budget.currency if budget else ''}".strip() if spent is not None else "no cost stated"
        note = f" ({unpriced} turns with no price: the program states none and no table is carried)" if unpriced else ""
        limit = f" of {budget.amount:.0f} {budget.currency}/{budget.period}" if budget else ""
        wall = "  WALL" if f"{pool.name} budget" in walls else ""
        lines.append(f"{pool.name:<20} {'spend':<8} {stated}{limit}{note}{wall}")
    for pool in (p for p in (ship.pools.values() if ship else ()) if p.kind == "endpoint"):
        lines.append(f"{pool.name:<20} {'endpoint':<8} {pool.endpoint} · no quota, no wall")
    if len(lines) == 1:
        lines.append("no usage read yet: the service reads active subscription pools in the background")
    return lines + ledger.quota.usage_notes() + ["wall: " + w for w in ledger.quota.walls()] + incomplete_notes(ledger)
