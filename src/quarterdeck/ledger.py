"""The Ledger: what the crew spends, by pool, by seat and by order.

One SQLite file in the runtime. Turns come from each harness's own record and are
ingested idempotently, a turn read twice counted once (law: every-turn-accounted). Each
turn carries its tokens in the provider's classes, the tool calls it made, its duration,
the machine it used, and the order it is charged to: the one the seat claimed with an ack,
else its only open order, else unattributed and counted as such. Pools carry the provider's
utilization windows and their walls; a key pool carries its spend against its budget.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .harness.base import Turn, Usage
from .post import Store
from .ship import Ship, ShipError

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
    PRIMARY KEY (record, turn)
);
CREATE TABLE IF NOT EXISTS tool_calls (
    record TEXT NOT NULL, turn INTEGER NOT NULL, name TEXT NOT NULL, seconds REAL, failed INTEGER
);
CREATE TABLE IF NOT EXISTS pools (
    pool TEXT NOT NULL, window TEXT NOT NULL, used REAL, resets_at TEXT, read_at TEXT,
    PRIMARY KEY (pool, window)
);
"""


@dataclass(frozen=True)
class Machine:
    """What a turn cost the machine, from its seat's process tree."""

    cpu_seconds: float = 0.0
    peak_rss: int = 0
    max_processes: int = 0
    write_bytes: int = 0


@dataclass(frozen=True)
class Row:
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


class Ledger:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # -- turns -----------------------------------------------------------------------------

    def ingested(self, record: Path) -> int:
        """How many turns of this record are already in the Ledger."""
        return self.db.execute("SELECT COUNT(*) FROM turns WHERE record = ?", (str(record),)).fetchone()[0]

    def ingest(self, seat: str, pool: str, record: Path, turns: list[Turn], store: Store,
               machine: dict[int, Machine] | None = None) -> int:
        """Write the turns of a record not yet written; the count written. Reading twice counts once."""
        known = self.ingested(record)
        fresh = [t for t in turns if t.index >= known]
        for turn in fresh:
            used = (machine or {}).get(turn.index, Machine())
            self.db.execute(
                "INSERT OR REPLACE INTO turns VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(record), turn.index, seat, pool, attribution(store, seat, turn), turn.started.astimezone().isoformat(),
                 turn.ended.astimezone().isoformat(), turn.seconds, turn.model, turn.tokens.read_cached, turn.tokens.read_uncached,
                 turn.tokens.cache_written, turn.tokens.written, turn.cost, len(turn.tools),
                 sum(t.failed for t in turn.tools), turn.subagent_turns, used.cpu_seconds, used.peak_rss,
                 used.max_processes, used.write_bytes),
            )
            self.db.execute("DELETE FROM tool_calls WHERE record = ? AND turn = ?", (str(record), turn.index))
            self.db.executemany("INSERT INTO tool_calls VALUES (?,?,?,?,?)",
                                [(str(record), turn.index, t.name, t.seconds, int(t.failed)) for t in turn.tools])
        self.db.commit()
        return len(fresh)

    def rollup(self, by: str) -> list[Row]:
        """Turns summed by ``seat``, ``order`` (order_id) or ``pool``."""
        column = {"seat": "seat", "order": "order_id", "pool": "pool"}[by]
        rows = self.db.execute(
            f"SELECT {column}, COUNT(*), SUM(read_cached), SUM(read_uncached), SUM(cache_written), SUM(written), "
            f"SUM(cost), SUM(tool_calls), SUM(tool_failures), SUM(seconds) / 60.0, SUM(cpu_seconds), SUM(write_bytes) "
            f"FROM turns GROUP BY {column} ORDER BY {column}"
        ).fetchall()
        return [Row(*(r[0], r[1], r[2] or 0, r[3] or 0, r[4] or 0, r[5] or 0, r[6], r[7] or 0, r[8] or 0,
                      r[9] or 0.0, r[10] or 0.0, r[11] or 0)) for r in rows]

    def today(self, seat: str, day: str) -> int:
        """Tokens of every class a seat used in turns that ended on ``day``, a local ISO date; turns are stored in local time."""
        row = self.db.execute(
            "SELECT SUM(read_cached + read_uncached + cache_written + written) FROM turns WHERE seat = ? AND substr(ended, 1, 10) = ?",
            (seat, day),
        ).fetchone()
        return row[0] or 0

    def spend(self, pool: str, since: datetime) -> tuple[float | None, int]:
        """A pool's stated cost since a moment, and how many of its turns stated no cost."""
        row = self.db.execute("SELECT SUM(cost), SUM(cost IS NULL) FROM turns WHERE pool = ? AND ended >= ?",
                              (pool, since.astimezone().isoformat())).fetchone()
        return row[0], row[1] or 0

    def tools(self, by: str = "name") -> list[tuple[str, int, float, int]]:
        """Tool calls by name: count, total seconds, failures."""
        return self.db.execute(
            "SELECT name, COUNT(*), SUM(COALESCE(seconds, 0)), SUM(failed) FROM tool_calls GROUP BY name ORDER BY COUNT(*) DESC, name"
        ).fetchall()

    # -- pools -----------------------------------------------------------------------------

    def record_usage(self, pool: str, usage: Usage) -> None:
        for window in usage.windows:
            self.db.execute("INSERT OR REPLACE INTO pools VALUES (?,?,?,?,?)",
                            (pool, window.name, window.used, window.resets_at.isoformat() if window.resets_at else None,
                             usage.read_at.isoformat()))
        self.db.commit()

    def pool_windows(self, pool: str | None = None) -> list[tuple[str, str, float, str | None, str]]:
        query = "SELECT pool, window, used, resets_at, read_at FROM pools" + (" WHERE pool = ?" if pool else "") + " ORDER BY pool, window"
        return self.db.execute(query, (pool,) if pool else ()).fetchall()

    def walls(self, ship: Ship | None = None, now: datetime | None = None) -> list[str]:
        """Pools with a window spent and not yet reset, ``claude-primary 7d``, and key pools past their budget."""
        now = now or datetime.now().astimezone()
        found = []
        for pool, window, used, resets_at, _ in self.pool_windows():
            reset = datetime.fromisoformat(resets_at) if resets_at else None
            if used is not None and used >= 1.0 and (reset is None or reset > now):
                found.append(f"{pool} {window}")
        for pool in (ship.pools.values() if ship else ()):
            budget = Budget.parse(pool.budget)
            if budget is not None:
                spent, _ = self.spend(pool.name, budget.period_start(now))
                if spent is not None and spent >= budget.amount:
                    found.append(f"{pool.name} budget")
        return found

    def walled(self, pool: str, ship: Ship | None = None) -> bool:
        return any(w.split(" ")[0] == pool for w in self.walls(ship))


def attribution(store: Store, seat: str, turn: Turn) -> str:
    """The order a turn is charged to: the one closed by a report sent in the turn, the one claimed
    with an ack, else the seat's only open order at the time; failing all, unattributed."""
    messages = store.all()
    closing = [m for m in messages if m.kind == "report" and m.sender == seat and m.re
               and turn.started <= m.at <= turn.ended]
    if closing:
        return closing[-1].re
    claimed = store.claimed(seat, turn.ended)
    if claimed:
        return claimed
    open_then = [m for m in messages if m.kind == "order" and m.to == seat and m.at <= turn.ended
                 and not any(r.kind == "report" and r.sender == seat and r.re == m.id and r.at <= turn.ended for r in messages)]
    return open_then[0].id if len(open_then) == 1 else UNATTRIBUTED  # law: every-turn-accounted


def table(ledger: Ledger, by: str) -> list[str]:
    """``qd ledger --by seat|order|pool``: tokens, cost, tool calls, minutes, machine load side by side."""
    head = f"{by:<30} {'turns':>5} {'cached':>10} {'uncached':>9} {'cache-w':>9} {'written':>8} {'cost':>8} {'tools':>6} {'fail':>5} {'min':>7} {'cpu-s':>7} {'written-MB':>10}"
    lines = [head]
    for r in ledger.rollup(by):
        cost = f"{r.cost:.2f}" if r.cost is not None else "-"
        lines.append(f"{r.key:<30} {r.turns:>5} {r.read_cached:>10} {r.read_uncached:>9} {r.cache_written:>9} {r.written:>8} "
                     f"{cost:>8} {r.tool_calls:>6} {r.tool_failures:>5} {r.minutes:>7.1f} {r.cpu_seconds:>7.1f} {r.write_bytes / 1e6:>10.1f}")
    if by == "order":
        lines.append(f"unattributed turns are shown as {UNATTRIBUTED!r}")
    return lines


def pools_table(ledger: Ledger, ship: Ship | None = None) -> list[str]:
    lines = [f"{'pool':<20} {'window':<8} {'used':>6} {'resets':<17} read"]
    walls = set(ledger.walls(ship))
    now = datetime.now().astimezone()
    for pool, window, used, resets_at, read_at in ledger.pool_windows():
        reset = datetime.fromisoformat(resets_at).astimezone().strftime("%m-%d %H:%M") if resets_at else "-"
        age = int((now - datetime.fromisoformat(read_at)).total_seconds() // 60)
        wall = "  WALL" if f"{pool} {window}" in walls else ""
        lines.append(f"{pool:<20} {window:<8} {used * 100:>5.0f}% {reset:<17} {age}m ago{wall}")
    for pool in (p for p in (ship.pools.values() if ship else ()) if p.kind == "key"):
        budget = Budget.parse(pool.budget)
        spent, unpriced = ledger.spend(pool.name, budget.period_start(now) if budget else now.replace(day=1, hour=0, minute=0, second=0, microsecond=0))
        stated = f"{spent:.2f} {budget.currency if budget else ''}".strip() if spent is not None else "no cost stated"
        note = f" ({unpriced} turns with no price: the program states none and no table is carried)" if unpriced else ""
        limit = f" of {budget.amount:.0f} {budget.currency}/{budget.period}" if budget else ""
        wall = "  WALL" if f"{pool.name} budget" in walls else ""
        lines.append(f"{pool.name:<20} {'spend':<8} {stated}{limit}{note}{wall}")
    if len(lines) == 1:
        lines.append("no usage read yet: the service reads subscription pools once a minute while a seat runs on them")
    return lines
