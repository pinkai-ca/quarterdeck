"""Read-only Ledger data and its human tables. No provider reads or schema changes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .ledger import Budget, Ledger, PLUS_NOTE, UNATTRIBUTED, figure
from .provenance import UNKNOWN
from .ship import Ship
from .usage_cache import Reading, parse, skipped_access_read
from . import keyfile, providers


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
            "reason": row.display_reason(now), "observed_at": now,
            "age_seconds": max(0, (now - at).total_seconds()) if at else None}


def uncertainty(book: Ledger) -> dict:
    refusals = book.projection_refusals()
    return {"unknown_pool_turns": book.db.execute("SELECT COUNT(*) FROM turns WHERE pool = ?", (UNKNOWN,)).fetchone()[0],
            "incomplete_records": [dict(zip(("record", "seat", "pool", "error"), row)) for row in book.incomplete_records()],
            "unread_spans": book.unread_spans(), **({"projection_refusals": refusals} if refusals else {})}


def _mib(count: int) -> str:
    return f'{count / (1024 * 1024):.1f} MiB'


def span_note(span: dict) -> str:
    """One unread span, in words: never a figure for the turns it holds (law: every-turn-accounted)."""
    limits = []
    if 'bound' in span:
        earlier = "previously " if 'projection' in span else ""
        limits.append(f"{earlier}{_mib(span['size'])} past its read bound of {_mib(span['bound'])}")
    if projection := span.get('projection', span if 'resource' in span else None):
        limits.append(f"{projection['size']:,} bytes raw, projection {projection['resource']} limit of "
                      f"{projection['limit']:,} {projection['unit']} exceeded")
    return (f"unread span: {Path(span['record']).name} ({span['seat']}, {span['pool']}) · {' · '.join(limits)}, first seen "
            f"{span['passed_at']}; {span['counted']} turns counted before it, its turns since unknown")


def notes(data: dict) -> list[str]:
    unknown = data["unknown_pool_turns"]
    found = ([f"{unknown} turns with unknown pool attribution: missing/ambiguous launch history or turn times, or a turn crossing pools"]
             if unknown else [])
    found.extend(f"unread record: {Path(r['record']).name} ({r['seat']}, {r['pool']}) · {r['error']}"
                 for r in data["incomplete_records"])
    found.extend(span_note(span) for span in data.get("unread_spans", []))
    for row in data.get('projection_refusals', []):
        path = '.'.join(row['path']) or 'row'
        read = ' · read by compatibility reader' if row['compatibility'] and row['exhausted'] is None else ''
        detail = f" · {row['layout']} · {row['reason']} ({row['value_type']})" if 'layout' in row else ''
        if 'content_is_list' in row:
            content = 'list' if row['content_is_list'] else 'non-list'
            detail += (f" · row {row['row_type']}, message role {row['message_role']}, "
                       f"block {row['block_type']}, content {content}")
        found.append(f"stream row limit: {Path(row['record']).name} · {path} ({row['size']:,} encoded bytes){read}{detail}")
    return found


def collect(book: Ledger, ship: Ship | None = None, *, by: str | None = None, tools: bool = False) -> dict:
    data = {"available": True, "reason": None, "by": by, "kind": "tools" if tools else "rollup" if by else "pools",
            **uncertainty(book)}
    if book.catching_up():
        data['accounting_current'] = False
        data['accounting_note'] = 'accounting catching up: historical figures and key spend are last known'
    if ship is not None:
        data.update(providers.identities(ship))
    if tools:
        return {**data, "rows": [ToolRow(*r) for r in book.tools()]}
    if by:
        return {**data, "rows": book.rollup(by), **({"attribution": book.attributions.details()} if by == "order" else {})}
    now = datetime.now().astimezone()
    walls = book.walls(ship, now)
    pools = []
    for pool in (ship.pools.values() if ship else ()):
        item = providers.pool_info(pool)
        if pool.kind == "key":
            budget = Budget.parse(pool.budget)
            since = budget.period_start(now) if budget else now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            spent, unpriced = book.key_spend(ship, pool, since)
            item.update(budget=budget, spent=spent, unpriced=unpriced, since=since, wall=f"{pool.name} budget" in walls)
        elif pool.kind == "endpoint":
            item.update(endpoint=pool.endpoint)
        if unread := sum(1 for span in data["unread_spans"] if span["pool"] == pool.name):
            item["unread_spans"] = unread  # only where there is one: a pool's ordinary reading is unchanged
        pools.append(item)
    windows = []
    for row in book.usage.rows():
        item = window_reading(row, now)
        item["wall"] = any(w == row.label or w.startswith(row.label + " ·") for w in walls)
        windows.append(item)
    from .usage_forecast import collect as forecasts
    if ship is not None and (spends := key_spends(ship, book, now)):
        data["key_spends"] = spends
    return {**data, "observed_at": now, "windows": windows, "pools": pools,
            "usage_forecasts": forecasts(book, now, {p.name for p in ship.pools.values() if p.kind == 'subscription'}
                                        if ship is not None else None),
            "usage_absent_reason": "no usage read yet: the service reads active subscription pools in the background"
            if not windows and not any(p["kind"] in ("key", "endpoint") for p in pools) else None,
            "usage_attempts": [dict(zip(("pool", "at", "error"), row)) for row in
                               book.db.execute("SELECT pool,at,error FROM usage_reads ORDER BY pool")],
            "wall_events": [{**{k: v for k, v in item.items() if k != "wall"},
                             "harness": item["wall"].harness, "model": item["wall"].model,
                             "group": item["wall"].group, "all_models": item["wall"].all_models}
                            for item in book.quota.events(now, ship=ship)],
            "walls": walls, "observed_walls": book.quota.walls(now, ship=ship)}


def missing(*, by: str | None = None, tools: bool = False, ship: Ship | None = None) -> dict:
    return {"available": False, "reason": "no ledger yet: the service writes it once the ship is up",
            "kind": "tools" if tools else "rollup" if by else "pools", "by": by,
            "rows": [], "windows": [], **(providers.identities(ship) if ship else {"pools": [], "harnesses": []})}


def render(data: dict) -> list[str]:
    if not data["available"]:
        return [data["reason"]]
    if data["kind"] == "rollup":
        lines = render_rollup(data)
    elif data["kind"] == "tools":
        lines = render_tools(data)
    else:
        lines = render_pools(data)
    if data["kind"] != "pools":
        lines += [f"pool {p['name']} · provider {p['provider_name']} · {p['kind']} · native harness {p['native_harness'] or 'none'}"
                  for p in data.get("pools", [])]
    return ([data['accounting_note']] if data.get('accounting_note') else []) + lines + notes(data)


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
            if item['status'] in ('shared', 'unresolved', 'pending', 'unassociated') or any(e.get('retained') for e in item.get('evidence', [])):
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


def key_spends(ship, book, now: datetime) -> list[str]:
    """One line for each key the ship's pools name: what it has spent, and what is left of a declared budget."""
    named: dict[str, list] = {}
    for pool in ship.pools.values():
        if pool.paid_by_key:
            named.setdefault(keyfile.name_of(pool.api_key) or pool.name, []).append(pool)
    lines = []
    for name, pools in named.items():
        budget = next((parsed for pool in pools if (parsed := Budget.parse(pool.budget))), None)
        since = budget.period_start(now) if budget else now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        spent, unpriced = book.key_spend(ship, pools[0], since)
        currency = budget.currency if budget else "USD"
        stated = f"{figure(spent, unpriced, '.2f')} {currency} spent" if spent is not None else "no cost stated"
        line = f"key {name}: {stated} since {since.date()}"
        if budget:
            line += f" of {budget.shown}"
            if spent is not None:
                line += f", {max(budget.amount - spent, 0):.2f} left"
            if budget.ended(now):
                line += ", ended"
        if unpriced:
            line += f" ({unpriced} turns with no price)"
        lines.append(line)
    return lines


def render_pools(data: dict) -> list[str]:
    lines = [f"pool {p['name']} · provider {p['provider_name']} · {p['kind']} · native harness {p['native_harness'] or 'none'}"
             + (f" · {p['unread_spans']} unread span{'s' if p['unread_spans'] != 1 else ''}: turns unknown"
                if p.get('unread_spans') else "")
             for p in data["pools"]]
    lines.append("pool window · used · freshness · reset · reading time (age)")
    for window in data["windows"]:
        lines.append(window["reading"].describe(window["observed_at"], inspect_command=None) + ("  WALL" if window["wall"] else ""))
    lines += [forecast.describe(detail=True) for forecast in data.get('usage_forecasts', [])]
    for pool in data["pools"]:
        if pool["kind"] != "key":
            continue
        budget, spent, unpriced = pool["budget"], pool["spent"], pool["unpriced"]
        stated = f"{figure(spent, unpriced, '.2f')} {budget.currency if budget else ''}".strip() if spent is not None else "no cost stated"
        note = f" ({unpriced} turns with no price: the program states none and no table is carried)" if unpriced else ""
        limit = f" of {budget.shown}" if budget else ""
        wall = "  WALL" if pool["wall"] else ""
        lines.append(f"{pool['name']:<20} {'spend':<8} {stated}{limit}{note}{wall}")
    for pool in data["pools"]:
        if pool["kind"] == "endpoint":
            lines.append(f"{pool['name']:<20} {'endpoint':<8} {pool['endpoint']} · no quota, no wall")
    lines += data.get("key_spends", [])
    if data["usage_absent_reason"]:
        lines.append(data["usage_absent_reason"])
    for attempt in data["usage_attempts"]:
        if skipped := skipped_access_read(attempt["error"]):
            lines.append(f"{attempt['pool']} latest usage attempt · {skipped}")
        elif attempt["error"]:
            lines.append(f"{attempt['pool']} latest usage read: {attempt['error']}")
    return lines + ["wall: " + wall for wall in data["observed_walls"]]
