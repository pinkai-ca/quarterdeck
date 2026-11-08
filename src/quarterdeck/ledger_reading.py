"""Read-only Ledger data and its human tables. No provider reads or schema changes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .ledger import Budget, Ledger, PLUS_NOTE, UNATTRIBUTED, figure
from .provenance import UNKNOWN
from .ship import Ship
from .usage_cache import Reading, parse


@dataclass(frozen=True)
class ToolRow:
    name: str
    calls: int
    seconds: float
    failed: int
    untimed: int
    unjudged: int


def window_reading(row: Reading, now: datetime) -> dict:
    at = parse(row.read_at)
    return {"pool": row.pool, "reading": row, "complete": row.complete, "current": row.current(now),
            "reason": row.reason(now), "observed_at": now,
            "age_seconds": max(0, (now - at).total_seconds()) if at else None}


def uncertainty(book: Ledger) -> dict:
    return {"unknown_pool_turns": book.db.execute("SELECT COUNT(*) FROM turns WHERE pool = ?", (UNKNOWN,)).fetchone()[0],
            "incomplete_records": [dict(zip(("record", "seat", "pool", "error"), row)) for row in book.incomplete_records()]}


def notes(data: dict) -> list[str]:
    unknown = data["unknown_pool_turns"]
    found = ([f"{unknown} turns with unknown pool attribution: missing/ambiguous launch history or turn times, or a turn crossing pools"]
             if unknown else [])
    return found + [f"unread record: {Path(r['record']).name} ({r['seat']}, {r['pool']}) · {r['error']}"
                    for r in data["incomplete_records"]]


def collect(book: Ledger, ship: Ship | None = None, *, by: str | None = None, tools: bool = False) -> dict:
    data = {"available": True, "reason": None, "by": by, "kind": "tools" if tools else "rollup" if by else "pools",
            **uncertainty(book)}
    if tools:
        return {**data, "rows": [ToolRow(*r) for r in book.tools()]}
    if by:
        return {**data, "rows": book.rollup(by), **({"attribution": book.attributions.details()} if by == "order" else {})}
    now = datetime.now().astimezone()
    walls = book.walls(ship, now)
    pools = []
    for pool in (ship.pools.values() if ship else ()):
        item = {"name": pool.name, "kind": pool.kind, "provider": pool.provider}
        if pool.kind == "key":
            budget = Budget.parse(pool.budget)
            since = budget.period_start(now) if budget else now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            spent, unpriced = book.spend(pool.name, since)
            item.update(budget=budget, spent=spent, unpriced=unpriced, since=since, wall=f"{pool.name} budget" in walls)
        elif pool.kind == "endpoint":
            item.update(endpoint=pool.endpoint)
        pools.append(item)
    windows = []
    for row in book.usage.rows():
        item = window_reading(row, now)
        item["wall"] = any(w == f"{row.pool} {row.window}" or w.startswith(f"{row.pool} {row.window} ·") for w in walls)
        windows.append(item)
    return {**data, "observed_at": now, "windows": windows, "pools": pools,
            "usage_absent_reason": "no usage read yet: the service reads active subscription pools in the background"
            if not windows and not any(p["kind"] in ("key", "endpoint") for p in pools) else None,
            "usage_attempts": [dict(zip(("pool", "at", "error"), row)) for row in
                               book.db.execute("SELECT pool,at,error FROM usage_reads ORDER BY pool")],
            "wall_events": [dict(zip(("seat", "pool", "reason", "resets_at", "observed_at", "source"), row))
                            for row in book.quota.active(now)],
            "walls": walls, "observed_walls": book.quota.walls(now)}


def missing(*, by: str | None = None, tools: bool = False) -> dict:
    return {"available": False, "reason": "no ledger yet: the service writes it once the ship is up",
            "kind": "tools" if tools else "rollup" if by else "pools", "by": by,
            "rows": [], "windows": [], "pools": []}


def render(data: dict) -> list[str]:
    if not data["available"]:
        return [data["reason"]]
    if data["kind"] == "rollup":
        lines = render_rollup(data)
    elif data["kind"] == "tools":
        lines = render_tools(data)
    else:
        lines = render_pools(data)
    return lines + notes(data)


def render_rollup(data: dict) -> list[str]:
    by, rows = data["by"], data["rows"]
    lines = [f"{by:<30} {'turns':>5} {'cached':>10} {'uncached':>9} {'cache-w':>9} {'written':>8} {'cost':>8} {'tools':>7} {'fail':>7} {'min':>7} {'cpu-s':>7} {'written-MB':>10}"]
    for r in rows:
        cost = figure(r.cost, r.unpriced, ".2f") if r.cost is not None else "-"
        lines.append(f"{r.key:<30} {r.turns:>5} {figure(r.read_cached, r.unmeasured):>10} {figure(r.read_uncached, r.unmeasured):>9} "
                     f"{figure(r.cache_written, r.unmeasured):>9} {figure(r.written, r.unmeasured):>8} {cost:>8} {r.tool_calls:>7} "
                     f"{figure(r.tool_failures, r.unjudged):>7} {figure(r.minutes, r.untimed, '.1f'):>7} {r.cpu_seconds:>7.1f} {r.write_bytes / 1e6:>10.1f}")
    if by == "order":
        lines.append(f"{UNATTRIBUTED!r}: no association; [shared]: several changes counted once; [unresolved]: evidence gap; pending: service replay needed")
        for item in data.get("attribution", []):
            if item['status'] in ('shared', 'unresolved', 'pending') or any(e.get('retained') for e in item.get('evidence', [])):
                label = f"{item.get('seat', '?')} turn {item.get('turn', '?')}"
                orders = ', '.join(item.get('orders', [])) or 'none proved'
                lines.append(f"attribution {item['status']}: {label} · {orders} · {item['reason']}")
    if any(r.unmeasured or r.untimed or r.unjudged or (r.unpriced and r.cost is not None) for r in rows):
        lines.append(PLUS_NOTE)
    if any(r.unpriced for r in rows):
        partial = "cost with + is a partial total; " if any(r.unpriced and r.cost is not None for r in rows) else ""
        lines.append(partial + "- means no established cost; unpriced turns are counted in JSON")
    return lines


def render_tools(data: dict) -> list[str]:
    lines = [f"{'tool':<24} {'calls':>6} {'seconds':>9} {'failed':>6}"]
    for r in data["rows"]:
        lines.append(f"{r.name:<24} {r.calls:>6} {figure(r.seconds, r.untimed, '.1f'):>9} {figure(r.failed, r.unjudged):>6}")
    if any(r.untimed or r.unjudged for r in data["rows"]):
        lines.append(PLUS_NOTE)
    return lines


def render_pools(data: dict) -> list[str]:
    lines = ["pool window · used · freshness · reset · reading time (age)"]
    for window in data["windows"]:
        lines.append(window["reading"].describe(window["observed_at"], inspect_command=None) + ("  WALL" if window["wall"] else ""))
    for pool in data["pools"]:
        if pool["kind"] != "key":
            continue
        budget, spent, unpriced = pool["budget"], pool["spent"], pool["unpriced"]
        stated = f"{figure(spent, unpriced, '.2f')} {budget.currency if budget else ''}".strip() if spent is not None else "no cost stated"
        note = f" ({unpriced} turns with no price: the program states none and no table is carried)" if unpriced else ""
        limit = f" of {budget.amount:.0f} {budget.currency}/{budget.period}" if budget else ""
        wall = "  WALL" if pool["wall"] else ""
        lines.append(f"{pool['name']:<20} {'spend':<8} {stated}{limit}{note}{wall}")
    for pool in data["pools"]:
        if pool["kind"] == "endpoint":
            lines.append(f"{pool['name']:<20} {'endpoint':<8} {pool['endpoint']} · no quota, no wall")
    if len(lines) == 1:
        lines.append(data["usage_absent_reason"])
    return lines + [f"{a['pool']} latest usage read: {a['error']}" for a in data["usage_attempts"] if a["error"]] + [
        "wall: " + w for w in data["observed_walls"]]
