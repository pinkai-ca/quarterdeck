"""Antigravity (``agy``), proven on 1.5.2.

State lives under ``~/.gemini/antigravity-cli`` with no home switch, so a seat passes ``--settings``
and joins only the single conversation that appears after launch; only records proven to belong to
the seat are ever opened (law: one-record). Each conversation is a SQLite database in WAL mode
holding protobuf blobs, read into an in-memory SQLite snapshot so neither ``~/.gemini`` nor system
``/tmp`` is ever written (law: one-folder).
"""

from __future__ import annotations

import json, re, struct
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..ship import Pool, SeatSpec
from .base import (Box, Control, Credential, Harness, Interruption, Join, Keys, Launch, RecordError, Screen, State,
                   Tokens, ToolCall, Turn, program_version, record_fingerprint)

from .antigravity_quota import banner, fetch_usage, record_wall, unknown
from .antigravity_wire import _bytes, _int, _proto, _snapshot_query, _str, _ts, _uints

STATE_DIR, BEFORE, OWNED = Path.home() / ".gemini" / "antigravity-cli", "conversations.before", "conversations.owned"
RULE, BG_TASK = re.compile(r"^─{20,}\s*$"), re.compile(r"^\s*● \[\d{2}:\d{2}:\d{2}\] .+$")
STATUS_LINE = re.compile(r"(?:^|\s{2,})([A-Z]\w*(?: [\w().-]+)*) · (low|medium|high|xhigh|max)(?: · \d+ task\(s\) · /tasks)?\s*$")
WORKING, EXIT_CODE = re.compile(r"esc to cancel|^[⠁-⣿]\s+\w+ing", re.M), re.compile(r"^Command exited with code (\d+)")
QUEUED_PLACEHOLDER, QUEUED_LINE = "Press up to edit queued messages", re.compile(r"^▸ (.*?)\s*\(alt\+m to send immediately\)\s*$")
STEP_USER_INPUT, STEP_PLANNER_RESPONSE, STEP_ERROR_MESSAGE, STEP_GENERIC = 14, 15, 17, 132
# exa.cortex_pb.CortexStepStatus, verified against the 1.5.2 descriptor (docs/ANTIGRAVITY.md).
ACTIVE_STATUSES, DONE_STATUSES, ABORTED_STATUSES = {1, 2, 8, 9, 11}, {3, 4, 5, 7}, {6, 12}
KNOWN_STATUSES = ACTIVE_STATUSES | DONE_STATUSES | ABORTED_STATUSES
TERMINATION_REASONS = {1, 2, 3, 4, 6, 7, 8, 9, 10, 11}


@dataclass(frozen=True)
class _Decoded:
    session: str | None
    created_at: datetime | None
    state: State
    turns: list[Turn]
_CACHE: dict[Path, tuple[tuple, _Decoded]] = {}

def _model_config(raw: bytes | None) -> str | None:
    return _str(_proto(_bytes(_proto(raw), 1)), 28)


def _parse_usage(raw: bytes | None) -> Tokens:
    if raw is None:
        return Tokens.unknown()
    uf = _proto(raw)
    _int(uf, 1)  # Model is an enum, never a model name string.
    return Tokens(_int(uf, 5) or 0, _int(uf, 2) or 0, _int(uf, 4), _int(uf, 3) or 0)

def _tool_failed(status: int, err_blob: bytes | None, payload: dict[int, list]) -> bool | None:
    if status == 7 or (err_blob and _proto(err_blob)):
        return True
    res = _proto(_bytes(_proto(_bytes(payload, 140)), 2)) if 140 in payload else {}
    any_msg = _proto(_bytes(res, 6))
    type_url = _str(any_msg, 1)
    fields = {"type.googleapis.com/exa.cortex.core.tools.RunCommand": (1, 2, 8),
              "type.googleapis.com/exa.cortex_pb.CortexStepRunCommand": (6, 37, None)}
    if type_url in fields:
        exit_field, timeout_field, unknown_field = fields[type_url]
        rc = _proto(_bytes(any_msg, 2))
        code, timed_out = _int(rc, exit_field), _int(rc, timeout_field)
        if code or timed_out:
            return True
        if (unknown_field and _int(rc, unknown_field)) or (unknown_field is None and code is None):
            return None
        return False
    if m := EXIT_CODE.search(_str(res, 1) or ""):
        return int(m.group(1)) != 0
    return False if status == 3 and type_url in (None, "type.googleapis.com/exa.cortex.core.tools.ReadFile",
                                                "type.googleapis.com/exa.cortex.core.tools.FileEdit") else None


def _is_closed(turn: dict, ended_execs: dict[str, tuple[int | None, str | None, int | None]]) -> bool:
    if turn["unknown_status"]:
        return False
    if (exec_id := turn["exec_id"]) and exec_id in ended_execs:
        last_step, _, reason = ended_execs[exec_id]
        if reason in TERMINATION_REASONS and (reason == 2 or not turn["active"]) and (last_step is None or last_step >= turn["last_step_idx"]):
            return True
    return not turn["active"] and turn["aborted"]

def _finalize_turn(turn: dict, ended_execs: dict[str, tuple[int | None, str | None, int | None]]) -> Turn:
    exec_info = ended_execs.get(turn["exec_id"], (None, None, None)) if turn["exec_id"] else (None, None, None)
    model, ended = turn["model"] or exec_info[1], turn["ended"]
    usages, costs = turn["usages"], turn["costs"]
    tokens = Tokens.unknown() if not usages else sum(usages[1:], start=usages[0])
    return Turn(turn["index"], turn["started"], ended, turn["input"], "\n\n".join(turn["replies"]).strip(),
                model, tokens, tuple(turn["tools"] + [ToolCall(name, None, None) for name, _ in turn["pending"].values()]), cost=sum(costs) if (costs and len(costs) == len(usages)) else None, subagent_turns=None)


def _decode(record: Path) -> _Decoded:
    if record.is_file() and (cached := _CACHE.get(record)) is not None and cached[0] == record_fingerprint(record):
        return cached[1]
    fp, cascade_id, meta_blob, steps, gen_rows, exec_rows = _snapshot_query(record)
    tm = _proto(meta_blob)
    created_at, session = _ts(_bytes(tm, 2)), cascade_id or _str(tm, 6) or record.stem
    gen_model_by_step, gen_model_by_exec, gen_usage_by_step, ended_execs, last_model = {}, {}, {}, {}, None
    planner_indices = {idx for idx, typ, *_ in steps if typ == STEP_PLANNER_RESPONSE}
    for _, data in gen_rows:
        exec_id, chat = _str(gm := _proto(data), 4) or "", _proto(_bytes(gm, 1))
        m_name = _str(chat, 21) or _str(chat, 19) or _str(_proto(_bytes(gm, 3)), 28)
        u_bytes = _bytes(chat, 4)
        if m_name and exec_id:
            gen_model_by_exec[exec_id] = m_name
        last_model = m_name or last_model
        indices = _uints(gm, 2)
        for s_idx in indices:
            gen_model_by_step[s_idx] = m_name or gen_model_by_step.get(s_idx)
        # One generation also lists its tools. Never charge the same usage to each step.
        planners = set(indices) & planner_indices
        if len(planners) == 1:
            gen_usage_by_step[next(iter(planners))] = u_bytes
    for _, data in exec_rows:
        reason, last_step, exec_id = _int(em := _proto(data), 1), _int(em, 3), _str(em, 9) or ""
        _int(em, 2)  # num_generator_invocations; this message contains no timestamp.
        exec_model = _model_config(_bytes(em, 10))
        last_model = exec_model or last_model
        if exec_id:
            ended_execs[exec_id] = (last_step, exec_model, reason)
    turns_out, consumed, current = [], [], None
    walls = []
    last_input = last_input_at = first_step_ts = last_step_ts = None
    unclosed_prior = any_unknown_status = False
    for idx, step_type, status, _, mblob, err_blob, pblob in steps:
        any_unknown_status = any_unknown_status or (status not in KNOWN_STATUSES)
        meta, payload = _proto(mblob), _proto(pblob)
        meta = meta or (_proto(_bytes(payload, 5)) if 5 in payload else {})
        created, viewable, finished = _ts(_bytes(meta, 1)), _ts(_bytes(meta, 6)), _ts(_bytes(meta, 7))
        completed, started = _ts(_bytes(meta, 8)), _ts(_bytes(meta, 32))
        if step_type == STEP_ERROR_MESSAGE and (event := record_wall(idx, payload, created)):
            walls.append(event)
        step_start, step_end, exec_id = started or created, completed, _str(meta, 12) or ""
        step_touch, first_step_ts = step_end or finished or viewable or step_start, first_step_ts or created or step_start
        last_step_ts = step_touch if step_touch and (last_step_ts is None or step_touch > last_step_ts) else last_step_ts
        step_model = gen_model_by_step.get(idx) or gen_model_by_exec.get(exec_id) or ended_execs.get(exec_id, (None, None, None))[1]
        last_model = step_model or last_model
        if step_type == STEP_USER_INPUT and (text := _str(ui := _proto(_bytes(payload, 19)), 2) or _str(ui, 1)) is not None:
            step_model = _model_config(_bytes(ui, 12)) or step_model
            last_model = step_model or last_model
            last_input, last_input_at = text, created or step_touch
            if current is not None and exec_id and current["exec_id"] == exec_id:
                consumed.append(text)
                current["last_step_idx"], current["ended"] = idx, step_end
                continue
            if current is not None:
                if _is_closed(current, ended_execs):
                    turns_out.append(_finalize_turn(current, ended_execs))
                else:
                    unclosed_prior = True  # never invent a newer USER_INPUT as the prior turn's terminal event
            current = {"index": idx, "exec_id": exec_id, "started": created or step_start, "ended": step_end,
                       "input": text, "replies": [], "model": step_model, "usages": [], "costs": [],
                       "tools": [], "pending": {}, "last_step_idx": idx, "active": status in ACTIVE_STATUSES,
                       "aborted": status in ABORTED_STATUSES, "unknown_status": status not in KNOWN_STATUSES}
            continue
        if step_type == STEP_USER_INPUT:
            any_unknown_status = True  # missing input payload: history cannot be counted completely
        if current is None:
            any_unknown_status = True
            continue
        # ERROR_MESSAGE is appended after an executor error; it has no execution_id and
        # is outside that executor's last_step_idx. It cannot invalidate a proven boundary.
        error_trailer = (step_type == STEP_ERROR_MESSAGE and not exec_id and status == 3
                         and ended_execs.get(current["exec_id"], (None, None, None))[2] == 1)
        current["exec_id"] = current["exec_id"] or exec_id
        if not error_trailer:
            current["last_step_idx"] = idx
        current["model"] = step_model or current["model"]
        current["ended"] = step_end  # only the final step completion can timestamp the turn end
        if status not in KNOWN_STATUSES:
            current["unknown_status"] = True
        elif status in ACTIVE_STATUSES | ABORTED_STATUSES:
            current["active"], current["aborted"] = (status in ACTIVE_STATUSES), (status in ABORTED_STATUSES)
        usage = _bytes(meta, 9) if 9 in meta else gen_usage_by_step.get(idx)
        if usage is not None or step_type == STEP_PLANNER_RESPONSE:
            current["usages"].append(_parse_usage(usage))
        if usage is not None and 10 in meta:
            cost_bytes = _bytes(meta, 10)
            if cost_bytes is None or len(cost_bytes) != 4:
                raise RecordError("invalid model_cost")
            current["costs"].append(struct.unpack("<f", cost_bytes)[0])
        if step_type == STEP_PLANNER_RESPONSE:
            if reply_text := (_str(pr := _proto(_bytes(payload, 20)), 8) or _str(pr, 1)):
                current["replies"].append(reply_text)
            for tc_bytes in pr.get(7, ()):
                tc = _proto(tc_bytes); current["pending"][_str(tc, 1) or f"step-{idx}"] = (_str(tc, 2) or "?", step_touch)
        elif 4 in meta or step_type == STEP_GENERIC:
            tc = _proto(_bytes(meta, 4)) if 4 in meta else {}
            prev_name, prev_start = current["pending"].get(tc_id := (_str(tc, 1) or f"step-{idx}"), ("?", None))
            tc_name = _str(tc, 2) or prev_name
            if status in ACTIVE_STATUSES:
                current["pending"][tc_id] = (tc_name, step_start or prev_start)
            elif status in DONE_STATUSES | ABORTED_STATUSES:
                current["pending"].pop(tc_id, None); start, end = step_start or prev_start, step_end
                secs = (end - start).total_seconds() if (start and end and end >= start and status not in ABORTED_STATUSES) else None
                current["tools"].append(ToolCall(tc_name, secs, _tool_failed(status, err_blob, payload)))
    turn_open, open_tool, open_tool_since = False, None, None
    if current is not None:
        if current["unknown_status"] or (current["exec_id"] in ended_execs and ended_execs[current["exec_id"]][2] not in TERMINATION_REASONS):
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
    file_write = datetime.fromtimestamp(m / 1e9, timezone.utc) if (m := max(fp[0][3], fp[1][3])) else None
    last_write = max(filter(None, (last_step_ts, file_write)), default=None)
    state_out = State(turn_open, open_tool, open_tool_since, last_write, last_input, last_input_at, last_model, None, (), tuple(consumed), turns_count, tuple(walls))
    _CACHE[record] = (fp, decoded := _Decoded(session, created_at or first_step_ts, state_out, turns_out))
    return decoded


def records(home: Path) -> list[Path]:
    """Only the conversations proven to belong to this seat (law: one-record)."""
    paths: list[Path] = []
    for source in (s for s in (home / OWNED, home.parent / "record") if s.is_file()):
        paths.extend(p for line in source.read_text().splitlines() if (p := Path(line.strip())).name and p not in paths)
    return paths

def first_timestamp(record: Path) -> datetime | None: return _decode(record).created_at
def session_id(record: Path) -> str | None: return _decode(record).session
def state(record: Path) -> State: return _decode(record).state
def turns(record: Path) -> list[Turn]: return list(_decode(record).turns)
def read(record: Path) -> tuple[State, list[Turn]]: return (d := _decode(record)).state, list(d.turns)


class AntigravityHarness(Harness):
    name, home_variable, brief_channel, record_confirms = "antigravity", None, "first-message", True
    record_limits = ("cache-write tokens and cost are unknown unless explicitly reported; effort and child-turn counts "
                     "are not exposed; missing completion timestamps leave durations unknown")
    reads_usage = True
    def usage(self, pool):
        if pool.config_dir is not None:
            return unknown("configured Google account directory is not qualified; /usage reads only the default local sign-in",
                           datetime.now(timezone.utc))
        return fetch_usage()

    keys = Keys(submit="Enter", send_now=("M-m",), interrupt="Escape", empty=None)

    def build_home(self, home: Path, seat: SeatSpec, pool: Pool, ship_home: Path | None) -> Credential | None:
        home.mkdir(parents=True, exist_ok=True)
        settings = {"trustedWorkspaces": [str(seat.cwd)] if seat.cwd else []}
        if ship_home and (ship_home / "settings.json").is_file():
            settings = {**json.loads((ship_home / "settings.json").read_text()), **settings}
        settings.update(seat.settings)
        (home / "settings.json").write_text(json.dumps(settings, indent=2) + "\n")
        return None

    def launch(self, seat: SeatSpec, pool: Pool, home: Path, brief: Path, resume: str | None, first: str | None) -> Launch:
        argv = ["agy", "--dangerously-skip-permissions", "--settings", str(home / "settings.json")]
        argv += (["--model", seat.model] if seat.model else []) + (["--effort", seat.effort] if seat.effort else [])
        argv += ["--conversation", resume] if resume else []
        if opening := "\n\n".join(filter(None, [brief.read_text().strip() if not resume else None, first])):
            argv += ["--prompt-interactive", opening]
        if not resume:
            (home / BEFORE).write_text("".join(f"{p.name}\n" for p in sorted(self._conversations())))
        return Launch(tuple(argv), {"AGY_CLI_DISABLE_AUTO_UPDATE": "true"})

    def version(self) -> str | None: return program_version(("agy", "--version"))
    @staticmethod
    def _conversations() -> list[Path]: return [p for p in (STATE_DIR / "conversations").glob("*.db") if p.is_file()]

    def _appeared(self, home: Path) -> list[Path]:
        noted = set((home / BEFORE).read_text().split()) if (home / BEFORE).is_file() else set()
        return sorted((p for p in self._conversations() if p.name not in noted), key=lambda p: p.stat().st_mtime)

    def join(self, home: Path, launched_at: datetime) -> Join:
        if not (appeared := self._appeared(home)):
            return Join(None, "no record yet")
        if len(appeared) > 1:
            return Join(None, f"ambiguous: {', '.join(p.name for p in appeared)} appeared since the launch")  # law: one-record
        return Join(appeared[0])

    records, first_timestamp, session_id = staticmethod(records), staticmethod(first_timestamp), staticmethod(session_id)
    state, turns, read = staticmethod(state), staticmethod(turns), staticmethod(read)

    def screen(self, text: str) -> Screen:
        lines = text.rstrip().split("\n")
        rules = [i for i, line in enumerate(lines) if RULE.match(line)]
        box, queued, top = None, (1 if QUEUED_PLACEHOLDER in text else 0), (rules[-2] if len(rules) >= 2 else len(lines))
        if len(rules) >= 2:
            top, bottom = rules[-2], rules[-1]
            if len(rules) >= 3 and (tasks := lines[top + 1 : bottom]) and all(BG_TASK.match(l) for l in tasks):
                top, bottom = rules[-3], rules[-2]
            if (span := lines[top + 1 : bottom]) and (span[0].rstrip() == ">" or span[0].startswith("> ")):
                body = [l[2:] if l.startswith(("> ", "  ")) else l.lstrip(">") for l in span]
                while body and not body[-1].strip():
                    body.pop()
                box = "\n".join(body)
        status = STATUS_LINE.search(lines[-1]) if lines else None
        shows = {"model": status.group(1).strip(), "effort": status.group(2)} if status else {}
        return Screen(text, box=box, working=bool(WORKING.search(text)), interruption=self.recognize(text) if box is None else None,
                      queued=queued, shows=shows, echoes=self.echoes(lines[:top]), quota=banner(text))

    @staticmethod
    def echoes(lines: list[str]) -> tuple[str, ...]:
        found, block = [], None
        for line in lines:
            if line.startswith("> "):
                found.append(block := [line[2:]])
            elif queued := QUEUED_LINE.match(line):
                found.append([queued.group(1).rstrip(".") + "…"]); block = None
            elif block is not None and (line.startswith("  ") or not line.strip()) and not line.lstrip().startswith("⎿"):
                block.append(line[2:] if line.startswith("  ") else "")
            else:
                block = None
        return tuple("\n".join(b).strip("\n") for b in found)

    def box(self, text: str) -> Box:
        return Box(text, folded=rf"\[Pasted text #\d+ (?:{len(text.removesuffix('\n'))} chars|\+{text.count('\n')} lines)\]")
    def controls(self) -> tuple[Control, ...]:
        return (Control("model", launch="--model <value>", session=None, confirmed_by="screen"),
                Control("effort", launch="--effort <value>", session=None, confirmed_by="screen"))
    def interruptions(self) -> tuple[Interruption, ...]:
        return (Interruption("trust dialog", r"Do you trust the contents of this project\?", key="Enter"),
                Interruption("login", r"Sign in|Log in to|Authenticate", key=None))
