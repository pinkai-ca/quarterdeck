"""OpenCode's session-aware SQLite reader, from a read-only byte snapshot.

Only root-session inputs establish seat state and delivery receipts. Child sessions
are decoded independently and their completed turns remain in the accounting stream.
Unknown nested fields never discard readable siblings or turn malformed usage into zero.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .base import RecordError, State, Tokens, ToolCall, Turn
from .sqlite_snapshot import snapshot

DATA_PREFIX = "data-"
RECORD = Path("opencode") / "opencode.db"
RUNNING = {"pending", "running"}
MORE_STEPS = "tool-calls"  # a finish reason that says another step follows in the same turn


def _ms(value) -> datetime | None:
    try:
        return datetime.fromtimestamp(value / 1000, timezone.utc) if type(value) in (int, float) and value > 0 else None
    except (ValueError, OverflowError, OSError):
        return None


def _json(text) -> dict:
    try:
        value = json.loads(text) if isinstance(text, str) else None
    except json.JSONDecodeError:
        value = None
    return value if isinstance(value, dict) else {}  # a row this reader cannot parse is skipped, never fatal


def records(home: Path) -> list[Path]:
    """Every launch's database, oldest first: the data directories are numbered in launch order."""
    found = [p for p in home.glob(f"{DATA_PREFIX}*/{RECORD}") if p.is_file()]
    return sorted(found, key=lambda p: (_launch_number(p), str(p)))


def _launch_number(record: Path) -> int:
    try:
        return int(record.parents[1].name.removeprefix(DATA_PREFIX))
    except ValueError:
        return 0


def data_dir_of(record: Path) -> Path:
    return record.parents[1]


class _Rows:
    """The three tables a reading needs, from one snapshot of the database."""

    def __init__(self, record: Path):
        _, merged_bytes = snapshot(record)
        try:
            con = sqlite3.connect(":memory:")
            try:
                con.deserialize(merged_bytes)
                tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"session", "message", "part"} <= tables:
                    raise RecordError(f"{record.name}: not an OpenCode database")
                self.sessions = con.execute("SELECT id, time_created, time_updated, parent_id FROM session ORDER BY time_created, id").fetchall()
                self.messages = [(mid, sid, created, updated, _json(data)) for mid, sid, created, updated, data in con.execute(
                    "SELECT id, session_id, time_created, time_updated, data FROM message ORDER BY time_created, id")]
                self.parts = [(pid, mid, sid, created, updated, _json(data)) for pid, mid, sid, created, updated, data in con.execute(
                    "SELECT id, message_id, session_id, time_created, time_updated, data FROM part ORDER BY time_created, id")]
            finally:
                con.close()
        except sqlite3.Error as error:
            raise RecordError(f"{record.name}: {error}") from error


def _object(value) -> dict:
    return value if isinstance(value, dict) else {}


def _text(value) -> str | None:
    return value if isinstance(value, str) else None


def _cost(value) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        return float(value) if value >= 0 and math.isfinite(value) else None
    except OverflowError:
        return None


def _tokens(step: dict) -> Tokens:
    tokens = step.get("tokens")
    if not isinstance(tokens, dict) or "total" not in tokens:
        return Tokens.unknown()
    cache = tokens.get("cache", {})
    if not isinstance(cache, dict):
        return Tokens.unknown()
    counts = [tokens.get(k, 0) for k in ("total", "input", "output", "reasoning")]
    counts += [cache.get(k, 0) for k in ("read", "write")]
    if any(type(n) is not int or n < 0 for n in counts):
        return Tokens.unknown()
    _, uncached, output, reasoning, cached, written = counts
    return Tokens(read_cached=cached, read_uncached=uncached, cache_written=written, written=output + reasoning)


def _tool_call(part: dict, state: dict) -> ToolCall:
    time = _object(state.get("time"))
    start, end = _ms(time.get("start")), _ms(time.get("end"))
    seconds = (end - start).total_seconds() if start and end and end >= start else None
    exit_code = _object(state.get("metadata")).get("exit")
    name = _text(part.get("tool")) or "?"
    failed = state.get("status") == "error" or name == "invalid" or (type(exit_code) is int and exit_code != 0)
    return ToolCall(name, seconds, failed)


def _active_session(rows: _Rows) -> str | None:
    # Each fresh launch has its own database. A later /new root can supersede it;
    # a task's newer child session never becomes the root to resume.
    roots = [sid for sid, _, _, parent in rows.sessions if parent is None]
    return roots[-1] if roots else None


class _Session:
    """Turn boundaries and composer state for one session only."""

    def __init__(self, users, answers):
        self.turns: list[Turn] = []
        self.open = False
        self.queued: list[str] = []
        self.model = self.last_input = self.last_input_at = self.open_tool = self.open_tool_since = None
        for pos, (index, mid, data, own) in enumerate(users):
            moved_on = any(answers.get(later[1]) for later in users[pos + 1:])
            self.user(index, data, own, answers.get(mid, []), moved_on)

    def user(self, index: int, data: dict, own: list[dict], steps: list[tuple[dict, list[dict]]], moved_on: bool) -> None:
        text = "\n".join(p["text"] for p in own if p.get("type") == "text" and isinstance(p.get("text"), str))
        if self.open:
            self.queued.append(text)  # typed while an earlier turn runs: taken, shown, read when that turn ends
            return
        at = _ms(_object(data.get("time")).get("created"))
        self.last_input, self.last_input_at = text, at
        model = _text(_object(data.get("model")).get("modelID"))
        tokens, tools, reply, cost = Tokens(), [], "", 0.0
        running = None
        for step, step_parts in steps:
            tokens = tokens + _tokens(step)
            model = _text(step.get("modelID")) or model
            amount = _cost(step.get("cost", 0))
            cost = cost + amount if cost is not None and amount is not None else None
            for part in step_parts:
                kind = part.get("type")
                if kind == "text" and isinstance(part.get("text"), str):
                    reply = part["text"]
                elif kind == "tool":
                    state = _object(part.get("state"))
                    if _text(state.get("status")) in RUNNING:
                        running = (_text(part.get("tool")) or "?", _ms(_object(state.get("time")).get("start")))
                    elif state.get("status") in ("completed", "error"):
                        tools.append(_tool_call(part, state))
        self.model = model or self.model
        last = steps[-1][0] if steps else {}
        ended = _ms(_object(last.get("time")).get("completed"))
        timed = True
        # A turn ends with a step that finished for good, or with an error; it also ends when the program answered a
        # later message, since a queued message is taken as the next turn straight after a tool result, with no closing step.
        closed = bool(steps) and ended is not None and running is None and (last.get("finish") != MORE_STEPS or bool(last.get("error")))
        if moved_on and not closed:
            closed, timed = True, ended is not None
            ended = ended or _ms(_object(last.get("time")).get("created")) or at
            if running:
                tools.append(ToolCall(running[0], None, None))  # left running when the program moved on: its end unknown
        if closed:
            self.turns.append(Turn(index, at, ended, text, reply, model, tokens, tuple(tools), cost=cost, timed=timed))
        else:
            self.open = True
            self.open_tool, self.open_tool_since = running or (None, None)


class _Reader:
    def __init__(self, record: Path):
        rows = _Rows(record)
        self.empty = not (rows.sessions or rows.messages or rows.parts)
        self.session = _active_session(rows)
        parts: dict[tuple[str, str], list[dict]] = {}
        for _, mid, sid, _, _, data in rows.parts:
            parts.setdefault((sid, mid), []).append(data)
        users, answers = {}, {}
        index = 0
        for mid, sid, _, _, data in rows.messages:
            if data.get("role") == "user":
                users.setdefault(sid, []).append((index, mid, data, parts.get((sid, mid), [])))
                index += 1  # stable even while an earlier parent turn has not ended yet
            elif data.get("role") == "assistant" and isinstance(data.get("parentID"), str):
                answers.setdefault(sid, {}).setdefault(data["parentID"], []).append((data, parts.get((sid, mid), [])))
        sessions = {sid: _Session(messages, answers.get(sid, {})) for sid, messages in users.items()}
        self.active = sessions.get(self.session, _Session([], {}))
        self.turns = sorted((turn for session in sessions.values() for turn in session.turns), key=lambda t: t.index)
        # A task's activity belongs to its root for the stall clock, not for turn closure.
        family = {self.session} if self.session else set()
        while True:
            children = {sid for sid, _, _, parent in rows.sessions if parent in family}
            if children <= family:
                break
            family |= children
        updated = [m[3] for m in rows.messages if m[1] in family]
        updated += [p[4] for p in rows.parts if p[2] in family] + [s[2] for s in rows.sessions if s[0] in family]
        self.last_write = max((at for value in updated if (at := _ms(value))), default=None)

    def state(self) -> State:
        active = self.active
        return State(turn_open=active.open if self.session or self.empty else None, open_tool=active.open_tool,
                     open_tool_since=active.open_tool_since, last_write=self.last_write, last_input=active.last_input,
                     last_input_at=active.last_input_at, model=active.model, queued=tuple(active.queued), turns=len(self.turns))


def first_timestamp(record: Path) -> datetime | None:
    rows = _Rows(record).sessions
    return _ms(rows[0][1]) if rows else None


def session_id(record: Path) -> str | None:
    return _active_session(_Rows(record))


def state(record: Path) -> State:
    return _Reader(record).state()


def turns(record: Path) -> list[Turn]:
    return _Reader(record).turns


def read(record: Path) -> tuple[State, list[Turn]]:
    reader = _Reader(record)
    return reader.state(), reader.turns


def inputs(record: Path) -> list[tuple[str, datetime | None]]:
    """Every user message with its time, for the delivery receipt: a queued message is in the record the moment it is taken."""
    rows = _Rows(record)
    active = _active_session(rows)
    texts: dict[str, list[str]] = {}
    for _, mid, sid, _, _, data in rows.parts:
        if sid == active and data.get("type") == "text" and isinstance(data.get("text"), str):
            texts.setdefault(mid, []).append(data["text"])
    return [("\n".join(texts.get(mid, [])), _ms(_object(data.get("time")).get("created")))
            for mid, sid, _, _, data in rows.messages if sid == active and data.get("role") == "user"]
