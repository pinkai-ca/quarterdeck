"""Seat cards: every seat of a ship on one page, one card each.

    .venv/bin/python dashboard/seat_cards.py --ship ships/default > seats.html

The page is a single HTML file with nothing to fetch, so it opens from disk. It reads `qd --json status`
and the ship file, and writes nothing but the page to standard output. A card shows a seat's name, role,
program, model and effort, what it is doing, whether its door is open, the subject of the order it holds
and today's tokens, and keeps the reading's register: what the reading could not see reads as unknown or
carries its `+`, never as a measured zero, and a value taken from the ship file reads as declared.

Order subjects are the crew's own free text and may name anything the ship works on. Escaping keeps them
from breaking the page, not private: read a page before you share it, as you would any of the ship's text.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import subprocess
import sys
from pathlib import Path

from quarterdeck import ship as shipmod


def status(folder: Path) -> dict:
    """The ship's status reading as `qd --json status` prints it."""
    beside = Path(sys.executable).with_name("qd")
    program = str(beside) if beside.exists() else "qd"
    env = dict(os.environ, QD_SHIP=str(folder))
    env.pop("QD_SEAT", None)
    done = subprocess.run([program, "--json", "status"], capture_output=True, text=True, env=env, check=True)
    return json.loads(done.stdout)


def tokens(n: int | None) -> str:
    if n is None:
        return "unknown"
    for size, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= size:
            return f"{n / size:.1f}".rstrip("0").rstrip(".") + unit
    return str(n)


def today(reading: dict) -> tuple[str, str]:
    """Today's tokens as the text reading prints them: the measured count, with + when some turns were not measured."""
    count = reading.get("today")
    if not isinstance(count, dict) or count.get("known") is None:
        return "unknown", "no token count in the reading"
    text = tokens(count["known"]) + ("+" if count.get("unmeasured") else "")
    return text, "some turns were not measured" if count.get("unmeasured") else "every turn measured"


STATES = {"working": "working", "idle": "idle", "stalled": "attention", "off": "down", "unknown": "unknown"}


def state_of(reading: dict | None, needs_human: set[str]) -> str:
    if reading is None:
        return "unknown"
    if reading["name"] in needs_human:
        return "attention"
    return STATES.get((reading.get("activity") or {}).get("state"), "unknown")


def card(spec: shipmod.SeatSpec, reading: dict | None, subjects: dict[str, str], needs_human: set[str]) -> str:
    """One seat's card. A seat the reading does not hold still has its card, saying so."""
    r = reading or {}
    pool = r.get("pool") or spec.pools[0]
    chosen = spec.for_pool(pool) if pool in spec.pools else spec
    observed = r.get("model")
    model = observed or (f"{chosen.model} (declared)" if chosen.model else "model unknown")
    engine = f"{r.get('harness') or chosen.harness} · {model}"
    if reading is None:
        activity = "unknown, not in the status reading"
    else:
        a = r.get("activity") or {}
        activity = f"{a.get('state')}, {a['detail']}" if a.get("state") and a.get("detail") else (a.get("state") or "unknown")
    door = r.get("door")
    door_text = ("door unknown" if not isinstance(door, dict) else
                 "door open" if door.get("open") else (door.get("reason") or "door closed"))
    subject = subjects.get(r.get("order") or "")
    version = r.get("version")
    count, count_title = today(r)
    esc = html.escape
    return (
        f'<article class="seat" data-state="{state_of(reading, needs_human)}">'
        f'<header><h2>{esc(spec.display)}</h2><span class="role">{esc(spec.role)}</span></header>'
        f'<p class="engine" title="pool {esc(pool)}">{esc(engine)}{" · on fallback" if pool != spec.pools[0] else ""}</p>'
        f'<p class="line"><i class="dot"></i><span>{esc(activity)}</span><small>{esc(door_text)}</small></p>'
        + (f'<p class="order">{esc(subject)}</p>' if subject else "")
        + f'<footer><span title="{esc(count_title)}"><small>today</small><b>{esc(count)}</b></span>'
        f'<span><small>pool</small><b>{esc(pool)}</b></span>'
        + (f'<span title="from the ship file, not read from the seat"><small>effort (declared)</small><b>{esc(chosen.effort)}</b></span>'
           if chosen.effort else "")
        + (f'<span><small>program</small><b>{esc(version)}</b></span>' if version else "")
        + "</footer></article>"
    )


STYLE = """
:root{--void:#04050a;--panel-a:#192532;--panel-b:#101722;--edge:#344557;--ink:#e6edf6;--muted:#8797ae;--dim:#63708a;
--mint:#82d8c7;--warn:#f5b73f;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
--sans:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
@media (prefers-color-scheme:light){:root{--void:#f3f5f9;--panel-a:#ffffff;--panel-b:#eef2f7;--edge:#c9d3e0;--ink:#16202c;
--muted:#4f5f74;--dim:#7a879a;--mint:#178a73;--warn:#b7791f}}
*{box-sizing:border-box}
body{margin:0;background:var(--void);color:var(--ink);font:14px/1.45 var(--sans);padding:24px 16px}
main{max-width:1240px;margin:0 auto}
.top{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;gap:8px 24px;margin-bottom:20px}
.top h1{margin:0;font:600 13px var(--mono);letter-spacing:.28em;text-transform:uppercase;color:var(--muted)}
.top p{margin:0;font:12px var(--mono);color:var(--dim)}
section{margin:0 0 22px}
section h3{margin:0 0 10px;font:600 11px var(--mono);letter-spacing:.2em;text-transform:uppercase;color:var(--muted)}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:12px}
.seat{background:linear-gradient(145deg,var(--panel-a),var(--panel-b) 78%);border:1px solid var(--edge);border-radius:9px;
padding:12px 13px 10px;box-shadow:0 5px 20px #0002;min-width:0}
.seat[data-state=working]{border-color:color-mix(in srgb,var(--mint) 55%,var(--edge))}
.seat[data-state=attention]{border-color:var(--warn)}
.seat header{display:flex;align-items:baseline;justify-content:space-between;gap:8px;min-width:0}
.seat h2{margin:0;font:600 16px var(--sans);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.role{font:10px var(--mono);letter-spacing:.14em;text-transform:uppercase;color:var(--muted);white-space:nowrap}
.engine{margin:3px 0 0;font:11px var(--mono);color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.line{display:flex;align-items:center;gap:7px;margin:9px 0 0;font:12px var(--mono);color:var(--mint)}
.line small{margin-left:auto;font-size:11px;color:var(--dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dot{display:block;width:7px;height:7px;border-radius:50%;background:var(--dim);flex-shrink:0}
[data-state=working] .dot{background:var(--mint);box-shadow:0 0 0 3px color-mix(in srgb,var(--mint) 25%,transparent)}
[data-state=attention] .dot,[data-state=unknown] .dot{background:var(--warn)}
.seat[data-state=unknown]{border-style:dashed}
[data-state=idle] .line,[data-state=down] .line{color:var(--muted)}
[data-state=unknown] .line{color:var(--warn)}
.order{margin:8px 0 0;font-size:13px;color:var(--ink);overflow-wrap:anywhere}
.seat footer{display:flex;flex-wrap:wrap;gap:4px 16px;margin-top:10px;padding-top:8px;border-top:1px solid var(--edge)}
.seat footer span{display:flex;gap:6px;align-items:baseline;font:11px var(--mono)}
.seat footer small{font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--dim)}
.seat footer b{font-weight:600;color:var(--ink)}
@media (prefers-reduced-motion:no-preference){[data-state=working] .dot{animation:pulse 2s ease-in-out infinite}}
@keyframes pulse{50%{opacity:.45}}
"""


def page(ship: shipmod.Ship, reading: dict, at: dt.datetime | None = None) -> str:
    at = at or dt.datetime.now().astimezone()
    by_name = {r["name"]: r for r in reading.get("seats", [])}
    subjects = {m["id"]: m["subject"] for m in reading.get("open_orders", []) if m.get("subject")}
    needs_human = set((reading.get("facts") or {}).get("needs_human", []))
    sections = []
    for session_name, session in ship.sessions.items():
        seats = [s for s in ship.seats.values() if s.session == session_name]
        if not seats:
            continue
        cards = "".join(card(s, by_name.get(s.name), subjects, needs_human) for s in seats)
        sections.append(f'<section><h3>{html.escape(session.title or session_name)}</h3><div class="cards">{cards}</div></section>')
    title = f"{ship.name} seats"
    summary = (f"service {html.escape(str(reading.get('service', 'unknown')))} · "
               f"{len(reading.get('open_orders', []))} open orders · read {at.strftime('%Y-%m-%d %H:%M %Z')}")
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>'
            f'<style>{STYLE}</style></head><body><main><div class="top"><h1>{html.escape(ship.name)}</h1><p>{summary}</p></div>'
            f'{"".join(sections)}</main></body></html>\n')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="every seat of a ship on one page, one card each")
    parser.add_argument("--ship", help="the ship folder (default: $QD_SHIP)")
    args = parser.parse_args(argv)
    ship = shipmod.load(shipmod.find(args.ship))
    sys.stdout.write(page(ship, status(ship.folder)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
