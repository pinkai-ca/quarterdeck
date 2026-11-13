"""Claude's default footer, run as a standalone stdlib script with -I -S.

Reads ship.toml, Claude's stdin facts and the ship's ordinary JSON readings.
Only its disposable cache is written, under runtime/statusline. Provider knowledge
stays with the ledger: no provider requests or model/window-name inference here.
"""

import fcntl
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import tomllib
from datetime import datetime

LEDGER_TTL = 30      # seconds a reading of the allowances is shown before another is asked for
SEATS_TTL = 120      # seconds before asking again which subscription each seat is on
MARGIN = 4           # columns Claude Code keeps for itself beside the line
BAND = 260           # the widest the line is drawn: past this, spreading only pulls the figures apart
BAR = 8              # cells in an allowance's bar
OLD = 240            # seconds after which a reading the background could not renew is marked as old
WARN = 75

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
PINK, DIM, FAINT, BLUE, AMBER, RED = (f"\x1b[38;5;{n}m" for n in (211, 245, 239, 111, 215, 203))
ANSI = re.compile(r"\x1b\[[0-9;]*m")
SEP = f"{DIM} · {RESET}"


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path: Path, value: dict) -> None:
    scratch = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        with open(os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as handle:
            json.dump(value, handle)
        os.replace(scratch, path)
    finally:
        scratch.unlink(missing_ok=True)


def qd_reading(ship_dir: Path, verb: str) -> dict | None:
    # A plain install and an editable checkout both expose qd beside this Python.
    # Let that entry point select the ship's own copy by its normal handoff rules.
    beside = Path(sys.executable).parent / "qd"
    qd = str(beside) if beside.is_file() else shutil.which("qd")
    if not qd:
        return None
    try:
        done = subprocess.run([qd, "--ship", str(ship_dir), "--json", verb],
                              capture_output=True, text=True, timeout=45)
        value = json.loads(done.stdout) if done.returncode == 0 else None
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def refresh(ship_dir: Path, path: Path) -> None:
    """One background reader at a time; process exit releases its lock."""
    with open(os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600), "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        cache = read_json(path)
        now = time.time()
        ledger = qd_reading(ship_dir, "ledger")
        cache["ledger_attempted_at"] = now
        cache["ledger_unavailable"] = not bool(ledger and ledger.get("available") is True)
        if not cache["ledger_unavailable"]:
            cache["windows"] = [
                {"pool": w.get("pool"), "wall": bool(w.get("wall")), "current": bool(w.get("current")),
                 "complete": w.get("complete"),
                 **{k: (w.get("reading") or {}).get(k) for k in ("window", "used", "resets_at", "blocks", "group")}}
                for w in ledger.get("windows") or []]
            cache["pools"] = {p["name"]: p.get("kind") for p in ledger.get("pools") or []}
            cache["ledger_at"] = now
        if now - cache.get("seats_attempted_at", 0) > SEATS_TTL:
            status = qd_reading(ship_dir, "status")
            cache["seats_attempted_at"] = now
            cache["status_unavailable"] = status is None
            if status is not None:
                cache["seats"] = {s["name"]: s.get("pool") for s in status.get("seats") or []}
                cache["seats_at"] = now
        # Cache contains only the display facts, never whole status/ledger records.
        write_json(path, cache)


def renew_in_background(path: Path) -> None:
    try:
        with open(os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600), "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
        subprocess.Popen([sys.executable, "-I", "-S", str(Path(__file__).absolute()), "--refresh"],
                         start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def ship_file(ship_dir: Path) -> dict:
    try:
        return tomllib.loads((ship_dir / "ship.toml").read_text())
    except (OSError, ValueError):
        return {}


def seconds_until(moment) -> float | None:
    try:
        at = datetime.fromisoformat(str(moment).replace("Z", "+00:00"))
        return at.timestamp() - time.time() if at.tzinfo is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def left(seconds: float) -> str:
    minutes = max(0, int(seconds // 60))
    days, hours = minutes // 1440, minutes % 1440 // 60
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    return f"{hours}h" if hours else f"{minutes}m"


def readable(window: dict) -> bool:
    used = window.get("used")
    return (window.get("complete") is True and type(used) in (int, float)
            and math.isfinite(used) and 0 <= used <= 1
            and seconds_until(window.get("resets_at")) is not None)


def allowances(cache: dict, ship: dict, own_pool: str | None) -> list[dict]:
    """One bar per declared subscription. The tightest blocking window leads.

    All window names and scopes come from the ledger. A partial collection stays
    visibly partial, even when its known window happens to read zero.
    """
    rows = []
    for name, spec in (ship.get("pools") or {}).items():
        kind = (cache.get("pools") or {}).get(name)
        if kind in ("key", "endpoint") or "endpoint" in spec or "api_key" in spec:
            continue
        windows = [w for w in cache.get("windows") or [] if w.get("pool") == name]
        known = [w for w in windows if readable(w)]
        blocking = [w for w in known if w.get("blocks") is not False]
        top = max(blocking or known, key=lambda w: (bool(w.get("wall")), w["used"]), default=None)
        rows.append({"pool": name, "own": name == own_pool,
                     "used": top["used"] * 100 if top else None,
                     "span": " / ".join(str(top[k]) for k in ("group", "window") if top.get(k)) if top else "",
                     "resets": seconds_until(top.get("resets_at")) if top else None,
                     "wall": any(w.get("wall") for w in windows),
                     "unknown": len(known) < len(windows),
                     "stale": bool(top) and (not top.get("current") or bool(cache.get("ledger_unavailable"))
                                             or time.time() - cache.get("ledger_at", 0) > OLD)})
    return rows


def width(text: str) -> int:
    return len(ANSI.sub("", text))


def tone(used: float | None, wall: bool = False) -> str:
    if wall or (used is not None and used >= 100):
        return RED
    return AMBER if used is not None and used >= WARN else BLUE


def percent(used: float) -> str:
    return f"{used:.1f}%" if 0 < used < 1 else f"{used:.0f}%"


def cell(row: dict, detail: int) -> str:
    mark = f"{PINK}●{RESET} " if row["own"] else ""
    name = f"{BOLD if row['own'] else ''}{row['pool']}{RESET}"
    if row["used"] is None:
        return f"{mark}{name} {DIM}unknown{RESET}" + (f" {RED}WALL{RESET}" if row["wall"] else "")
    hue = tone(row["used"], row["wall"])
    bar = ""
    if detail >= 2:
        filled = min(BAR, max(1 if row["used"] >= 1 else 0, round(row["used"] / 100 * BAR)))
        bar = f"{hue}" + "━" * filled + f"{FAINT}" + "─" * (BAR - filled) + f"{RESET} "
    figure = f"{hue}{percent(row['used'])}{RESET}"
    if detail >= 1 and row["span"]:
        figure += f"{DIM} {row['span']}{RESET}"
    if row["stale"]:
        figure += f"{DIM} last known{RESET}"
    if row["unknown"]:
        figure += f"{DIM} + unknown{RESET}"
    if row["wall"]:
        figure += f" {RED}WALL{RESET}"
    text = f"{mark}{name} {bar}{figure}"
    if detail >= 3 and row["resets"] is not None:
        text += SEP + f"{DIM}reset {left(row['resets']) if row['resets'] > 0 else 'passed'}{RESET}"
    return text


def identity(seat: dict, name: str, session: dict, detail: int) -> str:
    """Who this session is. detail 2: name, role, model, effort, context; 1: no effort; 0: name, model, context."""
    model = (session.get("model") or {}).get("display_name") or seat.get("model") or ""
    told = session.get("effort")
    effort = (told.get("level") if isinstance(told, dict) else told) or seat.get("effort")
    used = (session.get("context_window") or {}).get("used_percentage")
    persona, role = seat.get("persona") or name.title(), (seat.get("role") or "").replace("-", " ")
    head = f"{BOLD}{PINK}{persona}{RESET}"
    if detail >= 1 and role and role.lower() != persona.lower():
        head += f"  {DIM}{role}{RESET}"
    parts = [head, model] if model else [head]
    if detail >= 2 and effort:
        parts.append(f"{DIM}{effort}{RESET}")
    if isinstance(used, (int, float)):
        hue = RED if used >= 90 else AMBER if used >= 75 else ""
        parts.append(f"{DIM}context{RESET} {hue}{used:.0f}%{RESET}")
    return SEP.join(parts)


def spread(cells: list[str], room: int, least: int = 3, most: int = 8, lead: int = 1) -> str | None:
    """The cells on one row with equal gaps between them, as wide as the room allows; None when they do not fit.
    `lead` makes the first gap that many times the others, to set the first cell apart."""
    if len(cells) < 2:
        return cells[0] if cells and width(cells[0]) <= room else None
    spare = room - sum(width(c) for c in cells)
    gap = min(most, spare // (len(cells) - 2 + lead))
    if gap < least:
        return None
    return cells[0] + " " * (gap * lead) + (" " * gap).join(cells[1:])


def draw(columns: int, seat: dict, name: str, session: dict, rows: list[dict]) -> list[str]:
    """Shed decoration, then wrap: never drop subscriptions to fit the screen."""
    room = max(20, min(columns - MARGIN, BAND))
    who = identity(seat, name, session, 2)
    for detail in (3, 2):
        one = spread([who, *(cell(r, detail) for r in rows)], room, least=3, lead=2)
        if one:
            return [one]
    lines = [who]
    for row in rows:
        text = next((cell(row, d) for d in (3, 2, 1, 0) if width(cell(row, d)) <= room), cell(row, 0))
        if len(lines) > 1 and width(lines[-1]) + 3 + width(text) <= room:
            lines[-1] += "   " + text
        else:
            lines.append(text)
    return lines


def columns_of_terminal() -> int:
    forced = os.environ.get("QD_STATUSLINE_WIDTH") or os.environ.get("COLUMNS", "")
    return int(forced) if forced.isdigit() and int(forced) else 120


def main() -> None:
    folder = os.environ.get("QD_SHIP")
    name = os.environ.get("QD_SEAT", "claude")
    ship_dir = Path(folder) if folder else None
    ship = ship_file(ship_dir) if ship_dir else {}
    session = {}
    if "--refresh" not in sys.argv and not sys.stdin.isatty():
        try:
            value = json.load(sys.stdin)
            session = value if isinstance(value, dict) else {}
        except ValueError:
            pass
    seat = (ship.get("seats") or {}).get(name) or {}
    if not ship:
        if "--refresh" not in sys.argv:
            print(identity(seat, name, session, 2) + SEP + "ship reading unavailable")
        return
    # law: one-folder
    cache_folder = ship_dir / "runtime" / "statusline"
    cache_folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = cache_folder / "readings.json"
    if "--refresh" in sys.argv:
        refresh(ship_dir, path)
        return
    cache = read_json(path)
    now = time.time()
    if "--show" not in sys.argv and (now - cache.get("ledger_attempted_at", 0) > LEDGER_TTL
                                   or now - cache.get("seats_attempted_at", 0) > SEATS_TTL):
        renew_in_background(path)
    own = None if cache.get("status_unavailable") or now - cache.get("seats_at", 0) > OLD else (cache.get("seats") or {}).get(name)
    rows = allowances(cache, ship, own)
    lines = draw(columns_of_terminal(), seat, name, session, rows)
    if not cache.get("ledger_at") or cache.get("ledger_unavailable"):
        lines.append(f"{DIM}ship ledger unavailable{RESET}")
    if own is None:
        lines.append(f"{DIM}seat subscription unknown{RESET}")
    print("\n".join(lines))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(f"{DIM}status line unavailable{RESET}")
