"""OpenCode's session-aware SQLite reader, from a read-only byte snapshot.

Only root-session inputs establish seat state and delivery receipts. Child sessions
are decoded independently and their completed turns remain in the accounting stream.
Unknown nested fields never discard readable siblings or turn malformed usage into zero.
"""

from __future__ import annotations

import json
import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..pricing import amount
from .costs import CostStep, costed, model_name
from .base import Input, RecordError, State, Tokens, ToolCall, Turn
from .sqlite_snapshot import snapshot
from .json_record import RecordReader, ended_at_limit
from .types import CappedGeneration

DATA_PREFIX = "data-"
RECORD = Path("opencode") / "opencode.db"
RUNNING = {"pending", "running"}
ENDS_TURN = ("stop", "length", "content-filter")
LIMIT_REASONS = frozenset({"length"})  # measured on 15 Nov: 14 of OpenCode's ended assistant messages in crew records
ABORTED = "MessageAbortedError"  # the program's name for an interrupted step, as the pinned 1.19.5 record shows


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
    from .. import record_location
    found = {*(p for p in home.glob(f"{DATA_PREFIX}*/{RECORD}") if p.is_file()),
             *record_location.records(home, f"{DATA_PREFIX}*/{RECORD}")}
    return sorted(found, key=lambda p: (_launch_number(p), str(p)))


def _launch_number(record: Path) -> int:
    try:
        return int(record.parents[1].name.removeprefix(DATA_PREFIX))
    except ValueError:
        return 0


def later_launch(record: Path) -> bool:
    """Whether this record's launch has ended: the seat's current launch, as qd's trusted launch record names it, was
    given a data folder numbered after this record's. A launch that has ended writes nothing more, so a turn it left
    open was cut, not paused. Folder names in the home, which the seat's jail writes, count for nothing here. A
    retired record has ended: it lies in the service's own tree, which no jail writes and no launch resumes, whether
    it is named there or in the home."""
    from .. import record_location
    record = Path(record)
    folder = record.parents[1].name if len(record.parents) > 2 else ''
    if not folder.startswith(DATA_PREFIX) or not folder[len(DATA_PREFIX):].isdigit():
        return False  # not a launch's own record: no claim of an ending is made
    if record_location.retired(record):
        return True
    current = _current_data(record.parents[2])
    return current is not None and current > _launch_number(record)


def _current_data(home: Path) -> int | None:
    """The number of the data folder qd gave the seat's current launch, from its trusted launch record; None where
    there is none, it cannot be read, or it names no data folder of this home."""
    from .. import trusted_files
    try:
        data = json.loads((trusted_files.of_home(home) / trusted_files.LAUNCH).read_text())
    except (OSError, ValueError):
        return None
    given = launched_data(home, data)
    return None if given is None else int(given.name[len(DATA_PREFIX):])


def launched_data(home: Path, launch) -> Path | None:
    """The data folder of this home that a trusted launch record (as qd wrote it) gave its launch, from its
    XDG_DATA_HOME word; None where it names none."""
    argv = launch.get('argv') if isinstance(launch, dict) else None
    if not isinstance(argv, list):
        return None
    named = None
    for at, word in enumerate(argv):
        if word == 'XDG_DATA_HOME' and at + 1 < len(argv):
            named = argv[at + 1]  # bwrap's --setenv NAME VALUE
        elif isinstance(word, str) and word.startswith('XDG_DATA_HOME='):
            named = word.partition('=')[2]  # env NAME=VALUE
    if not isinstance(named, str):
        return None
    given = Path(named)
    if given.parent != home or not given.name.startswith(DATA_PREFIX) or not given.name[len(DATA_PREFIX):].isdigit():
        return None
    return given


def data_dir_of(record: Path) -> Path:
    return record.parents[1]


class _Rows:
    """The three tables a reading needs, from one snapshot of the database."""

    def __init__(self, record: Path, *, resume_metadata: bool = False):
        _, merged_bytes = snapshot(record)
        try:
            con = sqlite3.connect(":memory:")
            try:
                con.deserialize(merged_bytes)
                tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"session", "message", "part"} <= tables:
                    raise RecordError(f"{record.name}: not an OpenCode database")
                self.sessions = con.execute("SELECT id, time_created, time_updated, parent_id FROM session ORDER BY time_created, id").fetchall()
                self.permissions = {}
                self.directories = {}
                if resume_metadata:
                    columns = {row[1] for row in con.execute('PRAGMA table_info(session)')}
                    if 'permission' in columns:
                        self.permissions = dict(con.execute('SELECT id, permission FROM session'))
                    if 'directory' in columns:
                        self.directories = dict(con.execute('SELECT id, directory FROM session'))
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


def _tokens(step: dict) -> Tokens:
    tokens = step.get("tokens")
    if not isinstance(tokens, dict) or "total" not in tokens:
        return Tokens.unknown()
    cache = tokens.get("cache", {})
    if not isinstance(cache, dict):
        return Tokens.unknown()
    measured = Tokens.from_usage({**tokens, "cached": cache.get("read", 0), "cache_written": cache.get("write", 0)},
                                 ("cached", "input", "cache_written", "output"), total="total")
    reasoning = tokens.get("reasoning", 0)
    if not measured.complete or type(reasoning) is not int or reasoning < 0:
        return Tokens.unknown()
    return Tokens.measured(read_cached=measured.read_cached, read_uncached=measured.read_uncached,
                           cache_written=measured.cache_written, written=measured.written + reasoning)


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
    """Turn boundaries and box state for one session only."""

    def __init__(self, users, answers, launch_ended: bool = False):
        self.turns: list[Turn] = []
        self.open = False
        self.queued: list[str] = []
        self.inputs: list[Input] = []
        self.ended_at: datetime | None = None
        self.completed_turns: list[Turn] = []
        self.model = self.last_input = self.last_input_at = self.open_tool = self.open_tool_since = None
        for pos, (index, mid, data, own) in enumerate(users):
            moved_on = any(answers.get(later[1]) for later in users[pos + 1:])
            self.user(index, data, own, answers.get(mid, []), moved_on, launch_ended and pos == len(users) - 1)

    def user(self, index: int, data: dict, own: list[dict], steps: list[tuple[dict, list[dict]]], moved_on: bool,
             launch_ended: bool = False) -> None:
        text = "\n".join(p["text"] for p in own if p.get("type") == "text" and isinstance(p.get("text"), str))
        at = _ms(_object(data.get("time")).get("created"))
        overlapped = self.ended_at is not None and (at is None or at <= self.ended_at)
        if self.open or (overlapped and not steps and not moved_on):
            self.queued.append(text)
            self.inputs.append(Input(text, at, "queued"))
            return  # finishing the preceding turn alone does not show that this queue entry was consumed
        if overlapped:
            consumed_at = next((stamp for step, _ in steps
                                if (stamp := _ms(_object(step.get("time")).get("created"))) is not None), None)
            self.inputs.append(Input(text, at, "consumed", consumed_at))
        else:
            self.inputs.append(Input(text, at))
        self.last_input, self.last_input_at = text, at
        model = _text(_object(data.get("model")).get("modelID"))
        tokens, tools, reply = Tokens(), [], ""
        running = None
        costs = []
        for step, step_parts in steps:
            measured = _tokens(step)
            tokens = tokens + measured
            step_model = model_name(step.get("modelID"))
            model = step_model or model
            costs.append(CostStep(step_model, measured, amount(step.get("cost"))))
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
        if not steps:
            tokens = Tokens.unknown()
        last = steps[-1][0] if steps else {}
        ended = _ms(_object(last.get("time")).get("completed"))
        timed = True
        # A turn ends with a step that finished for good, or with an error; it also ends when the program answered a
        # later message, since a queued message is taken as the next turn straight after a tool result, with no closing step.
        completed = bool(steps) and ended is not None and running is None and (last.get("finish") in ENDS_TURN or bool(last.get("error")))
        closed = completed
        if moved_on and not closed:
            closed, timed = True, ended is not None
            ended = ended or _ms(_object(last.get("time")).get("created")) or at
            if running:
                tools.append(ToolCall(running[0], None, None))  # left running when the program moved on: its end unknown
        elif launch_ended and not closed and steps:
            # Its launch has ended (a later one exists), so this turn was cut, never to be written again: it closes
            # untimed at its last step, counted as far as the record shows, a tool it left running noted as such.
            closed, timed = True, False
            ended = ended or _ms(_object(last.get("time")).get("created")) or at
            if running:
                tools.append(ToolCall(running[0], None, None))
        if closed:
            self.ended_at = ended if timed else None
            failure = None
            error = _object(last.get('error'))
            # An interrupt, the person's or the door's, is recorded as an error named MessageAbortedError; it ends the turn
            # without failing it, so it neither holds the door nor unsettles the interrupted message.
            if completed and last.get('error') and error.get('name') != ABORTED:  # law: record-first
                from .failures import words
                from .types import TurnFailure
                detail = error.get('message') or _object(error.get('data')).get('message')
                activity = any(p.get('type') in ('tool', 'reasoning') or p.get('type') == 'text' and p.get('text')
                               for _, parts in steps for p in parts)  # model output; no proof of its absence is recorded
                failure = TurnFailure(words(detail), ended, (self.inputs[-1],), True if activity else None, 'program error')
            self.turns.append(costed(Turn(index, at, ended, text, reply, model, tokens, tuple(tools), timed=timed,
                                          cost_steps=tuple(costs), failure=failure,
                                          end_reason=_text(last.get("finish")) if completed else None)))
            if completed:
                self.completed_turns.append(self.turns[-1])
        else:
            self.open = True
            self.open_tool, self.open_tool_since = running or (None, None)


class _Reader(RecordReader):
    def __init__(self, record: Path, *, _rows=None):
        rows = _Rows(record) if _rows is None else _rows
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
        self.launch_ended = isinstance(record, Path) and later_launch(record)
        sessions = {sid: _Session(messages, answers.get(sid, {}), self.launch_ended) for sid, messages in users.items()}
        self.active = sessions.get(self.session, _Session([], {}))
        self.turns = sorted((turn for session in sessions.values() for turn in session.turns), key=lambda t: t.index)
        # A task's activity belongs to its root for the stall clock, not for turn closure.
        family = {self.session} if self.session else set()
        while True:
            children = {sid for sid, _, _, parent in rows.sessions if parent in family}
            if children <= family:
                break
            family |= children
        # Inspect every relevant part, including older calls and children. The display
        # reader deliberately retains only one tool; it cannot certify exclusivity.
        pending = []
        known = all(data and data.get('role') in ('user', 'assistant') for _, _, _, _, data in rows.messages)
        for _, _, sid, _, _, data in rows.parts:
            if not data or not isinstance(data.get('type'), str):
                known = False
            if data.get('type') != 'tool':
                if data.get('type') not in ('text', 'reasoning', 'file', 'step-start', 'step-finish',
                                            'snapshot', 'patch', 'retry', 'compaction'):
                    known = False
                continue
            name, status = data.get('tool'), _object(data.get('state')).get('status')
            if name == 'task' and _object(_object(data.get('state')).get('input')).get('background'):
                known = False  # scheduling/returned job handles are not child completion evidence
            if not isinstance(name, str) or not name or status not in (*RUNNING, 'completed', 'error'):
                known = False
            elif status in RUNNING and not self.launch_ended:
                pending.append(name)  # once its launch has ended, a running tool was left so: none is pending
        if any(session.open for sid, session in sessions.items() if sid != self.session):
            known = False  # children/previous roots in this launch can be generating between tools
        self.pending_tools = tuple(pending) if known else None
        updated = [m[3] for m in rows.messages if m[1] in family]
        updated += [p[4] for p in rows.parts if p[2] in family] + [s[2] for s in rows.sessions if s[0] in family]
        self.last_write = max((at for value in updated if (at := _ms(value))), default=None)
        self.input_id, self.work_id, self.capped = _generation(rows, self.session)

    def state(self) -> State:
        active = self.active
        return State(turn_open=active.open if self.session or self.empty else None, open_tool=active.open_tool,
                     open_tool_since=active.open_tool_since, last_write=self.last_write, last_input=active.last_input,
                     last_input_at=active.last_input_at, model=active.model, queued=tuple(active.queued), turns=len(self.turns),
                     completed_turns=tuple(active.completed_turns), pending_tools=self.pending_tools,
                     conversation=self.session, input_id=self.input_id, work_id=self.work_id,
                     capped=self.capped if not active.open and not active.queued and self.pending_tools == () else None,
                     failure=active.turns[-1].failure if not active.open and active.turns else None,
                     ended_at_limit=ended_at_limit(LIMIT_REASONS, active.turns if not active.open else ()))


def _generation(rows, root):
    """Bind the terminal generation and original work, including native marked compaction continuation.

    This does not change turn closure, receipts or accounting. Unmarked replay after
    compaction has no provable original-work identity and cannot receive recovery.
    """
    parts = {}
    for pid, mid, sid, _, _, data in rows.parts:
        if sid == root:
            parts.setdefault(mid, []).append((pid, data))
    messages = [(mid, data) for mid, sid, _, _, data in rows.messages if sid == root]
    users, current, work, compacting = {}, None, None, False
    for mid, data in messages:
        if data.get('role') != 'user':
            continue
        own = [p for _, p in parts.get(mid, [])]
        current = mid
        users[mid] = data, own
        if own and all(p.get('type') == 'compaction' for p in own):
            compacting = True
        elif (compacting and own and all(p.get('type') == 'text' and isinstance(p.get('text'), str)
              and p.get('synthetic') is True and _object(p.get('metadata')).get('compaction_continue') is True for p in own)):
            compacting = False  # the installed client's explicit automatic-continuation marker
        elif (compacting or not own or any(p.get('type') != 'text' or not isinstance(p.get('text'), str)
              or p.get('synthetic') or _object(p.get('metadata')).get('compaction_continue') for p in own)):
            work, compacting = None, False
        else:
            work = mid if any(p['text'].strip() for p in own) else None
    if compacting:
        return current, None, None  # a completed summary is not an idle continuation of accepted work
    if not messages or current is None or work is None:
        return current, work, None
    mid, last = messages[-1]
    user = users[current][0]
    model = _object(user.get('model'))
    tokens, times = _object(last.get('tokens')), _object(last.get('time'))
    own = [p for _, p in parts.get(mid, [])]
    finished = [p for p in own if p.get('type') == 'step-finish']
    created, ended = _ms(times.get('created')), _ms(times.get('completed'))
    if (last.get('role') != 'assistant' or last.get('parentID') != current or last.get('finish') != 'length'
            or last.get('error') or last.get('summary') or last.get('agent') == 'compaction'
            or not created or not ended or ended < created
            or not isinstance(last.get('agent'), str) or not last['agent']
            or not all(isinstance(last.get(k), str) and last[k] for k in ('modelID', 'providerID'))
            or model.get('modelID') != last['modelID'] or model.get('providerID') != last['providerID']
            or model.get('variant') not in (None, 'default')
            or not _tokens(last).complete or type(tokens.get('reasoning')) is not int or tokens['reasoning'] <= 0
            or type(tokens.get('output')) is not int or tokens['output'] < 0
            or len(finished) != 1 or finished[0].get('reason') != 'length' or finished[0].get('tokens') != tokens):
        return current, work, None
    for p in own:
        kind = p.get('type')
        if kind in ('text', 'reasoning'):
            if not isinstance(p.get('text'), str) or (kind == 'text' and p['text']):
                return current, work, None
        elif kind == 'patch':
            if not isinstance(p.get('hash'), str) or not isinstance(p.get('files'), list) or not all(isinstance(f, str) for f in p['files']):
                return current, work, None
        elif kind not in ('step-start', 'step-finish', 'snapshot'):
            return current, work, None  # any tool, retry, file, compaction or unfamiliar payload refuses
    if not any(p.get('type') == 'reasoning' and p.get('text') for p in own):
        return current, work, None
    bound = [root, work, current, messages, sorted(parts.items())]
    signature = hashlib.sha256(json.dumps(bound, sort_keys=True).encode()).hexdigest()
    return current, work, CappedGeneration(root, work, current, mid, last['modelID'], last['providerID'],
                                           tokens['reasoning'], tokens['output'], signature)


def first_timestamp(record: Path) -> datetime | None:
    rows = _Rows(record).sessions
    return _ms(rows[0][1]) if rows else None


def session_id(record: Path) -> str | None:
    return _active_session(_Rows(record))


state = _Reader.read_state
turns = _Reader.read_turns
read = _Reader.read


def inputs(record: Path) -> list[Input]:
    """The active root's inputs, with the same session-aware queue boundaries as its state."""
    return _Reader(record).active.inputs
