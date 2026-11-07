"""Antigravity (``agy``), proven on 1.5.2.

The program has no home switch: its state lives under ``~/.gemini/antigravity-cli``, so a seat passes
its settings on the command line and joins the one conversation that appears after its launch, and
only a record proven to be the seat's own is ever opened (law: one-record). A conversation is a
SQLite database in WAL mode holding protobuf blobs. It is read from its bytes into an in-memory
snapshot, so nothing is written beside the program's files (law: one-folder). The fields read are
listed in docs/ANTIGRAVITY.md.
"""

from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..ship import Pool, SeatSpec
from .antigravity_quota import banner, fetch_usage, record_wall, unknown
from .antigravity_wire import _bytes, _int, _proto, _snapshot_query, _str, _ts, _uints
from .base import (Box, Control, Credential, Harness, Interruption, Join, Keys, Launch, RecordError, Screen, State,
                   Tokens, ToolCall, Turn, program_version, record_fingerprint)
from .limits import Usage

STATE_DIR = Path.home() / ".gemini" / "antigravity-cli"
BEFORE = "conversations.before"  # written in the seat home at launch: the conversations that already existed
OWNED = "conversations.owned"  # written in the seat home at every join: the conversations that are the seat's own

RULE = re.compile(r"^─{20,}\s*$")
BG_TASK = re.compile(r"^\s*● \[\d{2}:\d{2}:\d{2}\] .+$")  # a background task's line, drawn in a box of its own
STATUS_LINE = re.compile(r"(?:^|\s{2,})([A-Z]\w*(?: [\w().-]+)*) · (low|medium|high|xhigh|max)(?: · \d+ task\(s\) · /tasks)?\s*$")
WORKING = re.compile(r"esc to cancel|^[⠁-⣿]\s+\w+ing", re.M)
EXIT_CODE = re.compile(r"^Command exited with code (\d+)")
QUEUED_PLACEHOLDER = "Press up to edit queued messages"
QUEUED_LINE = re.compile(r"^▸ (.*?)\s*\(alt\+m to send immediately\)\s*$")

# The record's steps table: step types and statuses of exa.cortex_pb, verified against the 1.5.2 descriptors.
STEP_USER_INPUT, STEP_PLANNER_RESPONSE, STEP_ERROR_MESSAGE, STEP_GENERIC = 14, 15, 17, 132
ACTIVE_STATUSES = {1, 2, 8, 9, 11}  # pending, running, generating, waiting, queued
DONE_STATUSES = {3, 4, 5, 7}  # done, invalid, cleared, error
ABORTED_STATUSES = {6, 12}  # canceled, interrupted
KNOWN_STATUSES = ACTIVE_STATUSES | DONE_STATUSES | ABORTED_STATUSES
TERMINATION_REASONS = {1, 2, 3, 4, 6, 7, 8, 9, 10, 11}  # an executor that ended, whatever ended it
RUN_COMMAND = "type.googleapis.com/exa.cortex.core.tools.RunCommand"
STEP_RUN_COMMAND = "type.googleapis.com/exa.cortex_pb.CortexStepRunCommand"
QUIET_TOOLS = (None, "type.googleapis.com/exa.cortex.core.tools.ReadFile", "type.googleapis.com/exa.cortex.core.tools.FileEdit")
EXECUTOR_ERROR, EXECUTOR_CANCELED = 1, 2


@dataclass(frozen=True)
class _Decoded:
    """One reading of a record: its identity, its live state and its ended turns."""

    session: str | None
    created_at: datetime | None
    state: State
    turns: list[Turn]


_CACHE: dict[Path, tuple[tuple, _Decoded]] = {}  # record -> (its fingerprint when read, what it said)


def _model_config(raw: bytes | None) -> str | None:
    """CascadeConfig: its planner config's model name."""
    return _str(_proto(_bytes(_proto(raw), 1)), 28)


def _parse_usage(raw: bytes | None) -> Tokens:
    """A ModelUsageStats message: input tokens already less the cached ones, output, cache written, cache read.

    A class the message does not carry is zero: this provider reports no cache writes. No message at
    all is one step the record did not measure.
    """
    if raw is None:
        return Tokens.unknown()
    fields = _proto(raw)
    return Tokens(read_cached=_int(fields, 5) or 0, read_uncached=_int(fields, 2) or 0,
                  cache_written=_int(fields, 4) or 0, written=_int(fields, 3) or 0)


def _tool_failed(status: int, err_blob: bytes | None, payload: dict[int, list]) -> bool | None:
    """Whether a tool step failed: by its status, its error details, or its command's exit; None when it cannot say."""
    if status == 7 or (err_blob and _proto(err_blob)):
        return True
    result = _proto(_bytes(_proto(_bytes(payload, 140)), 2)) if 140 in payload else {}
    any_message = _proto(_bytes(result, 6))
    type_url = _str(any_message, 1)
    fields = {RUN_COMMAND: (1, 2, 8), STEP_RUN_COMMAND: (6, 37, None)}  # exit code, timed out, exit code unknown
    if type_url in fields:
        exit_field, timeout_field, unknown_field = fields[type_url]
        command = _proto(_bytes(any_message, 2))
        code, timed_out = _int(command, exit_field), _int(command, timeout_field)
        if code or timed_out:
            return True
        if (unknown_field and _int(command, unknown_field)) or (unknown_field is None and code is None):
            return None
        return False
    if match := EXIT_CODE.search(_str(result, 1) or ""):
        return int(match.group(1)) != 0
    return False if status == 3 and type_url in QUIET_TOOLS else None


def _is_closed(turn: dict, ended_execs: dict[str, tuple[int | None, str | None, int | None]]) -> bool:
    """Whether a turn ended: its executor ended past its last step, or it was aborted; never by a newer input alone."""
    if turn["unknown_status"]:
        return False
    if (exec_id := turn["exec_id"]) and exec_id in ended_execs:
        last_step, _, reason = ended_execs[exec_id]
        if reason in TERMINATION_REASONS and (reason == EXECUTOR_CANCELED or not turn["active"]) \
                and (last_step is None or last_step >= turn["last_step_idx"]):
            return True
    return not turn["active"] and turn["aborted"]


def _finalize_turn(turn: dict, ended_execs: dict[str, tuple[int | None, str | None, int | None]]) -> Turn:
    exec_info = ended_execs.get(turn["exec_id"], (None, None, None)) if turn["exec_id"] else (None, None, None)
    model = turn["model"] or exec_info[1]
    ended = turn["ended"] or turn["touch"]  # a turn its executor ended without a completion time ends at its last step
    usages, costs = turn["usages"], turn["costs"]
    tokens = Tokens.unknown() if not usages else sum(usages[1:], start=usages[0])
    still_open = [ToolCall(name, None, None) for name, _ in turn["pending"].values()]
    cost = sum(costs) if costs and len(costs) == len(usages) else None
    return Turn(turn["index"], turn["started"], ended, turn["input"], "\n\n".join(turn["replies"]).strip(),
                model, tokens, tuple(turn["tools"] + still_open), cost=cost, subagent_turns=None,
                timed=turn["ended"] is not None)


def _decode(record: Path) -> _Decoded:
    """Read the record once into its turns and state; a record whose bytes have not moved is not read again."""
    if record.is_file() and (cached := _CACHE.get(record)) is not None and cached[0] == record_fingerprint(record):
        return cached[1]
    fingerprint, cascade_id, meta_blob, steps, gen_rows, exec_rows = _snapshot_query(record)
    trajectory = _proto(meta_blob)
    created_at = _ts(_bytes(trajectory, 2))
    session = cascade_id or _str(trajectory, 6) or record.stem

    # Generator metadata: which model made which steps, and the usage of a generation whose one planner step is known.
    gen_model_by_step: dict[int, str | None] = {}
    gen_model_by_exec: dict[str, str] = {}
    gen_usage_by_step: dict[int, bytes | None] = {}
    last_model = None
    planner_indices = {idx for idx, kind, *_ in steps if kind == STEP_PLANNER_RESPONSE}
    for _, data in gen_rows:
        generation = _proto(data)
        exec_id = _str(generation, 4) or ""
        chat = _proto(_bytes(generation, 1))
        model_name = _str(chat, 21) or _str(chat, 19) or _str(_proto(_bytes(generation, 3)), 28)
        if model_name and exec_id:
            gen_model_by_exec[exec_id] = model_name
        last_model = model_name or last_model
        indices = _uints(generation, 2)
        for step_index in indices:
            gen_model_by_step[step_index] = model_name or gen_model_by_step.get(step_index)
        planners = set(indices) & planner_indices  # one generation also lists its tools: never charge each step the same usage
        if len(planners) == 1:
            gen_usage_by_step[next(iter(planners))] = _bytes(chat, 4)

    # Executor metadata: which executions ended, at which step, and why.
    ended_execs: dict[str, tuple[int | None, str | None, int | None]] = {}
    for _, data in exec_rows:
        executor = _proto(data)
        reason, last_step, exec_id = _int(executor, 1), _int(executor, 3), _str(executor, 9) or ""
        exec_model = _model_config(_bytes(executor, 10))
        last_model = exec_model or last_model
        if exec_id:
            ended_execs[exec_id] = (last_step, exec_model, reason)

    turns_out: list[Turn] = []
    consumed: list[str] = []
    walls = []
    current: dict | None = None
    last_input = last_input_at = first_step_at = last_step_at = None
    unclosed_prior = any_unknown_status = False
    for idx, step_type, status, _, meta_blob, err_blob, payload_blob in steps:
        any_unknown_status = any_unknown_status or status not in KNOWN_STATUSES
        meta, payload = _proto(meta_blob), _proto(payload_blob)
        meta = meta or (_proto(_bytes(payload, 5)) if 5 in payload else {})
        created, viewable, finished = _ts(_bytes(meta, 1)), _ts(_bytes(meta, 6)), _ts(_bytes(meta, 7))
        completed, started = _ts(_bytes(meta, 8)), _ts(_bytes(meta, 32))
        if step_type == STEP_ERROR_MESSAGE and (event := record_wall(idx, payload, created)):
            walls.append(event)
        step_start, step_end, exec_id = started or created, completed, _str(meta, 12) or ""
        step_touch = step_end or finished or viewable or step_start
        first_step_at = first_step_at or created or step_start
        if step_touch and (last_step_at is None or step_touch > last_step_at):
            last_step_at = step_touch
        step_model = gen_model_by_step.get(idx) or gen_model_by_exec.get(exec_id) or ended_execs.get(exec_id, (None, None, None))[1]
        last_model = step_model or last_model

        if step_type == STEP_USER_INPUT and (text := _str(user_input := _proto(_bytes(payload, 19)), 2) or _str(user_input, 1)) is not None:
            step_model = _model_config(_bytes(user_input, 12)) or step_model
            last_model = step_model or last_model
            last_input, last_input_at = text, created or step_touch
            if current is not None and exec_id and current["exec_id"] == exec_id:
                consumed.append(text)  # a message taken into the running turn
                current["last_step_idx"], current["ended"] = idx, step_end
                current["touch"] = step_touch or current["touch"]
                continue
            if current is not None:
                if _is_closed(current, ended_execs):
                    turns_out.append(_finalize_turn(current, ended_execs))
                else:
                    unclosed_prior = True  # a newer input is never invented as the prior turn's end
            current = {"index": idx, "exec_id": exec_id, "started": created or step_start, "ended": step_end,
                       "touch": step_touch, "input": text, "replies": [], "model": step_model, "usages": [], "costs": [],
                       "tools": [], "pending": {}, "last_step_idx": idx, "active": status in ACTIVE_STATUSES,
                       "aborted": status in ABORTED_STATUSES, "unknown_status": status not in KNOWN_STATUSES}
            continue
        if step_type == STEP_USER_INPUT:
            any_unknown_status = True  # an input without its text: the history cannot be counted whole
        if current is None:
            any_unknown_status = True
            continue

        # An ERROR_MESSAGE appended after an executor error has no execution id and sits past that
        # executor's last step; it is a trailer, and cannot reopen a proven boundary.
        error_trailer = (step_type == STEP_ERROR_MESSAGE and not exec_id and status == 3
                         and ended_execs.get(current["exec_id"], (None, None, None))[2] == EXECUTOR_ERROR)
        current["exec_id"] = current["exec_id"] or exec_id
        if not error_trailer:
            current["last_step_idx"] = idx
        current["model"] = step_model or current["model"]
        current["ended"] = step_end  # only the final step's completion can time the turn's end
        current["touch"] = step_touch or current["touch"]
        if status not in KNOWN_STATUSES:
            current["unknown_status"] = True
        elif status in ACTIVE_STATUSES | ABORTED_STATUSES:
            current["active"], current["aborted"] = status in ACTIVE_STATUSES, status in ABORTED_STATUSES
        usage = _bytes(meta, 9) if 9 in meta else gen_usage_by_step.get(idx)
        if usage is not None or step_type == STEP_PLANNER_RESPONSE:
            current["usages"].append(_parse_usage(usage))
        if usage is not None and 10 in meta:
            cost_bytes = _bytes(meta, 10)
            if cost_bytes is None or len(cost_bytes) != 4:
                raise RecordError("invalid model_cost")
            current["costs"].append(struct.unpack("<f", cost_bytes)[0])
        if step_type == STEP_PLANNER_RESPONSE:
            planner = _proto(_bytes(payload, 20))
            if reply_text := (_str(planner, 8) or _str(planner, 1)):
                current["replies"].append(reply_text)
            for call_bytes in planner.get(7, ()):
                call = _proto(call_bytes)
                current["pending"][_str(call, 1) or f"step-{idx}"] = (_str(call, 2) or "?", step_touch)
        elif 4 in meta or step_type == STEP_GENERIC:
            call = _proto(_bytes(meta, 4)) if 4 in meta else {}
            call_id = _str(call, 1) or f"step-{idx}"
            prev_name, prev_start = current["pending"].get(call_id, ("?", None))
            call_name = _str(call, 2) or prev_name
            if status in ACTIVE_STATUSES:
                current["pending"][call_id] = (call_name, step_start or prev_start)
            elif status in DONE_STATUSES | ABORTED_STATUSES:
                current["pending"].pop(call_id, None)
                start, end = step_start or prev_start, step_end
                timed = start and end and end >= start and status not in ABORTED_STATUSES
                seconds = (end - start).total_seconds() if timed else None
                current["tools"].append(ToolCall(call_name, seconds, _tool_failed(status, err_blob, payload)))

    turn_open, open_tool, open_tool_since = False, None, None
    if current is not None:
        executor_unknown = current["exec_id"] in ended_execs and ended_execs[current["exec_id"]][2] not in TERMINATION_REASONS
        if current["unknown_status"] or executor_unknown:
            turn_open = None
        elif _is_closed(current, ended_execs):
            turns_out.append(_finalize_turn(current, ended_execs))
        else:
            turn_open = True
            if (first_pending := next(iter(current["pending"].values()), None)) is not None:
                open_tool, open_tool_since = first_pending
    if (any_unknown_status or unclosed_prior) and turn_open is False:
        turn_open = None
    turns_count = None if (any_unknown_status or unclosed_prior or turn_open is None) else len(turns_out)
    newest_write = max(fingerprint[0][3], fingerprint[1][3])
    file_write = datetime.fromtimestamp(newest_write / 1e9, timezone.utc) if newest_write else None
    last_write = max(filter(None, (last_step_at, file_write)), default=None)
    state = State(turn_open, open_tool, open_tool_since, last_write, last_input, last_input_at, last_model, None, (),
                  tuple(consumed), turns_count, tuple(walls))
    decoded = _Decoded(session, created_at or first_step_at, state, turns_out)
    _CACHE[record] = (fingerprint, decoded)
    return decoded


def records(home: Path) -> list[Path]:
    """Only the conversations proven to belong to this seat (law: one-record)."""
    paths: list[Path] = []
    for source in (s for s in (home / OWNED, home.parent / "record") if s.is_file()):
        for line in source.read_text().splitlines():
            if (path := Path(line.strip())).name and path not in paths:
                paths.append(path)
    return paths


def first_timestamp(record: Path) -> datetime | None:
    return _decode(record).created_at


def session_id(record: Path) -> str | None:
    return _decode(record).session


def state(record: Path) -> State:
    return _decode(record).state


def turns(record: Path) -> list[Turn]:
    return list(_decode(record).turns)


def read(record: Path) -> tuple[State, list[Turn]]:
    decoded = _decode(record)
    return decoded.state, list(decoded.turns)


class AntigravityHarness(Harness):
    name = "antigravity"
    home_variable = None  # no home switch: the seat runs in the program's own state directory
    brief_channel = "first-message"
    record_confirms = True
    reads_usage = True
    record_limits = ("cost is unknown unless the record states it; effort and child-turn counts are not exposed; "
                     "a step without a completion time leaves its duration unknown")
    keys = Keys(submit="Enter", send_now=("M-m",), interrupt="Escape", empty=None)

    def usage(self, pool: Pool) -> Usage:
        if pool.config_dir is not None:
            return unknown("a Google account directory is not read; /usage reads the sign-in the program itself holds",
                           datetime.now(timezone.utc))
        return fetch_usage()

    def build_home(self, home: Path, seat: SeatSpec, pool: Pool, ship_home: Path | None) -> Credential | None:
        """Our settings for the session, passed on the command line; nothing of the program's own is written."""
        home.mkdir(parents=True, exist_ok=True)
        settings = {"trustedWorkspaces": [str(seat.cwd)] if seat.cwd else []}
        if ship_home and (ship_home / "settings.json").is_file():
            settings = {**json.loads((ship_home / "settings.json").read_text()), **settings}
        settings.update(seat.settings)
        (home / "settings.json").write_text(json.dumps(settings, indent=2) + "\n")
        return None

    def launch(self, seat: SeatSpec, pool: Pool, home: Path, brief: Path, resume: str | None, first: str | None) -> Launch:
        argv = ["agy", "--dangerously-skip-permissions", "--settings", str(home / "settings.json")]
        if seat.model:
            argv += ["--model", seat.model]
        if seat.effort:
            argv += ["--effort", seat.effort]
        if resume:
            argv += ["--conversation", resume]
        opening = "\n\n".join(filter(None, [brief.read_text().strip() if not resume else None, first]))
        if opening:
            argv += ["--prompt-interactive", opening]
        if not resume:  # a new conversation is about to appear: note which ones are there already
            (home / BEFORE).write_text("".join(f"{p.name}\n" for p in sorted(self._conversations())))
        return Launch(tuple(argv), {"AGY_CLI_DISABLE_AUTO_UPDATE": "true"})

    def version(self) -> str | None:
        return program_version(("agy", "--version"))

    # -- record: the one conversation that appeared after the launch ---------------------------

    @staticmethod
    def _conversations() -> list[Path]:
        return [p for p in (STATE_DIR / "conversations").glob("*.db") if p.is_file()]

    def _appeared(self, home: Path) -> list[Path]:
        """The conversations the shared folder gained since the launch noted what was there."""
        noted = set((home / BEFORE).read_text().split()) if (home / BEFORE).is_file() else set()
        return sorted((p for p in self._conversations() if p.name not in noted), key=lambda p: p.stat().st_mtime)

    def join(self, home: Path, launched_at: datetime, excluded: tuple[Path, ...] = ()) -> Join:
        """The one conversation that appeared since the launch; two are reported, never guessed."""
        appeared = [p for p in self._appeared(home) if p not in excluded]
        if not appeared:
            return Join(None, "no record yet")
        if len(appeared) > 1:
            return Join(None, f"ambiguous: {', '.join(p.name for p in appeared)} appeared since the launch")  # law: one-record
        return Join(appeared[0])

    records = staticmethod(records)
    first_timestamp = staticmethod(first_timestamp)
    session_id = staticmethod(session_id)
    state = staticmethod(state)
    turns = staticmethod(turns)
    read = staticmethod(read)

    # -- screen ---------------------------------------------------------------------------

    def screen(self, text: str) -> Screen:
        lines = text.rstrip().split("\n")
        rules = [i for i, line in enumerate(lines) if RULE.match(line)]
        box, queued = None, 1 if QUEUED_PLACEHOLDER in text else 0  # the placeholder sits on the status line here
        top = rules[-2] if len(rules) >= 2 else len(lines)
        if len(rules) >= 2:
            top, bottom = rules[-2], rules[-1]
            tasks = lines[top + 1:bottom]
            if len(rules) >= 3 and tasks and all(BG_TASK.match(line) for line in tasks):
                top, bottom = rules[-3], rules[-2]  # the lowest box holds background tasks; the input box is above it
            span = lines[top + 1:bottom]
            if span and (span[0].rstrip() == ">" or span[0].startswith("> ")):
                body = [line[2:] if line.startswith(("> ", "  ")) else line.lstrip(">") for line in span]
                while body and not body[-1].strip():
                    body.pop()
                box = "\n".join(body)
                if box.rstrip(" ") == QUEUED_PLACEHOLDER:
                    box, queued = "", max(queued, 1)
        status = STATUS_LINE.search(lines[-1]) if lines else None
        shows = {"model": status.group(1).strip(), "effort": status.group(2)} if status else {}
        return Screen(text, box=box, working=bool(WORKING.search(text)), interruption=self.recognize(text) if box is None else None,
                      queued=queued, shows=shows, echoes=self.echoes(lines[:top]), quota=banner(text))

    @staticmethod
    def echoes(lines: list[str]) -> tuple[str, ...]:
        """Inputs the transcript shows: ``> `` blocks above the box with their indented lines, and a
        queued message's ``▸ …(alt+m to send immediately)`` line, cut short, as an echo ending in an ellipsis."""
        found: list[list[str]] = []
        block: list[str] | None = None
        for line in lines:
            queued = QUEUED_LINE.match(line)
            if line.startswith("> "):
                block = [line[2:]]
                found.append(block)
            elif queued:
                found.append([queued.group(1).rstrip(".") + "…"])
                block = None
            elif block is not None and (line.startswith("  ") or not line.strip()) and not line.lstrip().startswith("⎿"):
                block.append(line[2:] if line.startswith("  ") else "")  # a paragraph break continues the echo; ⎿ is the program's own line
            else:
                block = None
        return tuple("\n".join(b).strip("\n") for b in found)

    def box(self, text: str) -> Box:
        """A long paste folds two ways (1.5.2): one of few lines to ``[Pasted text #N M chars]``, M the length less
        one trailing newline; one of many lines to ``[Pasted text #N +k lines]``, k the newlines. The box takes either."""
        chars, newlines = len(text.removesuffix("\n")), text.count("\n")
        return Box(text, folded=rf"\[Pasted text #\d+ (?:{chars} chars|\+{newlines} lines)\]")

    def controls(self) -> tuple[Control, ...]:
        return (
            Control("model", launch="--model <value>", session=None, confirmed_by="screen"),
            Control("effort", launch="--effort <value>", session=None, confirmed_by="screen"),
        )

    def interruptions(self) -> tuple[Interruption, ...]:
        return (
            Interruption("trust dialog", r"Do you trust the contents of this project\?", key="Enter"),
            Interruption("login", r"Sign in|Log in to|Authenticate", key=None),
        )
