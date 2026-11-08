"""OpenCode: a SQLite database per launch, ``data-<n>/opencode/opencode.db`` in the seat home.

OpenCode keeps every session of a data directory in one database, so the seat home gives each
launch a data directory of its own, named by ``XDG_DATA_HOME``, and the record is the database
written there; a resumed launch names the directory holding the session it continues. The
``session`` table dates the record; the ``message`` table holds the turn structure, a user message
and the assistant messages that answer it, each with its model, tokens, cost and completion time;
the ``part`` table holds the text, the tool calls with their timing and exit, and the step
boundaries. The database is read from a snapshot of its bytes and its write-ahead log, never
opened in place (law: one-folder). Proven on 1.19.5 against a local vLLM endpoint.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .. import endpoint
from ..ship import Pool, SeatSpec, ShipError
from .base import (Box, Control, Credential, Harness, Interruption, Keys, Launch, RecordError, Screen, State, Tokens,
                   ToolCall, Turn, merged, program_version)
from .sqlite_snapshot import snapshot

DATA_PREFIX = "data-"
RECORD = Path("opencode") / "opencode.db"
RUNNING = {"pending", "running"}
MORE_STEPS = "tool-calls"  # a finish reason that says another step follows in the same turn
CONFIG = {
    "$schema": "https://opencode.ai/config.json",
    "autoupdate": False,  # versions change when the operator says, and the status table says so
    "share": "disabled",  # a seat's conversation is the ship's, never a public link
}


# -- the record ---------------------------------------------------------------------------------


def _ms(value) -> datetime | None:
    return datetime.fromtimestamp(value / 1000, timezone.utc) if isinstance(value, (int, float)) and value > 0 else None


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
                self.sessions = con.execute("SELECT id, time_created, time_updated FROM session ORDER BY time_created, id").fetchall()
                self.messages = [(mid, sid, created, updated, _json(data)) for mid, sid, created, updated, data in con.execute(
                    "SELECT id, session_id, time_created, time_updated, data FROM message ORDER BY time_created, id")]
                self.parts = [(pid, mid, created, updated, _json(data)) for pid, mid, created, updated, data in con.execute(
                    "SELECT id, message_id, time_created, time_updated, data FROM part ORDER BY time_created, id")]
            finally:
                con.close()
        except sqlite3.Error as error:
            raise RecordError(f"{record.name}: {error}") from error


def _tokens(step: dict) -> Tokens:
    tokens = step.get("tokens") or {}
    if "total" not in tokens:
        return Tokens.unknown()  # a step the program never measured: aborted before the usage arrived
    cache = tokens.get("cache") or {}
    return Tokens(read_cached=cache.get("read") or 0, read_uncached=tokens.get("input") or 0,
                  cache_written=cache.get("write") or 0, written=(tokens.get("output") or 0) + (tokens.get("reasoning") or 0))


def _tool_call(part: dict, state: dict) -> ToolCall:
    time = state.get("time") or {}
    start, end = time.get("start"), time.get("end")
    seconds = (end - start) / 1000 if isinstance(start, (int, float)) and isinstance(end, (int, float)) else None
    exit_code = (state.get("metadata") or {}).get("exit")
    name = part.get("tool") or "?"
    failed = state.get("status") == "error" or name == "invalid" or (isinstance(exit_code, int) and exit_code != 0)
    return ToolCall(name, seconds, failed)


class _Reader:
    def __init__(self, record: Path):
        rows = _Rows(record)
        self.first = _ms(rows.sessions[0][1]) if rows.sessions else None
        self.session = rows.sessions[-1][0] if rows.sessions else None
        updated = [m[3] for m in rows.messages] + [p[3] for p in rows.parts] + [s[2] for s in rows.sessions]
        self.last_write = _ms(max(updated)) if updated else None
        parts: dict[str, list[dict]] = {}
        for _, mid, _, _, data in rows.parts:
            parts.setdefault(mid, []).append(data)
        users: list[tuple[str, dict, list[dict]]] = []
        answers: dict[str, list[tuple[dict, list[dict]]]] = {}
        for mid, _, _, _, data in rows.messages:
            if data.get("role") == "user":
                users.append((mid, data, parts.get(mid, [])))
            elif data.get("role") == "assistant" and data.get("parentID"):
                answers.setdefault(data["parentID"], []).append((data, parts.get(mid, [])))
        self.turns: list[Turn] = []
        self.open = False
        self.queued: list[str] = []
        self.model = self.last_input = self.last_input_at = self.open_tool = self.open_tool_since = None
        for index, (mid, data, own) in enumerate(users):
            moved_on = any(answers.get(later[0]) for later in users[index + 1:])  # a later message answered: this turn is over
            self.user(data, own, answers.get(mid, []), moved_on)

    def user(self, data: dict, own: list[dict], steps: list[tuple[dict, list[dict]]], moved_on: bool) -> None:
        text = "\n".join(p.get("text", "") for p in own if p.get("type") == "text")
        if self.open:
            self.queued.append(text)  # typed while an earlier turn runs: taken, shown, read when that turn ends
            return
        at = _ms((data.get("time") or {}).get("created"))
        self.last_input, self.last_input_at = text, at
        model = (data.get("model") or {}).get("modelID")
        tokens, tools, reply, cost = Tokens(), [], "", 0.0
        running = None
        for step, step_parts in steps:
            tokens = tokens + _tokens(step)
            model = step.get("modelID") or model
            cost += step.get("cost") or 0
            for part in step_parts:
                kind = part.get("type")
                if kind == "text" and part.get("text"):
                    reply = part["text"]
                elif kind == "tool":
                    state = part.get("state") or {}
                    if state.get("status") in RUNNING:
                        running = (part.get("tool") or "?", _ms((state.get("time") or {}).get("start")))
                    else:
                        tools.append(_tool_call(part, state))
        self.model = model or self.model
        last = steps[-1][0] if steps else {}
        ended = _ms((last.get("time") or {}).get("completed"))
        timed = True
        # A turn ends with a step that finished for good, or with an error; it also ends when the program answered a
        # later message, since a queued message is taken as the next turn straight after a tool result, with no closing step.
        closed = bool(steps) and ended is not None and running is None and (last.get("finish") != MORE_STEPS or bool(last.get("error")))
        if moved_on and not closed:
            closed, timed = True, ended is not None
            ended = ended or _ms((last.get("time") or {}).get("created")) or at
            if running:
                tools.append(ToolCall(running[0], None, None))  # left running when the program moved on: its end unknown
        if closed:
            self.turns.append(Turn(len(self.turns), at, ended, text, reply, model, tokens, tuple(tools), cost=cost, timed=timed))
        else:
            self.open = True
            self.open_tool, self.open_tool_since = running or (None, None)

    def state(self) -> State:
        return State(turn_open=self.open, open_tool=self.open_tool, open_tool_since=self.open_tool_since, last_write=self.last_write,
                     last_input=self.last_input, last_input_at=self.last_input_at, model=self.model, queued=tuple(self.queued),
                     turns=len(self.turns))


def first_timestamp(record: Path) -> datetime | None:
    rows = _Rows(record).sessions
    return _ms(rows[0][1]) if rows else None


def session_id(record: Path) -> str | None:
    rows = _Rows(record).sessions
    return rows[-1][0] if rows else None  # the live session: the one the program opened last


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
    texts: dict[str, list[str]] = {}
    for _, mid, _, _, data in rows.parts:
        if data.get("type") == "text":
            texts.setdefault(mid, []).append(data.get("text", ""))
    return [("\n".join(texts.get(mid, [])), _ms((data.get("time") or {}).get("created")))
            for mid, _, _, _, data in rows.messages if data.get("role") == "user"]


# -- the adapter --------------------------------------------------------------------------------

BAR_TEXT = re.compile(r"^\s*┃  (.*)$")
BAR_BARE = re.compile(r"^\s*┃\s*$")
STATUS = re.compile(r"^\s*┃  (\S+) (\S+) · (.+?)\s*$")  # "Build auto · Qwen3.8 27B local-a": the agent, the permission mode, the model and pool as shown
RULE = re.compile(r"^\s*╹▀{3,}")
PLACEHOLDER = re.compile(r'^Ask anything(?:…|\.\.\.) "[^"]*"\s*$')  # a fresh box's hint, with a question that varies
WORKING = re.compile(r"\besc interrupt\b")
QUEUED_TAG = re.compile(r"^\s*┃\s+QUEUED\s*$")
COMPACT = re.compile(r"ctrl\+p cmd\s*$", re.M)


def _bar(line: str) -> str | None:
    """The text of one box row; the empty string for a bare bar; None for a line that is not a bar."""
    if BAR_BARE.match(line):
        return ""
    found = BAR_TEXT.match(line)
    return found.group(1) if found else None


class OpenCodeHarness(Harness):
    """OpenCode, proven on 1.19.5. Runs on an endpoint pool; the model changes only at launch."""

    name = "opencode"
    home_variable = "XDG_DATA_HOME"
    keys = Keys(submit="Enter", send_now=None, interrupt="Escape Escape", empty=None)  # Escape once opens nothing; twice, quickly, interrupts
    brief_channel = "instructions"

    def accepts(self, pool: Pool) -> str | None:
        if pool.kind != "endpoint":
            return f"pool {pool.name} names no endpoint, and {self.name} runs on an endpoint pool: declare endpoint = \"http://host:port/v1\""
        return None

    def build_home(self, home: Path, seat: SeatSpec, pool: Pool, ship_home: Path | None) -> Credential | None:
        home.mkdir(parents=True, exist_ok=True)
        runtime = home.parents[2]  # runtime/seats/<seat>/home
        choice = endpoint.choose(pool, seat.model)
        secret = endpoint.key(pool)
        provider = {
            "npm": "@ai-sdk/openai-compatible",
            "name": pool.name,
            "options": {"baseURL": pool.endpoint, "apiKey": f"{{env:{endpoint.KEY_VARIABLE}}}" if secret else "local"},
            "models": {choice.model: {"name": choice.model, "tool_call": True, "limit": {"context": choice.context, "output": choice.output}}},
        }
        config = {**CONFIG, "model": f"{pool.name}/{choice.model}", "provider": {pool.name: provider},
                  "instructions": [str(runtime / "brief" / f"{seat.name}.md")]}  # the brief, and no instruction file of the user's
        template = ship_home / "opencode.json" if ship_home else None
        layered = json.loads(template.read_text()) if template and template.is_file() else {}
        config = merged(merged(config, layered), seat.settings)
        folder = home / "config" / "opencode"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "opencode.json").write_text(json.dumps(config, indent=2) + "\n")
        choice.write(home)
        return Credential(variable=endpoint.KEY_VARIABLE) if secret else None

    def launch(self, seat: SeatSpec, pool: Pool, home: Path, brief: Path, resume: str | None, first: str | None) -> Launch:
        choice = endpoint.Choice.read(home)
        data = self._data_dir(home, resume, seat.name)
        argv = ["opencode", "--auto", "--model", f"{pool.name}/{choice.model}"]  # --auto: no approval prompt (law: can-post)
        if resume:
            argv += ["--session", resume]
        if first:
            argv += ["--prompt", first]
        env = {"XDG_CONFIG_HOME": str(home / "config"), self.home_variable: str(data), "XDG_CACHE_HOME": str(home / "cache"),
               "XDG_STATE_HOME": str(home / "state"), "OPENCODE_DISABLE_AUTOUPDATE": "1", **endpoint.key(pool)}
        return Launch(tuple(argv), env)

    @staticmethod
    def _data_dir(home: Path, resume: str | None, seat_name: str) -> Path:
        """A fresh launch gets the next numbered data directory; a resumed one, the directory holding its session."""
        existing = records(home)
        if resume:
            for record in reversed(existing):
                try:
                    if any(row[0] == resume for row in _Rows(record).sessions):
                        return data_dir_of(record)
                except OSError:
                    continue
            raise ShipError(f"{seat_name}: no record in {home} holds the session {resume}; qd fresh {seat_name} starts a new one")
        number = max((_launch_number(r) for r in existing), default=0) + 1
        return home / f"{DATA_PREFIX}{number:03d}"

    def version(self) -> str | None:
        return program_version(("opencode", "--version"))

    records = staticmethod(records)
    first_timestamp = staticmethod(first_timestamp)
    session_id = staticmethod(session_id)
    state = staticmethod(state)
    turns = staticmethod(turns)
    read = staticmethod(read)

    def screen(self, text: str) -> Screen:
        """The box is the bar block straight above the status row, a pad row above and below its text."""
        lines = text.rstrip("\n").split("\n")
        rule_at = next((i for i in range(len(lines) - 1, 0, -1) if RULE.match(lines[i]) and STATUS.match(lines[i - 1])), None)
        working = bool(WORKING.search(text))
        queued = sum(1 for line in lines if QUEUED_TAG.match(line))
        interruption = self.recognize(text)
        if rule_at is None or interruption is not None:
            unknown = None
            if rule_at is None and interruption is None:
                unknown = "the compact layout: widen the pane" if COMPACT.search(text) else "no box drawn as 1.19.5 draws it"
            return Screen(text, box=None, working=working, interruption=interruption, unknown=unknown, queued=queued)
        status_at = rule_at - 1
        i = status_at - 1
        while i >= 0 and _bar(lines[i]) is not None and not QUEUED_TAG.match(lines[i]):
            i -= 1
        top = i + 1
        if i >= 0 and QUEUED_TAG.match(lines[i]) and top < status_at and _bar(lines[top]) == "":
            top += 1  # the queued block's own closing pad
        rows = [_bar(line) for line in lines[top:status_at]]
        if len(rows) < 2 or rows[0] != "" or rows[-1] != "":
            return Screen(text, box=None, working=working, unknown="no box drawn as 1.19.5 draws it", queued=queued)
        body = [row or "" for row in rows[1:-1]]
        while body and not body[-1].strip():
            body.pop()
        box = "" if len(body) == 1 and PLACEHOLDER.match(body[0]) else "\n".join(body)
        return Screen(text, box=box, working=working, queued=queued)

    def box_columns(self, pane_width: int) -> int | None:
        return pane_width - 7  # two cells of margin, the bar, two more cells, and a margin at the right

    def box(self, text: str) -> Box:
        """Probed on 1.19.5: a paste of three lines or more, or a long one, folds to a count of its lines."""
        lines = text.rstrip("\n").count("\n") + 1
        return Box(text, folded=rf"\[Pasted ~{lines} lines\]")

    def controls(self) -> tuple[Control, ...]:
        return (Control("model", launch="--model <pool>/<value>", session=None, confirmed_by="record"),)

    def interruptions(self) -> tuple[Interruption, ...]:
        return (Interruption("model picker", r"┃\s*/models\s+Switch model", key="Escape"),)
