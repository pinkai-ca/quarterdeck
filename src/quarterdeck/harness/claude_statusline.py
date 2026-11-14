"""Claude's default footer, run as a standalone stdlib script with -I -S.

Reads ship.toml, Claude's stdin facts, the ship's ordinary JSON readings and
read-only launch-pool metadata from its Ledger.
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
import sqlite3
import struct
import subprocess
import sys
import termios
import time
import tomllib
import unicodedata
from datetime import datetime

LEDGER_TTL = 30      # seconds a reading of the allowances is shown before another is asked for
SEATS_TTL = 120      # seconds before asking again which subscription each seat is on
MARGIN = 4           # columns Claude Code keeps for itself beside the line
BAND = 260           # the widest the line is drawn: past this, spreading only pulls the figures apart
BAR = 8              # cells in an allowance's bar
WHO = 52             # align subscription columns where the identity fits this band
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


def launched_pools(ship_dir: Path) -> list[str] | None:
    """Only pool names from this ship's accounted launches; no records or migrations."""
    source = ship_dir / 'runtime' / 'ledger.sqlite'
    try:
        db = sqlite3.connect(source.absolute().as_uri() + '?mode=ro', uri=True, timeout=.1)
        try:
            return sorted(pool for (pool,) in db.execute('SELECT DISTINCT pool FROM record_launches')
                          if isinstance(pool, str) and pool)
        finally:
            db.close()
    except (OSError, sqlite3.Error):
        return None


def used_pools(cache: dict, ship: dict) -> set[str]:
    """A seat declares the pool, or this ship has a launch on it."""
    found = {pool for pool in cache.get('launched_pools') or [] if isinstance(pool, str)}
    for seat in (ship.get('seats') or {}).values():
        choices = (seat or {}).get('pool', [])
        for choice in choices if isinstance(choices, list) else [choices]:
            pool = choice.get('pool') if isinstance(choice, dict) else choice
            if isinstance(pool, str):
                found.add(pool)
    return found


def refresh(ship_dir: Path, path: Path) -> None:
    """One background reader at a time; process exit releases its lock."""
    with open(os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600), "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        cache = read_json(path)
        history = launched_pools(ship_dir)
        if history is not None:
            cache['launched_pools'] = history
        now = time.time()
        ledger = qd_reading(ship_dir, "ledger")
        cache["ledger_attempted_at"] = now
        cache["ledger_unavailable"] = not bool(ledger and ledger.get("available") is True)
        if not cache["ledger_unavailable"]:
            cache["windows"] = [
                {"pool": w.get("pool"), "wall": bool(w.get("wall")), "current": bool(w.get("current")),
                 "complete": w.get("complete"), "failed": bool((w.get("reading") or {}).get("error")),
                 **{k: (w.get("reading") or {}).get(k) for k in ("window", "used", "resets_at", "blocks", "group")}}
                for w in ledger.get("windows") or []]
            cache["pools"] = {p["name"]: p.get("kind") for p in ledger.get("pools") or []}
            cache["attempted_pools"] = sorted({a["pool"] for a in ledger.get("usage_attempts") or []
                                              if isinstance(a.get("pool"), str) and a.get("at")})
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


def label(pool: str) -> str:
    """The ship's subscription label: the part after its first hyphen, title-cased."""
    return pool.split("-", 1)[-1].replace("-", " ").title()


def allowances(cache: dict, ship: dict, own_pool: str | None) -> list[dict]:
    """A total and an unambiguous ledger-reported grouped constraint, without model-name guesses.

    A grouped-only reading remains a known constraint, not an invented whole allowance.
    Multiple groups do not prove which model's limit belongs on this seat's notch.
    """
    rows = []
    declared = ship.get("pools") or {}
    used = used_pools(cache, ship)
    providers = list(dict.fromkeys((spec or {}).get("provider", "") for spec in declared.values()))
    names = sorted(declared, key=lambda name: (providers.index((declared[name] or {}).get("provider", "")), name))
    now = time.time()
    unavailable = bool(cache.get("ledger_unavailable")) or now - cache.get("ledger_at", 0) > OLD

    def leading(windows):
        return max(windows, key=lambda w: (bool(w.get("wall")), w["used"]), default=None)

    def stale(window):
        return unavailable or not window.get("current") or seconds_until(window.get("resets_at")) <= 0

    for name in names:
        if name not in used:
            continue
        spec = declared[name] or {}
        kind = (cache.get("pools") or {}).get(name)
        if kind in ("key", "endpoint") or "endpoint" in spec or "api_key" in spec:
            continue
        windows = [w for w in cache.get("windows") or [] if w.get("pool") == name]
        if not windows and name not in (cache.get("attempted_pools") or []):
            continue  # declaration and current seat membership establish no usage reading
        known = [w for w in windows if readable(w)]
        blocking = [w for w in known if w.get("blocks") is not False]
        whole = [w for w in known if not w.get("group")]
        top = leading([w for w in whole if w.get("blocks") is not False] or whole or blocking or known)
        grouped = [w for w in blocking if w.get("group")]
        groups = {w["group"] for w in grouped}
        single = leading(grouped) if whole and len(groups) == 1 else None
        rows.append({"pool": name, "own": name == own_pool,
                     "used": top["used"] * 100 if top else None,
                     "span": " / ".join(str(top[k]) for k in ("group", "window") if top.get(k)) if top else "",
                     "resets": seconds_until(top.get("resets_at")) if top else None,
                     "wall": bool(top.get("wall")) if top else any(w.get("wall") for w in windows),
                     "observed_wall": any(w.get("wall") for w in windows),
                     "whole": bool(whole),
                     "unknown": not known or len(known) < len(windows) or any(w.get("failed") for w in windows),
                     "stale": bool(top) and stale(top),
                     "model": ({"used": single["used"] * 100,
                                "resets": seconds_until(single.get("resets_at")),
                                "wall": bool(single.get("wall")), "stale": stale(single)} if single else None)})
    return rows


def width(text: str) -> int:
    return sum(0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
               for char in ANSI.sub("", text))


def tone(used: float | None, wall: bool = False) -> str:
    if wall or (used is not None and used >= 100):
        return RED
    return AMBER if used is not None and used >= WARN else BLUE


def percent(used: float) -> str:
    return f"{used:.1f}%" if 0 < used < 1 else f"{used:.0f}%"


def cell(row: dict, detail: int) -> str:
    mark = f"{PINK}●{RESET} " if row["own"] else "  "
    name = f"{BOLD if row['own'] else ''}{label(row['pool'])}{RESET}"
    if row["used"] is None:
        return f"{mark}{name} {DIM}unknown{RESET}" + (f" {RED}WALL{RESET}" if row["wall"] else "")
    single = row.get("model")
    tighter = single if single and (single["wall"] and not row["wall"]
                                   or single["wall"] == row["wall"] and single["used"] > row["used"]) else None
    selected = tighter or row
    spent = selected["wall"] or selected["used"] >= 100
    unplaced_wall = row.get("observed_wall") and not spent
    hue = tone(row["used"], row["wall"])
    unknown = f"{DIM} + unknown{RESET}" if row["unknown"] else ""
    if detail == 0:
        old = f"{DIM}?{RESET}" if selected.get("stale") else ""
        wall = f" {RED}WALL{RESET}" if unplaced_wall or spent and selected["used"] < 100 else ""
        return f"{mark}{name} {tone(selected['used'], spent)}{percent(selected['used'])}{RESET}{old}{unknown}{wall}"
    bar = ""
    if detail >= 2 and row.get("whole", True):
        filled = min(BAR, max(1 if row["used"] >= 1 else 0, round(row["used"] / 100 * BAR)))
        track = [(hue, "━") if at < filled else (FAINT, "─") for at in range(BAR)]
        if single:
            at = min(BAR - 1, max(0, round(single["used"] / 100 * BAR) - 1))
            track[at] = (tone(single["used"], single["wall"]), "╋" if at < filled else "╂")
        bar = "".join(colour + glyph for colour, glyph in track) + f"{RESET} "
    figure = f"{hue}{percent(row['used'])}{RESET}"
    if row["stale"]:
        figure += f"{DIM}?{RESET}"
    if tighter:
        old = "?" if tighter.get("stale") else ""
        figure += f" {DIM}({RESET}{tone(tighter['used'], tighter['wall'])}{percent(tighter['used'])}{RESET}{DIM}{old}){RESET}"
    text = f"{mark}{name} {bar}{figure}{unknown}"
    resets = selected["resets"]
    if detail >= 3 and resets is not None and not unplaced_wall:
        when = ('back in ' if spent else '') + left(resets) if resets > 0 else 'reset passed'
        text += SEP + f"{RED if spent else DIM}{when}{RESET}"
    if unplaced_wall or spent and selected["used"] < 100 and (detail < 3 or resets is None):
        text += f" {RED}WALL{RESET}"
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
    """The ship's one/two rows; retain every selected subscription on narrower screens."""
    room = max(20, min(columns - MARGIN, BAND))
    who = identity(seat, name, session, 2)
    for detail in (3, 2):
        for first in (who + " " * max(0, WHO - width(who)), who):
            one = spread([first, *(cell(r, detail) for r in rows)], room, least=3, lead=2)
            if one:
                return [one]
    who = next((text for text in (identity(seat, name, session, d) for d in (2, 1, 0)) if width(text) <= room),
               identity(seat, name, session, 0))
    for detail in (3, 2, 1, 0):
        below = spread([cell(row, detail) for row in rows], room, least=2, most=6)
        if below:
            return [who, below]
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
    if forced.isdigit() and int(forced):
        return int(forced)
    try:
        with open("/dev/tty") as tty:
            columns = struct.unpack("hh", fcntl.ioctl(tty, termios.TIOCGWINSZ, b"\0" * 4))[1]
        if columns:
            return columns
    except (OSError, struct.error):
        pass
    return 120


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
